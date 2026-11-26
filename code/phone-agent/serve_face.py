#!/usr/bin/env python3
"""The existing 8190 static service, with cache-free updates and no directory listing."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from remote_update import atomic_write

ROOT = Path(__file__).resolve().parent.parent

class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, *args):
        pass

if __name__ == '__main__':
    atomic_write(ROOT / '.body-update/face.pid', str(os.getpid()).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 8190), partial(Handler, directory=str(ROOT / 'face')))
    server.daemon_threads = True
    server.serve_forever()
