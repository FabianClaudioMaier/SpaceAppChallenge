"""Serve the frontend and expose the local middleware API on one origin."""

import json
import os
import shutil
import sys
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import traceback

from middleware import BackendError, main


FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
VIDEO_CACHE_DIR = Path(__file__).resolve().parent / "video_cache"


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

    def send_video(self, head_only=False):
        path = urlsplit(self.path).path
        prefix = "/api/videos/"
        if not path.startswith(prefix):
            return False

        name = path[len(prefix):]
        try:
            # <uuid>.gif (frames) or <uuid>_diff.gif (difference images)
            uuid = str(UUID(name.removesuffix(".gif").removesuffix("_diff")))
            if name not in (f"{uuid}.gif", f"{uuid}_diff.gif"):
                raise ValueError("Invalid video name")
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return True

        video_path = VIDEO_CACHE_DIR / name
        try:
            with video_path.open("rb") as video:
                os.utime(video.fileno(), None)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/gif")
                self.send_header("Content-Length", str(os.fstat(video.fileno()).st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if not head_only:
                    shutil.copyfileobj(video, self.wfile)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
        return True

    def do_GET(self):
        if not self.send_video():
            super().do_GET()

    def do_HEAD(self):
        if not self.send_video(head_only=True):
            super().do_HEAD()

    def do_POST(self):
        if self.path != "/api/send-to-backend":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid request size")

            payload = json.loads(self.rfile.read(length))
            DEC = payload["DEC"]
            RA = payload["RA"]
            PIXELS = int(payload["PIXELS"])
            MS = int(payload["MS"])

            if not isinstance(DEC, str) or not isinstance(RA, str):
                raise ValueError("Coordinates must be strings")
            if not 50 <= PIXELS <= 500 or not 25 <= MS <= 1000:
                raise ValueError("Pixel Size or Speed is outside its allowed range")

            uuid, json = main(DEC, RA, PIXELS, MS)
        except BackendError as error:
            # The backend's own explanation, e.g. "DEC must be in [-90, 90] degrees, got 91"
            status = {
                1: HTTPStatus.BAD_REQUEST,   # invalid coordinates / input
                2: HTTPStatus.NOT_FOUND,     # no SPHEREx images cover that position
                3: HTTPStatus.BAD_GATEWAY,   # NASA archive unreachable
            }.get(error.exit_code, HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send_json(status, {"error": str(error)})
            return
        except (KeyError, TypeError, ValueError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except Exception as error:
            # Anything unexpected: answer anyway, so the page shows a message
            # instead of "Failed to fetch"
            traceback.print_exc()
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR,
                           {"error": f"Internal error: {type(error).__name__}: {error}"})
            return

        self.send_json(HTTPStatus.OK, {"uuid": uuid})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    print(f"Serving frontend and middleware at http://127.0.0.1:{port}/", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
