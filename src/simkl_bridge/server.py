"""HTTP routes: /sonarr/{list_id}, /radarr/{list_id}, /healthz."""
import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import Forbidden, WrongTarget
from .simkl import ListNotFound, SimklError

ROUTE = re.compile(r"^/(sonarr|radarr)/([0-9]+)$")

# Most specific first: ListNotFound is a SimklError.
ERRORS = [(ListNotFound, 404), (Forbidden, 403), (WrongTarget, 400), (SimklError, 502)]


def make_server(service, host, port):
    class Handler(BaseHTTPRequestHandler):
        server_version = "simkl-bridge"

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, {"status": "ok"})
            m = ROUTE.match(path)
            if not m:
                return self._send(404, {"error": "unknown route; use /sonarr/<list_id> or /radarr/<list_id>"})
            try:
                return self._send(200, service.feed(m.group(1), int(m.group(2))))
            except Exception as e:  # noqa: BLE001 -- every failure must become a non-200
                for kind, status in ERRORS:
                    if isinstance(e, kind):
                        return self._send(status, {"error": str(e)})
                traceback.print_exc(file=sys.stderr)
                return self._send(500, {"error": "internal error; see the arrs log"})

        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{self.address_string()} {fmt % args}\n")

    return ThreadingHTTPServer((host, port), Handler)
