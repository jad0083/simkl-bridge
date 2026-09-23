"""The one place that touches the network.

Everything else takes a `transport(method, url, headers, data, timeout)`
callable, so tests can substitute a scripted Simkl.
"""
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        try:
            return json.loads(self.body or b"null")
        except ValueError:
            return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would forward Authorization to whatever host a redirect names."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return Response(r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, dict(e.headers or {}), e.read())


def write_private(path, text):
    """Write `text` to `path` as 0600, atomically, never world-readable in between."""
    path = os.fspath(path)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
