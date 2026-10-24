#!/usr/bin/env python3
"""Stand-in for llama-server: answers /health and /props on --port.

FAKE_SERVER_MODE=never  -> never listens (start timeout)
FAKE_SERVER_MODE=exit   -> exits immediately (start failure)
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

mode = os.environ.get("FAKE_SERVER_MODE", "serve")
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])

print(f"fake llama-server starting in mode {mode}", flush=True)
if mode == "exit":
    sys.exit(3)
if mode == "never":
    time.sleep(3600)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = {"status": "ok"}
        elif self.path == "/props":
            body = {"default_generation_settings": {"n_ctx": 16384}}
        else:
            self.send_error(404)
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
