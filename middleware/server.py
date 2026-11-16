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

from middleware import BackendError, catalogLayer, catalogList, downloadCSV, exportView, main, save, validateJob

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
VIDEO_CACHE_DIR = Path(__file__).resolve().parent / "video_cache"


class Server(ThreadingHTTPServer):
    # Connections waiting to be accepted. The default (5) reset connections as soon
    # as ~8 requests came at once - e.g. a page load fetching many catalogue layers,
    # or Saves (measured: 2 of 10 parallel Saves lost).
    request_queue_size = 128


class Handler(SimpleHTTPRequestHandler):
    # A connection silent this long (request or body never finished) is closed,
    # instead of keeping its thread forever. Only waiting for the client counts:
    # a long job doesn't.
    timeout = 60

    # Set in __main__ from the port: the only Host headers answered.
    # Other pages open in the same browser can't use this server:
    #  - DNS rebinding (a foreign domain pointed at 127.0.0.1) sends a foreign Host
    #  - a POST from a foreign page carries its Origin, and can't send
    #    Content-Type: application/json without the browser asking first (CORS),
    #    which this server never allows
    allowed_hosts = set()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def host_ok(self):
        if self.headers.get("Host") in self.allowed_hosts:
            return True
        self.send_error(HTTPStatus.FORBIDDEN, "Unknown Host")
        return False

    def post_ok(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in self.allowed_hosts}:
            self.send_error(HTTPStatus.FORBIDDEN, "Foreign Origin")
            return False
        if self.headers.get_content_type() != "application/json":
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected application/json")
            return False
        return True

    def send_saves_csv(self):
        if urlsplit(self.path).path != "/api/saves.csv":
            return False
        data = downloadCSV()
        if data is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Nothing saved yet")
            return True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="saves.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)
        return True

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
        if not self.host_ok():
            return
        if urlsplit(self.path).path == "/api/catalogs":
            self.send_json(HTTPStatus.OK, catalogList())
            return
        if not self.send_video() and not self.send_saves_csv():
            super().do_GET()

    def do_HEAD(self):
        if not self.host_ok():
            return
        if not self.send_video(head_only=True):
            super().do_HEAD()

    def do_POST(self):
        if not self.host_ok() or not self.post_ok():
            return
        if self.path not in ("/api/send-to-backend", "/api/save", "/api/catalog", "/api/export"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            # /api/export: every shown star's position per frame (10,000 stars x 20 frames ~ 3 MB)
            # /api/save: with the coordinate grid's lines (a few 100 kB)
            max_length = {"/api/save": 2_000_000, "/api/catalog": 65536, "/api/export": 32_000_000}.get(self.path, 4096)
            if not 0 < length <= max_length:
                raise ValueError("Invalid request size")

            payload = json.loads(self.rfile.read(length))
            if self.path == "/api/save":
                self.send_json(HTTPStatus.OK, save(payload))  # save() checks the request
                return
            if self.path == "/api/export":
                try:
                    name, data = exportView(payload)  # (checks the request)
                except FileNotFoundError as error:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/gif")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/api/catalog":
                try:
                    layer = catalogLayer(payload)  # (checks the request)
                except ValueError:
                    raise
                except Exception as error:  # catalogue servers unreachable etc.
                    self.send_json(HTTPStatus.BAD_GATEWAY, {"error": f"{type(error).__name__}: {error}"})
                    return
                self.send_json(HTTPStatus.OK, layer)
                return

            uuid, metadata = main(*validateJob(payload))
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

        self.send_json(HTTPStatus.OK, {"uuid": uuid, "json": metadata})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    Handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    import export
    export.warm_up()  # the export worker processes (export.py), before the first "Download GIF"
    print(f"Serving frontend and middleware at http://127.0.0.1:{port}/", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
