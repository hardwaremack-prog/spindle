"""Tiny local web server for Spindle's dashboard (no extra packages needed)."""
import argparse
import json
import mimetypes
import os
import string
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import smart
from .engine import Engine

UI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
ENGINE = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(raw or b"{}")
        return raw

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._file(os.path.join(UI, "index.html"))
            if u.path.startswith("/ui/"):
                p = os.path.normpath(os.path.join(UI, u.path[4:]))
                if p.startswith(UI):
                    return self._file(p)
            if u.path == "/api/state":
                return self._send(200, ENGINE.state())
            if u.path == "/api/art":
                g = ENGINE._g(q["g"])
                if g["art"] and os.path.exists(g["art"]):
                    return self._send(200, open(g["art"], "rb").read(), "image/jpeg")
                return self._send(404, {"error": "no art"})
            if u.path == "/api/art/search":
                return self._send(200, {"results": smart.search_art(q.get("q", ""))})
            if u.path == "/api/browse":
                return self._send(200, browse(q.get("path", "")))
            if u.path == "/api/preview":          # play a track in the browser
                g = ENGINE._g(q["g"])
                t = next(t for t in g["tracks"] if t["id"] == q["t"])
                p = t.get("out") or t.get("rip") or t.get("src")
                if p and os.path.exists(p):
                    ct = mimetypes.guess_type(p)[0] or "audio/mpeg"
                    return self._send(200, open(p, "rb").read(), ct)
                return self._send(404, {"error": "Not ripped yet"})
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(400, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/scan":
                ENGINE.scan(b["source"])
            elif u.path == "/api/rip":
                ENGINE.rip()
            elif u.path == "/api/identify":
                ENGINE.identify()
            elif u.path == "/api/finish":
                ENGINE.finish()
            elif u.path == "/api/auto":            # scan + rip + identify (+ finish if confident)
                ENGINE.run("Auto", ENGINE._auto, b["source"])
            elif u.path == "/api/stop":
                ENGINE.stop()
            elif u.path == "/api/edit":
                ENGINE.edit(b)
            elif u.path == "/api/settings":
                ENGINE.save_settings(b)
            elif u.path == "/api/eject":
                from . import cdrom
                cdrom.eject(b["drive"])
            elif u.path == "/api/clear":
                ENGINE.session = None
            elif u.path == "/api/art/url":
                img = smart._get(b["url"], binary=True, timeout=20)
                if not img:
                    raise RuntimeError("Couldn't download that image")
                ENGINE.set_art(b["g"], img)
            elif u.path == "/api/art/upload":
                gid = parse_qs(u.query)["g"][0]
                ENGINE.set_art(gid, b if isinstance(b, bytes) else b"", "Your image")
            elif u.path == "/api/open":
                open_folder(b["path"])
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, {"ok": True})
        except Exception as e:
            self._send(400, {"error": str(e)})

    def _file(self, p):
        if not os.path.isfile(p):
            return self._send(404, {"error": "missing"})
        self._send(200, open(p, "rb").read(), mimetypes.guess_type(p)[0] or "text/plain")


def browse(path):
    if not path:
        if sys.platform.startswith("win"):
            roots = ["%s:\\" % c for c in string.ascii_uppercase if os.path.exists("%s:\\" % c)]
        else:
            roots = [os.path.expanduser("~"), "/media", "/mnt", "/"]
        return {"path": "", "parent": None, "dirs": [{"name": r, "path": r} for r in roots if os.path.isdir(r)]}
    path = os.path.abspath(path)
    dirs = []
    try:
        for e in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if e.is_dir() and not e.name.startswith((".", "$")):
                dirs.append({"name": e.name, "path": e.path})
    except OSError:
        pass
    parent = os.path.dirname(path.rstrip("/\\"))
    return {"path": path, "parent": parent if parent and parent != path else "", "dirs": dirs}


def open_folder(p):
    import subprocess
    if sys.platform.startswith("win"):
        os.startfile(p)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def main():
    global ENGINE
    ap = argparse.ArgumentParser(description="Spindle - smart CD ripper")
    ap.add_argument("--port", type=int, default=8484)
    ap.add_argument("--lan", action="store_true", help="let other computers on your network open it")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    ENGINE = Engine()
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    srv = ThreadingHTTPServer((host, a.port), Handler)
    url = "http://127.0.0.1:%d/" % a.port
    print("Spindle is running at %s  (Ctrl+C to quit)" % url)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
