"""The 'smart' half of Spindle: tidy messy names and tags, guess what a file is
from its filename, and look tracks up online (MusicBrainz, AcoustID, Cover Art
Archive, iTunes art). Every lookup is optional and fails quietly."""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request

UA = "Spindle/1.0 ( https://github.com/ ; smart CD ripper )"

# ---------------------------------------------------------------- text tidy
JUNK = [
    r"\b(?:www\.)?[a-z0-9-]+\.(?:com|net|org|ru|info|biz|us|to|cc)\b",
    r"[\(\[]\s*(?:\d{2,3}\s*k(?:bps)?|v0|v2|vbr|cbr|mp3|hq|hd|hi-?fi|320)\s*[\)\]]",
    r"[\(\[]\s*(?:official\s+)?(?:music\s+)?(?:video|audio|lyrics?|lyric video|visualizer)\s*[\)\]]",
    r"[\(\[]\s*(?:ripped|rip|downloaded|free download|explicit|clean|uncensored)[^\)\]]*[\)\]]",
    r"\s+-\s*(?:copy|kopie)\s*$", r"\s*\(\d\)\s*$",
]
KEEP_UPPER = {"DJ", "MC", "UK", "USA", "US", "LA", "NYC", "TV", "OK", "AC/DC", "ABBA", "TLC", "REM",
              "R.E.M.", "UB40", "INXS", "ELO", "UFO", "II", "III", "IV", "VI", "VII", "VIII", "IX",
              "XX", "EP", "LP", "BBC", "MTV", "OPP", "RnB", "NWA", "N.W.A", "KISS", "U2", "MGMT",
              "ZZ", "DMX", "LL", "AJR", "TNT", "SOS", "XO", "DNA"}
SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to",
         "vs", "vs.", "via", "with", "from", "into", "nor", "per", "feat."}


def fix_mojibake(s):
    if any(c in s for c in ("Ã", "â€", "Â")):
        for enc in ("cp1252", "latin-1"):
            try:
                return s.encode(enc).decode("utf-8")
            except UnicodeError:
                pass
    return s


def smart_case(s):
    """Only re-case text that is SHOUTING or all lowercase; leave deliberate
    styling like 'iPhone' or 'McCartney' alone."""
    letters = [c for c in s if c.isalpha()]
    if not letters or not (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        return s
    words = s.split(" ")
    out = []
    for i, w in enumerate(words):
        bare = w.strip("()[]\"'.,!?:")
        up = w.upper()
        if bare.upper() in KEEP_UPPER or up in KEEP_UPPER:
            out.append(up if up in KEEP_UPPER else w.replace(bare, bare.upper()))
        elif 0 < i < len(words) - 1 and w.lower() in SMALL and not out[-1].endswith((":", "-")):
            out.append(w.lower())
        else:
            out.append(re.sub(r"[A-Za-z][A-Za-z']*",
                              lambda m: m.group(0)[0].upper() + m.group(0)[1:].lower(), w))
    return " ".join(out)


def clean(s, fix_case=True):
    if not s:
        return ""
    s = fix_mojibake(str(s)).replace("\x00", "")
    if " " not in s.strip() and ("_" in s or s.count(".") > 1) and not re.search(r"\.\w{2,4}$", s):
        s = s.replace("_", " ").replace(".", " ")
    s = s.replace("_", " ")
    for pat in JUNK:
        s = re.sub(pat, " ", s, flags=re.I)
    s = re.sub(r"\s*\b(?:ft\.?|feat\.?|featuring)\s+", " feat. ", s, flags=re.I)
    s = re.sub(r"\(\s*feat\. ", "(feat. ", s)
    s = re.sub(r"[\s\-–]+$", "", re.sub(r"^[\s\-–.]+", "", s))
    s = re.sub(r"\s{2,}", " ", s).strip()
    s = re.sub(r"\(\s*\)|\[\s*\]", "", s).strip()
    return " - ".join(smart_case(x) for x in s.split(" - ")) if fix_case else s


GENERIC = re.compile(r"^(?:track|audio ?track|piste|pista|titel|untitled|unknown|new|song)\s*\d*$", re.I)


def guess_from_filename(path):
    """'03 - Fleetwood Mac - Dreams (www.mp3s.ru).mp3' -> (3, 'Fleetwood Mac', 'Dreams')"""
    base = os.path.splitext(os.path.basename(path))[0]
    base = fix_mojibake(base).replace("_", " ")
    num = None
    m = re.match(r"^\s*(?:cd\s*\d+\s*[-.]?\s*)?(\d{1,3})(?:\s*[-._)\]]\s*|\s+)(.*)$", base, re.I)
    if m and (m.group(2) or len(m.group(1)) <= 2):
        num, base = int(m.group(1)), m.group(2)
    m = re.match(r"^(?:track|piste|pista)\s*(\d{1,3})$", base.strip(), re.I)
    if m:
        return int(m.group(1)), "", ""
    parts = [p for p in re.split(r"\s+[-–—]\s+|\s*--\s*", base) if p.strip()]
    artist, title = "", base
    if len(parts) >= 2:
        if re.fullmatch(r"\d{1,3}", parts[0].strip()) and num is None:
            num, parts = int(parts[0]), parts[1:]
        if len(parts) >= 2:
            artist, title = parts[0], " - ".join(parts[1:])
    if GENERIC.match(title.strip()):
        title = ""
    return num, clean(artist), clean(title)


def safe_name(s, maxlen=120):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip().rstrip(".")
    s = re.sub(r"\s{2,}", " ", s)
    return (s[:maxlen].rstrip() or "Unknown")


# ------------------------------------------------------------- web helpers
_mb_lock = threading.Lock()
_mb_last = [0.0]
OFFLINE = {"on": False}


def _get(url, binary=False, timeout=12, headers=None):
    if OFFLINE["on"]:
        return None
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        return data if binary else json.loads(data.decode("utf-8", "replace"))
    except Exception:
        return None


def _mb(path, **params):
    with _mb_lock:                               # MusicBrainz asks for 1 request/second
        wait = 1.05 - (time.time() - _mb_last[0])
        if wait > 0:
            time.sleep(wait)
        _mb_last[0] = time.time()
    params["fmt"] = "json"
    return _get("https://musicbrainz.org/ws/2/%s?%s" % (path, urllib.parse.urlencode(params)))


def _credit(ac):
    return "".join(a.get("name", "") + a.get("joinphrase", "") for a in ac or []).strip()


def _norm(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s.replace("&", "and"))


def similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


# ------------------------------------------------------------- disc lookup
def lookup_discid(discid, n_tracks):
    """Retail CDs (and exact copies of them) match by disc ID."""
    if not discid:
        return None
    d = _mb("discid/" + discid, inc="recordings artist-credits")
    rels = (d or {}).get("releases") or []
    best = None
    for rel in rels:
        for med in rel.get("media", []):
            if len(med.get("tracks", [])) == n_tracks:
                best = (rel, med)
                break
        if best:
            break
    if not best:
        return None
    rel, med = best
    return {
        "album": rel.get("title", ""), "albumartist": _credit(rel.get("artist-credit")),
        "year": (rel.get("date") or "")[:4], "mbid": rel.get("id"),
        "disc": med.get("position", 1), "discs": len(rel.get("media", [])) or 1,
        "tracks": [{"title": t.get("title") or t["recording"].get("title", ""),
                    "artist": _credit(t.get("artist-credit") or t["recording"].get("artist-credit"))}
                   for t in med["tracks"]],
    }


def lookup_recording(artist, title, duration=None):
    """Best MusicBrainz recording for an artist/title guess."""
    if not title:
        return None
    q = 'recording:"%s"' % title.replace('"', "")
    if artist:
        q += ' AND artist:"%s"' % artist.replace('"', "")
    d = _mb("recording", query=q, limit=10)
    best, best_score = None, 0
    for r in (d or {}).get("recordings", []):
        ra = _credit(r.get("artist-credit"))
        sc = similar(title, r.get("title")) * 0.6 + (similar(artist, ra) * 0.4 if artist else 0.25)
        if duration and r.get("length"):
            if abs(r["length"] / 1000 - duration) < 6:
                sc += 0.1
            elif abs(r["length"] / 1000 - duration) > 30:
                sc -= 0.15
        if sc > best_score:
            best, best_score = r, sc
    if not best or best_score < 0.55:
        return None
    rel = _pick_release(best.get("releases") or [])
    return {
        "title": best.get("title"), "artist": _credit(best.get("artist-credit")),
        "album": rel.get("title", "") if rel else "", "year": (rel.get("date") or "")[:4] if rel else "",
        "release_mbid": rel.get("id") if rel else None, "score": min(1.0, best_score),
    }


def _pick_release(rels):
    """Prefer the original studio album over compilations/singles."""
    def rank(r):
        rg = r.get("release-group") or {}
        prim = rg.get("primary-type") or ""
        sec = rg.get("secondary-types") or []
        return ((prim == "Album") * 4 + (not sec) * 2 + (r.get("status") == "Official"),
                -int((r.get("date") or "9999")[:4] or 9999))
    return max(rels, key=rank) if rels else None


def fingerprint(path):
    exe = shutil.which("fpcalc")
    if not exe:
        here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
        for name in ("fpcalc.exe", "fpcalc"):
            if os.path.exists(os.path.join(here, name)):
                exe = os.path.join(here, name)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-json", path], capture_output=True, text=True, timeout=60)
        d = json.loads(out.stdout)
        return int(d["duration"]), d["fingerprint"]
    except Exception:
        return None


def lookup_acoustid(path, key):
    """Identify a track by its sound alone - rescues 'Track01.wav' with no info."""
    if not key or OFFLINE["on"]:
        return None
    fp = fingerprint(path)
    if not fp:
        return None
    url = "https://api.acoustid.org/v2/lookup?" + urllib.parse.urlencode({
        "client": key, "meta": "recordings releases releasegroups", "duration": fp[0], "fingerprint": fp[1]})
    d = _get(url)
    for res in sorted((d or {}).get("results", []), key=lambda r: -r.get("score", 0)):
        if res.get("score", 0) < 0.5:
            break
        for rec in res.get("recordings", []):
            if not rec.get("title"):
                continue
            rels = rec.get("releases") or []
            rel = None
            if rels:
                albums = [r for r in rels if (r.get("releasegroup") or {}).get("type") == "Album"
                          and not (r.get("releasegroup") or {}).get("secondarytypes")]
                rel = (albums or rels)[0]
            date = (rel or {}).get("date") or {}
            return {"title": rec["title"], "artist": _credit(rec.get("artists")),
                    "album": (rel or {}).get("title", ""), "year": str(date.get("year", "")) if date else "",
                    "release_mbid": (rel or {}).get("id"), "score": res["score"]}
    return None


# ----------------------------------------------------------------- artwork
def fetch_art(mbid=None, artist="", album="", title=""):
    """Returns (jpeg bytes, source) or (None, None)."""
    if mbid:
        img = _get("https://coverartarchive.org/release/%s/front-500" % mbid, binary=True, timeout=20)
        if img and len(img) > 2000:
            return img, "Cover Art Archive"
    for term, entity in ((artist + " " + album, "album"), (artist + " " + title, "song")):
        if not term.strip() or (entity == "album" and not album) or (entity == "song" and not title):
            continue
        d = _get("https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": term.strip(), "entity": entity, "limit": 5}))
        for r in (d or {}).get("results", []):
            name = r.get("collectionName", "")
            if album and entity == "album" and similar(album, name) < 0.6:
                continue
            if artist and similar(artist, r.get("artistName", "")) < 0.5:
                continue
            u = (r.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb")
            img = _get(u, binary=True, timeout=20) if u else None
            if img:
                return img, "iTunes"
    return None, None


def search_art(query):
    """Several cover choices for the 'pick different art' panel."""
    d = _get("https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": query, "entity": "album", "limit": 12}))
    return [{"thumb": r.get("artworkUrl100"),
             "url": (r.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb"),
             "label": "%s - %s" % (r.get("artistName", ""), r.get("collectionName", ""))}
            for r in (d or {}).get("results", []) if r.get("artworkUrl100")]
