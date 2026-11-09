"""Standalone transport adapter mounted into existing Python observe tasks.

The optional observe stack has an independent image release. Its existing
exporter/shipper can therefore adopt ACL + mTLS during a platform upgrade
without rebuilding or changing the stack's data volumes.
"""
from __future__ import annotations

import os
import runpy
import ssl
import stat
import sys
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    origin = urllib.parse.urlsplit(os.environ["NOMAD_ADDR"])
    original = urllib.request.urlopen

    def authenticated(url, data=None, timeout=30, **kwargs):
        request = url if isinstance(url, urllib.request.Request) else urllib.request.Request(url, data=data)
        parsed = urllib.parse.urlsplit(request.full_url)
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            return original(url, data=data, timeout=timeout, **kwargs)
        fd = os.open(os.environ["NOMAD_TOKEN_FILE"], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 4096:
                raise OSError("unsafe Nomad credential file")
            token = handle.read().strip()
        if not token or any(c.isspace() for c in token):
            raise OSError("invalid Nomad credential")
        request.remove_header("X-nomad-token")
        request.add_header("X-Nomad-Token", token)
        context = ssl.create_default_context(cafile=os.environ["NOMAD_CACERT"])
        context.load_cert_chain(os.environ["NOMAD_CLIENT_CERT"], os.environ["NOMAD_CLIENT_KEY"])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context))
        return opener.open(request, timeout=timeout)

    urllib.request.urlopen = authenticated
    if len(sys.argv) < 2 or sys.argv[1] not in {"/app/nomad_exporter.py", "/app/log_shipper.py"}:
        raise SystemExit("unsupported Nomad consumer")
    script = sys.argv.pop(1)
    sys.argv[0] = script
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
