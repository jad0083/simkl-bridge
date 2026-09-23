"""python -m simkl_bridge serve | auth"""
import os
import pathlib
import sys

from .auth_cli import DeviceFlowError, device_flow
from .resolve import IdCache, Resolver
from .server import make_server
from .service import ListService
from .simkl import Simkl
from .tokens import TokenStore


def _env(name, default=None):
    value = os.environ.get(name, default)
    if value in (None, ""):
        sys.exit(f"{name} is required")
    return value


def serve():
    data = pathlib.Path(os.environ.get("BRIDGE_DATA_DIR", "/data"))
    data.mkdir(parents=True, exist_ok=True)
    client_id = _env("SIMKL_CLIENT_ID")
    tokens = TokenStore(data / "token.json", refresh_token=_env("SIMKL_REFRESH_TOKEN"),
                        client_id=client_id,
                        client_secret=os.environ.get("SIMKL_CLIENT_SECRET") or None)
    simkl = Simkl(client_id, tokens)
    allowed = os.environ.get("BRIDGE_LISTS", "").replace(",", " ").split()
    service = ListService(simkl, Resolver(simkl, IdCache(data / "ids.json")),
                          min_refresh=int(os.environ.get("BRIDGE_MIN_REFRESH", "900")),
                          allowed={int(x) for x in allowed} or None)
    port = int(os.environ.get("BRIDGE_PORT", "8080"))
    server = make_server(service, "0.0.0.0", port)
    print(f"simkl-bridge listening on :{port}", file=sys.stderr, flush=True)
    server.serve_forever()


def auth():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    out = pathlib.Path(runtime if os.path.isdir(runtime) else ".") / "simkl-refresh-token.secret"
    try:
        device_flow(_env("SIMKL_CLIENT_ID"), out)
    except DeviceFlowError as e:
        sys.exit(str(e))


def main(argv=None):
    cmd = (argv or sys.argv[1:] or ["serve"])[0]
    {"serve": serve, "auth": auth}.get(cmd, lambda: sys.exit("usage: python -m simkl_bridge serve|auth"))()


if __name__ == "__main__":
    main()
