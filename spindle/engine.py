"""Spindle's pipeline: scan -> rip -> identify -> (review) -> finish."""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid

from . import cdrom, smart

AUDIO_EXT = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav", ".aif", ".aiff",
             ".ape", ".wv", ".mpc", ".mp2", ".alac"}
LOSSY_KEEP = {".mp3": ".mp3", ".m4a": ".m4a", ".aac": ".m4a", ".ogg": ".ogg", ".opus": ".opus"}
HOME = os.path.join(os.path.expanduser("~"), ".spindle")
NOWIN = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}

DEFAULTS = {
    "output_dir": os.path.join(os.path.expanduser("~"), "Music", "Spindle"),
    "format": "flac",                     # flac | mp3 | m4a | opus
    "mp3_mode": "v0",                     # v0 | 320
    "loudness": "auto",                   # auto | track | album | replaygain | off
    "target_lufs": -14.0,
    "keep_lossy": True,                   # don't re-encode MP3s etc. from data discs; tag them instead
    "fix_case": True,
    "album_template": "{albumartist}/{album} ({year})/{disc}{track:02} - {title}",
    "mix_template": "Mixes/{album} ({year})/{track:02} - {artist} - {title}",
    "save_cover_file": True,
    "playlist": True,
    "acoustid_key": "",
    "auto_mode": False,
    "auto_eject": True,
    "auto_confidence": 0.75,
    "offline": False,
}


def ffmpeg_exe(name="ffmpeg"):
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
    for cand in (name + ".exe", name):
        if os.path.exists(os.path.join(here, cand)):
            return os.path.join(here, cand)
    return shutil.which(name)


class Engine:
    def __init__(self):
        os.makedirs(HOME, exist_ok=True)
        self.lock = threading.RLock()
        self.settings = dict(DEFAULTS)
        try:
            self.settings.update(json.load(open(os.path.join(HOME, "settings.json"))))
        except Exception:
            pass
        smart.OFFLINE["on"] = bool(self.settings.get("offline"))
        self.session = None
        self.job = {"running": False, "name": "", "pct": 0, "msg": "Ready"}
        self.log = []
        self.history = []
        self._stop = False
        self._known_media = {}
        self.drives = []
        threading.Thread(target=self._watch, daemon=True).start()

    # ------------------------------------------------------------ plumbing
    def note(self, msg, level="info"):
        with self.lock:
            self.log.append({"t": time.strftime("%H:%M:%S"), "msg": msg, "level": level})
            self.log = self.log[-300:]

    def save_settings(self, upd):
        with self.lock:
            for k, v in upd.items():
                if k in DEFAULTS:
                    self.settings[k] = type(DEFAULTS[k])(v) if DEFAULTS[k] is not None else v
            smart.OFFLINE["on"] = bool(self.settings.get("offline"))
            json.dump(self.settings, open(os.path.join(HOME, "settings.json"), "w"), indent=2)

    def state(self):
        with self.lock:
            return {"settings": self.settings, "session": self.session, "job": dict(self.job),
                    "log": self.log[-120:], "drives": self.drives, "history": self.history[-20:],
                    "tools": {"ffmpeg": bool(ffmpeg_exe()), "fpcalc": bool(ffmpeg_exe("fpcalc")),
                              "cdparanoia": bool(shutil.which("cdparanoia"))}}

    def run(self, name, fn, *a):
        with self.lock:
            if self.job["running"]:
                raise RuntimeError("Busy: " + self.job["name"])
            self.job = {"running": True, "name": name, "pct": 0, "msg": name + "..."}
            self._stop = False

        def wrap():
            try:
                fn(*a)
                self.progress(1, name + " done")
            except InterruptedError:
                self.note("Stopped.", "warn")
                self.progress(0, "Stopped")
            except Exception as e:
                self.note("%s failed: %s" % (name, e), "error")
                traceback.print_exc()
                self.progress(0, "Error: %s" % e)
            finally:
                with self.lock:
                    self.job["running"] = False
        threading.Thread(target=wrap, daemon=True).start()

    def progress(self, pct, msg=None):
        with self.lock:
            self.job["pct"] = round(max(0, min(1, pct)), 4)
            if msg:
                self.job["msg"] = msg

    def stop(self):
        self._stop = True

    def _check(self):
        if self._stop:
            raise InterruptedError()

    def _watch(self):
        """Polls the drives; in auto mode a freshly inserted disc runs end to end."""
        while True:
            try:
                drives = cdrom.list_drives()
                with self.lock:
                    self.drives = drives
                for d in drives:
                    was = self._known_media.get(d["id"])
                    self._known_media[d["id"]] = d["media"]
                    if d["media"] and was is False:
                        self.note("Disc inserted in %s %s" % (d["id"], d["label"] or ""))
                        if self.settings["auto_mode"] and not self.job["running"]:
                            self.run("Auto", self._auto, d["id"])
            except Exception:
                pass
            time.sleep(3)

    # ----------------------------------------------------------------- scan
    def scan(self, source):
        self.run("Scan", self._scan, source)

    def _scan(self, source):
        drive = next((d for d in cdrom.list_drives() if d["id"].lower() == source.lower()), None)
        sess = {"id": uuid.uuid4().hex[:8], "stage": "scanned", "groups": [], "created": time.time()}
        if drive:
            toc = None
            try:
                toc = cdrom.read_toc(drive["id"])
            except Exception as e:
                self.note("Couldn't read the disc's table of contents (%s)" % e, "warn")
            if toc and toc.audio_tracks:
                self._scan_audio(sess, drive, toc)
            elif drive.get("mount"):
                self._scan_files(sess, drive["mount"], kind="data", label=drive.get("label"))
                sess["source"]["drive"] = drive["id"]
            else:
                raise RuntimeError("No readable disc in %s (on a Pi, data discs must be mounted)" % drive["id"])
        elif os.path.isdir(source):
            self._scan_files(sess, source, kind="folder", label=os.path.basename(source.rstrip("/\\")))
        else:
            raise RuntimeError("Not a drive or folder: " + source)
        self._find_duplicates(sess)
        with self.lock:
            self.session = sess
        n = sum(len(g["tracks"]) for g in sess["groups"])
        self.note("Scanned %s: %d track%s in %d group%s" % (sess["source"]["label"] or source, n,
                  "" if n == 1 else "s", len(sess["groups"]), "" if len(sess["groups"]) == 1 else "s"))

    def _scan_audio(self, sess, drive, toc):
        discid = toc.musicbrainz_id()
        sess["source"] = {"kind": "audio", "drive": drive["id"], "label": drive.get("label") or "Audio CD",
                          "discid": discid, "data_session": toc.has_data}
        cache = os.path.join(HOME, "cache", discid.replace("-", "_").replace(".", "_"))
        tracks = []
        for t in toc.audio_tracks:
            wav = os.path.join(cache, "track%02d.wav" % t.number)
            tracks.append(self._track(n=t.number, title="", artist="", duration=round(t.seconds, 1),
                                      cd={"number": t.number, "lba": t.lba, "length": t.length},
                                      rip=wav if os.path.exists(wav) else None,
                                      confidence=0.0, how="disc"))
        sess["groups"].append(self._group(tracks, album="", label=sess["source"]["label"]))
        sess["cache"] = cache
        mins = sum(t["duration"] for t in tracks) / 60
        self.note("Audio CD: %d tracks, %.0f min, disc ID %s" % (len(tracks), mins, discid))
        if toc.has_data:
            self.note("This disc also has a data session (enhanced CD).")

    def _scan_files(self, sess, root, kind, label):
        sess["source"] = {"kind": kind, "path": root, "label": label or os.path.basename(root)}
        files = []
        for dp, dn, fn in os.walk(root):
            dn.sort(key=str.lower)
            for f in sorted(fn, key=str.lower):
                if os.path.splitext(f)[1].lower() in AUDIO_EXT and not f.startswith("._"):
                    files.append(os.path.join(dp, f))
        if not files:
            raise RuntimeError("No audio files found on %s" % (label or root))
        by_folder = {}
        for i, path in enumerate(files):
            self._check()
            self.progress(i / len(files), "Reading %s" % os.path.basename(path))
            by_folder.setdefault(os.path.dirname(path), []).append(self._read_file(path))
        for folder, tracks in by_folder.items():
            # A folder full of different albums (by tag) splits into one group per album
            albums = {}
            for t in tracks:
                albums.setdefault(smart._norm(t["_album"]) or "_", []).append(t)
            real = {k: v for k, v in albums.items() if k != "_"}
            if len(real) > 1 and all(len(v) >= 3 for v in real.values()):
                buckets = list(real.values()) + ([albums["_"]] if "_" in albums else [])
            else:
                buckets = [tracks]
            for b in buckets:
                fname = os.path.basename(folder) if folder != root.rstrip("/\\") else (label or "")
                sess["groups"].append(self._group(b, label=fname))
        for g in sess["groups"]:
            g["tracks"].sort(key=lambda t: (t["disc"] or 1, t["n"] or 999, t["src"].lower()))
            for i, t in enumerate(g["tracks"], 1):
                if not t["n"] or len({x["n"] for x in g["tracks"]}) < len(g["tracks"]):
                    t["n"] = i

    def _read_file(self, path):
        tags, dur, art = {}, 0, None
        try:
            import mutagen
            f = mutagen.File(path, easy=True)
            if f is not None:
                dur = getattr(f.info, "length", 0) or 0
                for k in ("title", "artist", "album", "albumartist", "date", "genre", "tracknumber", "discnumber"):
                    v = f.get(k)
                    if v:
                        tags[k] = str(v[0])
            art = _embedded_art(path)
        except Exception:
            pass
        n_f, a_f, t_f = smart.guess_from_filename(path)
        fix = self.settings["fix_case"]
        title = smart.clean(tags.get("title"), fix) or t_f
        artist = smart.clean(tags.get("artist"), fix) or a_f
        if smart.GENERIC.match(title or ""):
            title = ""
        n = _int(tags.get("tracknumber")) or n_f
        how = "tags" if tags.get("title") and tags.get("artist") else ("filename" if title and artist else "guess")
        conf = {"tags": 0.7, "filename": 0.5, "guess": 0.15}[how]
        t = self._track(n=n, title=title, artist=artist, duration=round(dur, 1), src=path,
                        confidence=conf, how=how, disc=_int(tags.get("discnumber")))
        t["_album"] = smart.clean(tags.get("album"), fix)
        t["_albumartist"] = smart.clean(tags.get("albumartist"), fix)
        t["_year"] = (tags.get("date") or "")[:4]
        t["_genre"] = smart.clean(tags.get("genre"), False)
        t["_art"] = art
        if tags.get("title") and smart.clean(tags["title"], fix) != tags["title"]:
            t["notes"].append("Tidied title (was \u201c%s\u201d)" % tags["title"][:60])
        return t

    def _track(self, **kw):
        t = {"id": uuid.uuid4().hex[:8], "n": None, "title": "", "artist": "", "duration": 0, "src": None,
             "rip": None, "cd": None, "confidence": 0, "how": "", "notes": [], "include": True,
             "disc": None, "bad_sectors": 0, "out": None, "lufs": None}
        t.update(kw)
        return t

    def _group(self, tracks, album="", label=""):
        def common(key):
            vals = [t.get(key) for t in tracks if t.get(key)]
            if not vals:
                return ""
            top = max(set(vals), key=vals.count)
            return top if vals.count(top) >= max(1, len(tracks) * 0.5) else ""
        g = {"id": uuid.uuid4().hex[:8], "album": album or common("_album"),
             "albumartist": common("_albumartist") or common("artist"), "year": common("_year"),
             "genre": common("_genre"), "compilation": False, "disc": 1, "discs": 1,
             "art": None, "art_source": None, "mbid": None, "label": label, "confidence": 0,
             "tracks": tracks}
        arts = [t.get("_art") for t in tracks if t.get("_art")]
        if arts:
            g["art"] = self._store_art(max(arts, key=len), "embedded")
            g["art_source"] = "Already in files"
        for t in tracks:
            for k in ("_art",):
                t.pop(k, None)
        return g

    def _find_duplicates(self, sess):
        seen = {}
        for g in sess["groups"]:
            for t in g["tracks"]:
                key = smart._norm(t["artist"]) + "|" + smart._norm(t["title"])
                if t["title"] and key in seen and abs(seen[key]["duration"] - t["duration"]) < 4:
                    t["include"] = False
                    t["notes"].append("Looks like a duplicate of track %s - skipped" % seen[key]["n"])
                else:
                    seen.setdefault(key, t)

    # ------------------------------------------------------------------ rip
    def rip(self):
        self.run("Rip", self._rip)

    def _rip(self):
        sess = self.session
        if not sess or sess["source"]["kind"] != "audio":
            return
        os.makedirs(sess["cache"], exist_ok=True)
        tracks = [t for t in sess["groups"][0]["tracks"] if t["include"]]
        total = sum(t["cd"]["length"] for t in tracks) or 1
        done = 0
        for t in tracks:
            self._check()
            wav = os.path.join(sess["cache"], "track%02d.wav" % t["n"])
            if t.get("rip") and os.path.exists(t["rip"]):
                done += t["cd"]["length"]
                continue
            tr = cdrom.Track(t["n"], t["cd"]["lba"], True)
            tr.length = t["cd"]["length"]
            base = done

            def prog(p, base=base, tr=tr, n=t["n"]):
                self.progress((base + p * tr.length) / total, "Ripping track %d of %d  (%d%%)" % (
                    n, len(tracks), p * 100))
            tmp = wav + ".part"
            bad = cdrom.rip_track(sess["source"]["drive"], tr, tmp, prog, stop=lambda: self._stop)
            os.replace(tmp, wav)
            t["rip"], t["bad_sectors"] = wav, bad
            if bad:
                t["notes"].append("%d unreadable sector%s (%.2fs) - disc may be scratched" % (
                    bad, "" if bad == 1 else "s", bad / 75))
                self.note("Track %d: %d unreadable sectors" % (t["n"], bad), "warn")
            done += t["cd"]["length"]
        sess["stage"] = "ripped"
        self.note("Ripped %d tracks to the cache." % len(tracks))

    # ------------------------------------------------------------- identify
    def identify(self):
        self.run("Identify", self._identify)

    def _identify(self):
        sess = self.session
        if not sess:
            raise RuntimeError("Scan a disc first")
        if sess["source"]["kind"] == "audio" and not all(t.get("rip") for t in sess["groups"][0]["tracks"]
                                                         if t["include"]):
            self._rip()
        key = self.settings.get("acoustid_key")
        all_tracks = [(g, t) for g in sess["groups"] for t in g["tracks"] if t["include"]]
        # 1) Whole-disc match for pressed / copied retail CDs
        if sess["source"]["kind"] == "audio":
            g = sess["groups"][0]
            self.progress(0.02, "Looking up disc ID...")
            hit = smart.lookup_discid(sess["source"]["discid"], len(g["tracks"]))
            if hit:
                g.update(album=hit["album"], albumartist=hit["albumartist"], year=hit["year"],
                         mbid=hit["mbid"], disc=hit["disc"], discs=hit["discs"])
                for t, ht in zip(g["tracks"], hit["tracks"]):
                    t.update(title=ht["title"], artist=ht["artist"], confidence=1.0, how="disc ID")
                self.note("Disc ID matched: %s - %s" % (hit["albumartist"], hit["album"]), "good")
                all_tracks = []
            else:
                self.note("No disc ID match (normal for a home-burnt mix) - identifying track by track.")
        # 2) Track by track
        for i, (g, t) in enumerate(all_tracks):
            self._check()
            self.progress(0.05 + 0.8 * i / max(1, len(all_tracks)),
                          "Identifying %d/%d  %s" % (i + 1, len(all_tracks), t["title"] or "unknown track"))
            audio = t.get("rip") or t.get("src")
            hit = None
            if t["confidence"] < 0.7 or not (t["title"] and t["artist"]):
                hit = smart.lookup_acoustid(audio, key) if audio else None
                if hit:
                    hit["how"] = "sound (AcoustID)"
            if not hit and t["title"]:
                hit = smart.lookup_recording(t["artist"], t["title"], t["duration"])
                if hit:
                    hit["how"] = "MusicBrainz"
            if hit:
                old = (t["artist"], t["title"])
                t["title"], t["artist"] = hit["title"], hit["artist"] or t["artist"]
                t["confidence"] = round(max(t["confidence"], 0.55 + 0.45 * hit["score"]), 2)
                t["how"] = hit["how"]
                t["_hit"] = hit
                if old[1] and (smart._norm(old[1]) != smart._norm(t["title"]) or
                               (old[0] and smart._norm(old[0]) != smart._norm(t["artist"]))):
                    t["notes"].append("Corrected from \u201c%s - %s\u201d" % old)
            elif not t["title"]:
                t["title"] = "Track %02d" % (t["n"] or i + 1)
                t["notes"].append("Couldn't identify - please name it" + (
                    "" if key else " (add a free AcoustID key in Settings to identify by sound)"))
        # 3) Album-level decisions
        for gi, g in enumerate(sess["groups"]):
            self._check()
            self.progress(0.86 + 0.13 * gi / len(sess["groups"]), "Finding album art...")
            self._decide_album(sess, g)
            for t in g["tracks"]:
                t.pop("_hit", None)
        sess["stage"] = "identified"
        low = sum(1 for g in sess["groups"] for t in g["tracks"] if t["include"] and t["confidence"] < 0.6)
        self.note("Identified. %s" % ("Everything looks confident." if not low else
                                      "%d track%s a look." % (low, " needs" if low == 1 else "s need")),
                  "good" if not low else "warn")

    def _decide_album(self, sess, g):
        tracks = [t for t in g["tracks"] if t["include"]] or g["tracks"]
        artists = [smart._norm(t["artist"].split(" feat.")[0]) for t in tracks if t["artist"]]
        distinct = len(set(artists))
        hits = [t.get("_hit") for t in tracks if t.get("_hit")]
        albums = [smart._norm(h["album"]) for h in hits if h.get("album")]
        top_album = max(set(albums), key=albums.count) if albums else None
        same_album = top_album and albums.count(top_album) >= max(2, len(tracks) * 0.6)
        g["compilation"] = distinct > max(2, len(tracks) * 0.4) and not same_album and not g["mbid"]
        if g["compilation"]:
            g["albumartist"] = "Various Artists"
            if not g["album"] or smart._norm(g["album"]) in ("", "audiocd", "untitled"):
                label = smart.clean(g.get("label") or "", True)
                if not label or re.fullmatch(r"(?i)(audio ?cd|cd ?rom|new volume|disc|cd)\s*\d*", label):
                    label = "Mix CD %s" % time.strftime("%Y-%m-%d")
                g["album"] = label
            m = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", g["album"] or "")
            g["year"] = g["year"] or (m.group(1) if m else "")
        else:
            if same_album:
                h = next(h for h in hits if smart._norm(h["album"]) == top_album)
                if not g["album"]:
                    g["album"] = h["album"]
                g["year"] = g["year"] or h.get("year", "")
                g["mbid"] = g["mbid"] or h.get("release_mbid")
            if not g["albumartist"] and artists:
                g["albumartist"] = max(set(t["artist"] for t in tracks if t["artist"]),
                                       key=[t["artist"] for t in tracks].count)
            if not g["album"]:
                g["album"] = smart.clean(g.get("label") or "", True) or "Unknown Album"
        # Artwork: real cover if we can find one; mixes get a mosaic of their tracks' covers
        if not g["art"] or g["art_source"] == "Already in files":
            img, src = None, None
            if not g["compilation"]:
                img, src = smart.fetch_art(g["mbid"], g["albumartist"], g["album"])
            else:
                tiles, seen = [], set()
                for t in tracks:
                    h = t.get("_hit") or {}
                    k = smart._norm(t["artist"])
                    if k in seen or len(tiles) >= 4:
                        continue
                    seen.add(k)
                    b, _ = smart.fetch_art(h.get("release_mbid"), t["artist"], h.get("album", ""), t["title"])
                    if b:
                        tiles.append(b)
                if tiles:
                    img, src = self._mosaic(tiles), "Mosaic of track covers"
            if img:
                g["art"], g["art_source"] = self._store_art(img, g["id"]), src
        conf = [t["confidence"] for t in tracks]
        g["confidence"] = round(min(conf) * 0.5 + (sum(conf) / len(conf)) * 0.5, 2) if conf else 0

    def _mosaic(self, tiles):
        tmp = tempfile.mkdtemp(prefix="spindle")
        paths = []
        for i, b in enumerate(tiles):
            p = os.path.join(tmp, "%d.jpg" % i)
            open(p, "wb").write(b)
            paths.append(p)
        if len(paths) < 4:
            paths = (paths * 4)[:4] if len(paths) > 1 else paths
        if len(paths) == 1:
            return tiles[0]
        out = os.path.join(tmp, "mosaic.jpg")
        args = [ffmpeg_exe(), "-y", "-loglevel", "error"]
        for p in paths:
            args += ["-i", p]
        fc = "".join("[%d:v]scale=500:500:force_original_aspect_ratio=increase,crop=500:500,setsar=1[v%d];" % (i, i)
                      for i in range(4))
        fc += "[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0[out]"
        subprocess.run(args + ["-filter_complex", fc, "-map", "[out]", "-frames:v", "1", "-q:v", "3", out],
                       capture_output=True, **NOWIN)
        data = open(out, "rb").read() if os.path.exists(out) else tiles[0]
        shutil.rmtree(tmp, ignore_errors=True)
        return data

    def _store_art(self, data, name):
        d = os.path.join(HOME, "art")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "%s_%s.jpg" % (name, uuid.uuid4().hex[:6]))
        open(p, "wb").write(data)
        return p

    def set_art(self, gid, data, source="Chosen by you"):
        g = self._g(gid)
        g["art"], g["art_source"] = self._store_art(data, gid), source

    def _g(self, gid):
        for g in (self.session or {}).get("groups", []):
            if g["id"] == gid:
                return g
        raise KeyError("No such album")

    def edit(self, upd):
        """Apply edits from the review screen."""
        with self.lock:
            for gu in upd.get("groups", []):
                g = self._g(gu["id"])
                for k in ("album", "albumartist", "year", "genre", "compilation", "disc", "discs"):
                    if k in gu:
                        g[k] = gu[k]
                for tu in gu.get("tracks", []):
                    t = next((t for t in g["tracks"] if t["id"] == tu["id"]), None)
                    if not t:
                        continue
                    for k in ("title", "artist", "n", "include"):
                        if k in tu and tu[k] != t[k]:
                            t[k] = int(tu[k] or 0) if k == "n" else tu[k]
                            if k in ("title", "artist"):
                                t["confidence"], t["how"] = 1.0, "you"
                                t["notes"] = [n for n in t["notes"] if not n.startswith("Couldn't identify")]
                conf = [t["confidence"] for t in g["tracks"] if t["include"]]
                if conf and g["confidence"]:
                    g["confidence"] = round(min(conf) * 0.5 + sum(conf) / len(conf) * 0.5, 2)

    # --------------------------------------------------------------- finish
    def finish(self):
        self.run("Finish", self._finish)

    def _finish(self):
        sess = self.session
        if not sess:
            raise RuntimeError("Nothing to finish")
        if sess["source"]["kind"] == "audio" and not all(t.get("rip") for t in sess["groups"][0]["tracks"]
                                                         if t["include"]):
            self._rip()
        S = self.settings
        jobs = [(g, t) for g in sess["groups"] for t in g["tracks"] if t["include"]]
        if not jobs:
            raise RuntimeError("No tracks selected")
        # Measure loudness first (needed for album gain)
        mode = S["loudness"]
        for i, (g, t) in enumerate(jobs):
            self._check()
            if mode != "off" and t["lufs"] is None:
                self.progress(0.3 * i / len(jobs), "Measuring loudness %d/%d" % (i + 1, len(jobs)))
                t["lufs"], t["peak"] = measure(t.get("rip") or t["src"])
        outs = []
        for i, (g, t) in enumerate(jobs):
            self._check()
            self.progress(0.3 + 0.68 * i / len(jobs), "Writing %d/%d  %s" % (i + 1, len(jobs), t["title"]))
            t["out"] = self._write_track(g, t, jobs)
            outs.append((g, t))
        # Covers & playlists
        done_dirs = {}
        for g, t in outs:
            done_dirs.setdefault((g["id"], os.path.dirname(t["out"])), (g, []))[1].append(t)
        for (gid, d), (g, ts) in done_dirs.items():
            if S["save_cover_file"] and g["art"] and os.path.exists(g["art"]):
                shutil.copyfile(g["art"], os.path.join(d, "cover.jpg"))
            if S["playlist"]:
                with open(os.path.join(d, smart.safe_name(g["album"]) + ".m3u8"), "w", encoding="utf-8") as f:
                    f.write("#EXTM3U\n")
                    for t in sorted(ts, key=lambda t: t["n"] or 0):
                        f.write("#EXTINF:%d,%s - %s\n%s\n" % (t["duration"], t["artist"], t["title"],
                                                             os.path.basename(t["out"])))
        sess["stage"] = "done"
        folders = sorted({d for (_, d) in done_dirs})
        sess["output_folders"] = folders
        with self.lock:
            self.history.append({"t": time.strftime("%Y-%m-%d %H:%M"), "albums": [g["album"] for g in sess["groups"]],
                                 "tracks": len(outs), "folder": folders[0] if folders else ""})
        self.note("Finished %d tracks into %s" % (len(outs), folders[0] if len(folders) == 1 else
                                                    "%d folders" % len(folders)), "good")
        if sess["source"].get("drive") and S["auto_eject"]:
            cdrom.eject(sess["source"]["drive"])

    def _gain(self, g, t, jobs):
        S = self.settings
        mode = S["loudness"]
        if mode == "auto":
            mode = "track" if g["compilation"] else "album"
        if mode == "off" or t["lufs"] is None:
            return mode, None, None, None
        target = float(S["target_lufs"])
        track_gain = target - t["lufs"]
        mates = [x["lufs"] for gg, x in jobs if gg is g and x["lufs"] is not None]
        album_l = 10 * __import__("math").log10(sum(10 ** (l / 10) for l in mates) / len(mates)) if mates else t["lufs"]
        album_gain = target - album_l
        return mode, round(track_gain, 2), round(album_gain, 2), album_l

    def _write_track(self, g, t, jobs):
        S = self.settings
        mode, tg, ag, album_l = self._gain(g, t, jobs)
        src = t.get("rip") or t["src"]
        sext = os.path.splitext(src)[1].lower()
        keep = S["keep_lossy"] and sext in LOSSY_KEEP and t.get("src")
        ext = LOSSY_KEEP[sext] if keep else {"flac": ".flac", "mp3": ".mp3", "m4a": ".m4a", "opus": ".opus"}[S["format"]]
        tmpl = S["mix_template"] if g["compilation"] else S["album_template"]
        rel = render_template(tmpl, g, t) + ext
        dest = os.path.join(S["output_dir"], rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        dest = _unique(dest)
        args = [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-map", "0:a:0",
                "-map_metadata", "-1"]
        rg = None
        apply = None
        if mode in ("track", "album") and not keep:
            apply = tg if mode == "track" else ag
        elif mode != "off" and tg is not None:
            rg = (-18.0 - t["lufs"], -18.0 - album_l, t.get("peak"))   # ReplayGain 2.0 reference: -18 LUFS
        if keep:
            args += ["-c:a", "copy"]
        else:
            if apply is not None:
                f = "volume=%.2fdB" % apply
                peak_after = (t.get("peak") or 0) + apply
                if peak_after > -1.0:
                    f += ",alimiter=limit=0.891:attack=5:release=60:level=disabled"
                args += ["-af", f]
                t["notes"] = [n for n in t["notes"] if not n.startswith("Loudness")] + ["Loudness %+.1f dB" % apply]
            args += {".flac": ["-c:a", "flac", "-compression_level", "8", "-sample_fmt", "s16"],
                     ".mp3": ["-c:a", "libmp3lame"] + (["-q:a", "0"] if S["mp3_mode"] == "v0" else ["-b:a", "320k"]),
                     ".m4a": ["-c:a", "aac", "-b:a", "256k"],
                     ".opus": ["-c:a", "libopus", "-b:a", "160k"]}[ext]
        if ext == ".m4a":
            args += ["-movflags", "+faststart"]
        tmp = dest + ".part" + ext
        r = subprocess.run(args + [tmp], capture_output=True, text=True, **NOWIN)
        if r.returncode != 0:
            raise RuntimeError("ffmpeg: " + (r.stderr.strip().splitlines() or ["error"])[-1])
        art = open(g["art"], "rb").read() if g["art"] and os.path.exists(g["art"]) else None
        tag_file(tmp, ext, g, t, art, rg)
        os.replace(tmp, dest)
        return dest

    # ----------------------------------------------------------------- auto
    def _auto(self, drive):
        self._scan(drive)
        if self.session["source"]["kind"] == "audio":
            self._rip()
        self._identify()
        worst = min((g["confidence"] for g in self.session["groups"]), default=0)
        if worst >= float(self.settings["auto_confidence"]):
            self._finish()
        else:
            self.note("Auto mode paused: some tracks need your review before finishing.", "warn")


# ------------------------------------------------------------------- helpers
def _int(v):
    try:
        return int(str(v).split("/")[0])
    except Exception:
        return None


def _unique(p):
    if not os.path.exists(p):
        return p
    b, e = os.path.splitext(p)
    i = 2
    while os.path.exists("%s (%d)%s" % (b, i, e)):
        i += 1
    return "%s (%d)%s" % (b, i, e)


def render_template(tmpl, g, t):
    disc = "%d-" % (g.get("disc") or 1) if (g.get("discs") or 1) > 1 else ""
    vals = {"albumartist": g["albumartist"] or t["artist"] or "Unknown Artist", "album": g["album"] or "Unknown Album",
            "year": g["year"] or "", "artist": t["artist"] or "Unknown Artist", "title": t["title"] or "Untitled",
            "genre": g.get("genre") or "", "disc": disc}
    if vals["year"] and vals["year"] in vals["album"]:
        vals["year"] = ""                                  # "Summer Mix 2009 (2009)" -> "Summer Mix 2009"

    def sub(m):
        key, fmt = m.group(1), m.group(2)
        if key == "track":
            n = t["n"] or 0
            return format(n, fmt) if fmt else str(n)
        v = vals.get(key, "")
        return smart.safe_name(v) if v else ""
    out = re.sub(r"\{(\w+)(?::([^}]+))?\}", sub, tmpl)
    out = re.sub(r"\s*\(\s*\)", "", out)                  # "(year)" with no year
    parts = [smart.safe_name(p) for p in re.split(r"[/\\]", out) if p.strip()]
    return os.path.join(*parts)


def measure(path):
    """Integrated loudness (LUFS) and true peak (dBTP)."""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostats", "-i", path, "-map", "0:a:0",
                        "-filter_complex", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True, text=True, errors="ignore", **NOWIN)
    tail = r.stderr[r.stderr.rfind("Summary:"):]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", tail)
    p = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", tail)
    lufs = float(i.group(1)) if i else None
    if lufs is not None and lufs < -69:
        lufs = None
    peak = float(p.group(1)) if p and p.group(1) != "-inf" else None
    return lufs, peak


def _embedded_art(path):
    try:
        import mutagen
        f = mutagen.File(path)
        if f is None:
            return None
        if hasattr(f, "pictures") and f.pictures:
            return f.pictures[0].data
        tags = f.tags or {}
        if hasattr(tags, "getall"):
            ap = tags.getall("APIC")
            if ap:
                return ap[0].data
        if "covr" in tags and tags["covr"]:
            return bytes(tags["covr"][0])
        for k in ("metadata_block_picture", "METADATA_BLOCK_PICTURE"):
            if k in tags:
                from mutagen.flac import Picture
                return Picture(base64.b64decode(tags[k][0])).data
    except Exception:
        pass
    return None


def tag_file(path, ext, g, t, art, rg):
    import mutagen
    n_total = len([x for x in g["tracks"] if x["include"]])
    common = {"title": t["title"], "artist": t["artist"], "album": g["album"],
              "albumartist": g["albumartist"], "date": g["year"], "genre": g.get("genre") or "",
              "tracknumber": "%d/%d" % (t["n"] or 0, n_total),
              "discnumber": "%d/%d" % (g.get("disc") or 1, g.get("discs") or 1)}
    common = {k: v for k, v in common.items() if v}
    rgt = {}
    if rg:
        rgt = {"REPLAYGAIN_TRACK_GAIN": "%+.2f dB" % rg[0], "REPLAYGAIN_ALBUM_GAIN": "%+.2f dB" % rg[1]}
        if rg[2] is not None:
            rgt["REPLAYGAIN_TRACK_PEAK"] = "%.6f" % (10 ** (rg[2] / 20))
    if ext == ".mp3":
        from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TCON, TCMP, APIC, TXXX
        tags = ID3()
        for cls, k in ((TIT2, "title"), (TPE1, "artist"), (TPE2, "albumartist"), (TALB, "album"),
                       (TRCK, "tracknumber"), (TPOS, "discnumber"), (TDRC, "date"), (TCON, "genre")):
            if k in common:
                tags.add(cls(encoding=3, text=common[k]))
        if g["compilation"]:
            tags.add(TCMP(encoding=3, text="1"))
        for k, v in rgt.items():
            tags.add(TXXX(encoding=3, desc=k, text=v))
        if art:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=art))
        tags.save(path, v2_version=3)
    elif ext == ".m4a":
        from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
        f = MP4(path)
        if f.tags is None:
            f.add_tags()
        f.tags.clear()
        m = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART",
             "date": "\xa9day", "genre": "\xa9gen"}
        for k, a in m.items():
            if k in common:
                f[a] = [common[k]]
        f["trkn"] = [(t["n"] or 0, n_total)]
        f["disk"] = [(g.get("disc") or 1, g.get("discs") or 1)]
        f["cpil"] = bool(g["compilation"])
        for k, v in rgt.items():
            f["----:com.apple.iTunes:" + k] = [MP4FreeForm(v.encode())]
        if art:
            f["covr"] = [MP4Cover(art, imageformat=MP4Cover.FORMAT_JPEG)]
        f.save()
    else:   # FLAC, Ogg Vorbis, Opus all use Vorbis comments
        from mutagen.flac import Picture
        f = mutagen.File(path)
        if f.tags is None:
            f.add_tags()
        f.tags.clear()
        for k, v in common.items():
            f.tags[k] = v
        if g["compilation"]:
            f.tags["compilation"] = "1"
        for k, v in rgt.items():
            f.tags[k] = v
        if art:
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Cover", art
            if ext == ".flac":
                f.clear_pictures()
                f.add_picture(pic)
            else:
                f.tags["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode()
        f.save()
