"""Serve the frontend and expose the local middleware API on one origin."""

import json
import math
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from middleware import sendToBackend


FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/send-to-backend":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request size")

            payload = json.loads(self.rfile.read(length))
            DEC = float(payload["DEC"])
            RA = float(payload["RA"])
            PIXELS = int(payload["PIXELS"])
            MS = int(payload["MS"])

            if not math.isfinite(DEC) or not math.isfinite(RA):
                raise ValueError("Coordinates must be finite numbers")
            if not 50 <= PIXELS <= 500 or not 25 <= MS <= 1000:
                raise ValueError("Pixel Size or Speed is outside its allowed range")

            uuid = sendToBackend(DEC, RA, PIXELS, MS)
        except (KeyError, TypeError, ValueError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        self.send_json(HTTPStatus.OK, {"uuid": uuid})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    print(f"Serving frontend and middleware at http://127.0.0.1:{port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
