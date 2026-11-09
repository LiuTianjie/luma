"""Read a scoped Nomad credential on each request so rotation is observable."""
from __future__ import annotations

import os
import ssl
import stat
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_nomad(request: urllib.request.Request, *, token: str = "", timeout: float = 4):
    path = os.environ.get("NOMAD_TOKEN_FILE")
    if path:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
                raise OSError("Nomad reader credential must be a small, private regular file")
            token = handle.read().strip()
        if not token or any(c.isspace() for c in token):
            raise OSError("Nomad reader credential is empty or invalid")
    if token:
        request.add_header("X-Nomad-Token", token)
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if request.full_url.startswith("https://"):
        context = ssl.create_default_context(cafile=os.environ.get("NOMAD_CACERT") or None)
        cert, key = os.environ.get("NOMAD_CLIENT_CERT"), os.environ.get("NOMAD_CLIENT_KEY")
        if bool(cert) != bool(key):
            raise OSError("Nomad TLS requires both certificate and key")
        if cert and key:
            context.load_cert_chain(cert, key)
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)
