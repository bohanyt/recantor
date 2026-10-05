from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HOST = "0.0.0.0"
_PORT = 8787
_EXPECTED_AUTH = "Bearer alpha-proof-key"
_MAX_BODY_BYTES = 16 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "RecantorAlphaProof/1"

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/openai/v1/audio/transcriptions":
            self._json(404, {"error": "not_found"})
            return
        if self.headers.get("authorization") != _EXPECTED_AUTH:
            self._json(401, {"error": "unauthorized"})
            return

        content_type = self.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            self._json(400, {"error": "multipart_required"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid_length"})
            return
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._json(413, {"error": "invalid_body_size"})
            return

        body = self.rfile.read(length)
        if b'name="model"' not in body or b'name="file"' not in body:
            self._json(400, {"error": "missing_form_fields"})
            return

        self._json(200, {"text": "alpha proof transcript", "language": "en"})

    def log_message(self, format: str, *args: object) -> None:
        # Do not log request headers/bodies. The endpoint exists only for deterministic CI proof.
        return


if __name__ == "__main__":
    ThreadingHTTPServer((_HOST, _PORT), Handler).serve_forever()
