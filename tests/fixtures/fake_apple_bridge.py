#!/usr/bin/env python3
"""Stand-in for the Swift `jobsmith-apple-ai` sidecar, used by
tests/test_apple_bridge.py. Speaks the same handshake and the same three
endpoints, so the supervisor is exercised against a real child process (spawn,
crash, restart, terminate) without needing Apple Silicon or macOS 26.

Behaviour is driven by env vars so the spawner's argv stays exactly the real
one (`--port 0`):
  FAKE_BRIDGE_MODE      ok (default) | silent | exit | garbage
  FAKE_BRIDGE_AVAILABLE 1 (default) | 0   — what /health reports
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("FAKE_BRIDGE_MODE", "ok")
AVAILABLE = os.environ.get("FAKE_BRIDGE_AVAILABLE", "1") == "1"
REASON = None if AVAILABLE else "Apple Intelligence is not turned on in Settings"
MODEL_ID = "apple-on-device"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True, "available": AVAILABLE, "reason": REASON})
        elif self.path in ("/v1/models", "/models"):
            data = [{"id": MODEL_ID, "object": "model", "created": 0, "owned_by": "apple"}] if AVAILABLE else []
            self._send(200, {"object": "list", "data": data})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._send(200, {
            "id": "chatcmpl-fake", "object": "chat.completion", "created": 0,
            "model": MODEL_ID,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "fake on-device reply"}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

    def log_message(self, *args):  # keep pytest output clean
        pass


def main():
    if MODE == "exit":
        sys.exit(3)
    if MODE == "silent":
        time.sleep(60)  # never prints a handshake
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if MODE == "garbage":
        print("this is not the handshake", flush=True)
    else:
        print(json.dumps({"port": server.server_address[1]}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
