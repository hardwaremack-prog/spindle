"""Optical drive access for Spindle.

Works without any extra programs on Windows (talks to the drive directly) and on
Linux / Raspberry Pi OS (kernel ioctls, or cdparanoia when it is installed,
which is better at pulling audio off scratched discs).
"""
import base64
import ctypes
import glob
import hashlib
import os
import shutil
import struct
import subprocess
import sys
import wave

IS_WIN = sys.platform.startswith("win")
SECTOR = 2352            # bytes of CD audio per sector (1/75 s)
CHUNK = 20               # sectors per read


class Track:
    def __init__(self, number, lba, audio):
        self.number, self.lba, self.audio, self.length = number, lba, audio, 0

    @property
    def seconds(self):
        return self.length / 75.0


class Toc:
    def __init__(self, tracks, leadout):
        self.tracks, self.leadout = tracks, leadout
        for i, t in enumerate(tracks):
            end = tracks[i + 1].lba if i + 1 < len(tracks) else leadout
            # A data session after audio tracks is separated by an 11,400-sector gap
            if i + 1 < len(tracks) and t.audio and not tracks[i + 1].audio:
                end -= 11400
            t.length = max(0, end - t.lba)

    @property
    def audio_tracks(self):
        return [t for t in self.tracks if t.audio]

    @property
    def has_data(self):
        return any(not t.audio for t in self.tracks)

    def musicbrainz_id(self):
        """MusicBrainz disc ID (audio session only, per the MB spec)."""
        audio = self.audio_tracks
        if not audio:
            return None
        first, last = audio[0].number, audio[-1].number
        leadout = self.leadout
        if self.has_data and self.tracks[-1].audio is False:
            leadout = self.tracks[len(audio)].lba - 11400
        offs = {t.number: t.lba + 150 for t in audio}
        s = "%02X%02X%08X" % (first, last, leadout + 150)
        s += "".join("%08X" % offs.get(i, 0) for i in range(1, 100))
        d = base64.b64encode(hashlib.sha1(s.encode()).digest()).decode()
        return d.replace("+", ".").replace("/", "_").replace("=", "-")


# --------------------------------------------------------------------- drives
def list_drives():
    """[{id, label, mount, media}] for every optical drive."""
    out = []
    if IS_WIN:
        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        for i in range(26):
            if not mask & (1 << i):
                continue
            root = "%s:\\" % chr(65 + i)
            if k32.GetDriveTypeW(root) != 5:      # DRIVE_CDROM
                continue
            name = ctypes.create_unicode_buffer(261)
            fs = ctypes.create_unicode_buffer(261)
            ok = k32.GetVolumeInformationW(root, name, 261, None, None, None, fs, 261)
            out.append({"id": root[:2], "label": name.value if ok else "",
                        "mount": root if ok else None, "media": bool(ok)})
    else:
        mounts = {}
        try:
            for line in open("/proc/mounts"):
                dev, mnt = line.split()[:2]
                mounts[os.path.realpath(dev)] = mnt.replace("\\040", " ")
        except OSError:
            pass
        for dev in sorted(glob.glob("/dev/sr[0-9]*")):
            mnt = mounts.get(os.path.realpath(dev))
            media = mnt is not None or _linux_has_media(dev)
            label = os.path.basename(mnt) if mnt else ""
            out.append({"id": dev, "label": label, "mount": mnt, "media": media})
    return out


def _linux_has_media(dev):
    try:
        import fcntl
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, 0x5326, 0) == 4   # CDROM_DRIVE_STATUS -> CDS_DISC_OK
        finally:
            os.close(fd)
    except Exception:
        return False


def eject(drive):
    try:
        if IS_WIN:
            mci = ctypes.windll.winmm.mciSendStringW
            mci('open %s type cdaudio alias spindle_drv' % drive, None, 0, None)
            mci('set spindle_drv door open', None, 0, None)
            mci('close spindle_drv', None, 0, None)
        else:
            subprocess.run(["eject", drive], timeout=20, capture_output=True)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ TOC read
def read_toc(drive):
    return _win_toc(drive) if IS_WIN else _linux_toc(drive)


def _win_handle(drive):
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = ctypes.c_void_p
    h = k32.CreateFileW("\\\\.\\" + drive.rstrip("\\"), 0x80000000, 3, None, 3, 0, None)
    if h in (None, ctypes.c_void_p(-1).value):
        raise OSError("Could not open drive %s" % drive)
    return h


def _win_ioctl(h, code, inbuf, outsize):
    out = ctypes.create_string_buffer(outsize)
    ret = ctypes.c_ulong(0)
    k32 = ctypes.windll.kernel32
    k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                    ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
    inp = ctypes.create_string_buffer(inbuf, len(inbuf)) if inbuf else None
    ok = k32.DeviceIoControl(h, code, inp, len(inbuf) if inbuf else 0, out, outsize, ctypes.byref(ret), None)
    if not ok:
        raise OSError("drive read failed (error %d)" % k32.GetLastError())
    return out.raw[:ret.value]


def _win_toc(drive):
    h = _win_handle(drive)
    try:
        raw = _win_ioctl(h, 0x00024000, None, 804)       # IOCTL_CDROM_READ_TOC
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
    first, last = raw[2], raw[3]
    tracks, leadout = [], 0
    for i in range(last - first + 2):
        e = raw[4 + i * 8: 12 + i * 8]
        ctrl, num = e[1] & 0x0F, e[2]
        m, s, f = e[5], e[6], e[7]
        lba = (m * 60 + s) * 75 + f - 150
        if num == 0xAA:
            leadout = lba
        else:
            tracks.append(Track(num, lba, not ctrl & 4))
    return Toc(tracks, leadout)


def _linux_toc(drive):
    import fcntl
    fd = os.open(drive, os.O_RDONLY | os.O_NONBLOCK)
    try:
        hdr = bytearray(2)
        fcntl.ioctl(fd, 0x5305, hdr)                      # CDROMREADTOCHDR
        tracks, leadout = [], 0
        for num in list(range(hdr[0], hdr[1] + 1)) + [0xAA]:
            buf = bytearray(struct.pack("BBB", num, 0, 1) + b"\0" * 9)   # format = CDROM_LBA
            fcntl.ioctl(fd, 0x5306, buf)                  # CDROMREADTOCENTRY
            ctrl = buf[1] >> 4
            lba = struct.unpack_from("<i", buf, 4)[0]
            if num == 0xAA:
                leadout = lba
            else:
                tracks.append(Track(num, lba, not ctrl & 4))
        return Toc(tracks, leadout)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- audio rip
def rip_track(drive, track, dest_wav, progress=None, retries=4, stop=None):
    """Rip one audio track to a WAV. Returns number of sectors that could not be
    recovered (filled with silence) so the UI can warn about scratches."""
    if not IS_WIN and shutil.which("cdparanoia"):
        return _rip_paranoia(drive, track, dest_wav, progress)
    reader = _WinReader(drive) if IS_WIN else _LinuxReader(drive)
    bad = 0
    try:
        with wave.open(dest_wav, "wb") as w:
            w.setnchannels(2), w.setsampwidth(2), w.setframerate(44100)
            pos, end = track.lba, track.lba + track.length
            while pos < end:
                if stop and stop():
                    raise InterruptedError("stopped")
                n = min(CHUNK, end - pos)
                data = _read_retry(reader, pos, n, retries)
                if data is None:                          # fall back to one sector at a time
                    parts = []
                    for s in range(pos, pos + n):
                        d = _read_retry(reader, s, 1, retries)
                        if d is None:
                            bad += 1
                            d = b"\0" * SECTOR
                        parts.append(d)
                    data = b"".join(parts)
                w.writeframes(data)
                pos += n
                if progress:
                    progress((pos - track.lba) / max(1, track.length))
    finally:
        reader.close()
    return bad


def _read_retry(reader, lba, n, retries):
    for _ in range(retries):
        try:
            d = reader.read(lba, n)
            if len(d) == n * SECTOR:
                return d
        except OSError:
            pass
    return None


class _WinReader:
    def __init__(self, drive):
        self.h = _win_handle(drive)

    def read(self, lba, n):
        info = struct.pack("<qII", lba * 2048, n, 2)      # RAW_READ_INFO, TrackMode = CDDA
        return _win_ioctl(self.h, 0x0002403E, info, n * SECTOR)

    def close(self):
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.h))


class _LinuxReader:
    class _Req(ctypes.Structure):
        _fields_ = [("lba", ctypes.c_int), ("fmt", ctypes.c_ubyte),
                    ("nframes", ctypes.c_int), ("buf", ctypes.c_void_p)]

    def __init__(self, drive):
        self.fd = os.open(drive, os.O_RDONLY | os.O_NONBLOCK)

    def read(self, lba, n):
        import fcntl
        buf = ctypes.create_string_buffer(n * SECTOR)
        req = self._Req(lba, 1, n, ctypes.cast(buf, ctypes.c_void_p))
        fcntl.ioctl(self.fd, 0x530E, req)                 # CDROMREADAUDIO
        return buf.raw

    def close(self):
        os.close(self.fd)


def _rip_paranoia(drive, track, dest, progress):
    p = subprocess.Popen(["cdparanoia", "-d", drive, "-e", "-w", str(track.number), dest],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="ignore")
    bad = 0
    for line in p.stderr:
        if "##: -2 [wrote]" in line and progress:
            try:
                sector = int(line.rsplit("@", 1)[1]) // 1176
                progress((sector - track.lba) / max(1, track.length))
            except Exception:
                pass
        if "[skip]" in line or "[skipped]" in line:
            bad += 1
    if p.wait() != 0:
        raise OSError("cdparanoia could not read track %d" % track.number)
    return bad
