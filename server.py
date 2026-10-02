#!/usr/bin/env python3
"""Static file server for local dev with caching disabled.

Plain `python -m http.server` sends no Cache-Control header, so browsers apply
heuristic caching off Last-Modified and can keep serving a stale app.js/style.css
after an edit — surprising during active development and after any future
deploy. This just adds explicit no-store headers on every response.
"""

import http.server
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
PUBLIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        name = PUBLIC_FILES.get(unquote(urlsplit(self.path).path))
        if name is None or (ROOT / name).resolve().parent != ROOT:
            self.send_error(404, "Not found")
            return None
        return super().send_head()

    def translate_path(self, path):
        return str(ROOT / PUBLIC_FILES.get(unquote(urlsplit(path).path), "__not_public__"))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8788
    http.server.test(HandlerClass=NoCacheHandler, port=port, bind="127.0.0.1")
