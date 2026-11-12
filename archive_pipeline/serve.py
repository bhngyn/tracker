"""Serve an archive (or a package folder) over HTTP with byte-range support.

usage: python serve.py <folder> [--port 8753] [--bind 127.0.0.1]

`OPEN ME.html` works straight from the file system, so this is only for viewing over HTTP
(a preview pane, or sharing on a trusted local network with --bind 0.0.0.0). Python's
built-in `http.server` ignores Range requests, which makes browsers treat videos as
unseekable, so jump-to-timestamp silently fails there. This server answers them.
"""
import argparse, os, re
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


class RangeHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or os.path.isdir(path) or not os.path.isfile(path):
            return super().send_head()
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        size = os.path.getsize(path)
        if not m or (not m.group(1) and not m.group(2)):
            return super().send_head()
        if m.group(1):
            start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
        else:  # suffix range: last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def end_headers(self):
        if not self.headers.get("Range"):
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        try:
            while remaining > 0:
                chunk = source.read(min(1 << 16, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):  # the player cancelled a range: normal
            pass
        finally:
            self._remaining = None

    def log_message(self, fmt, *args):  # quiet: players issue many range requests
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--port", type=int, default=8753)
    ap.add_argument("--bind", default="127.0.0.1")
    a = ap.parse_args()
    httpd = ThreadingHTTPServer((a.bind, a.port), partial(RangeHandler, directory=a.folder))
    print(f"serving {a.folder} on http://{a.bind}:{a.port}/  (open 'OPEN ME.html')", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
