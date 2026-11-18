"""Interrupt runner long polls without abandoning a live socket in a thread."""
from __future__ import annotations

import contextlib
import http.client
import socket
import ssl
import threading
import urllib.request


@contextlib.contextmanager
def interruptible_urlopen(request, *, timeout, stop_event, handlers=(), insecure=False):
    sockets = []
    lock = threading.Lock()
    finished = threading.Event()

    def shutdown(sock):
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)

    def track(sock):
        with lock:
            sockets.append(sock)
        if stop_event.is_set():
            shutdown(sock)

    class HTTPConnection(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            track(self.sock)

    class HTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            track(self.sock)

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(HTTPConnection, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(HTTPSConnection, req, context=self._context)

    def cancel_on_stop():
        while not finished.wait(0.05):
            if stop_event.is_set():
                with lock:
                    current = list(sockets)
                for sock in current:
                    shutdown(sock)
                return

    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    custom = [h for h in handlers if not isinstance(h, (urllib.request.HTTPHandler, urllib.request.HTTPSHandler))]
    opener = urllib.request.build_opener(*custom, HTTPHandler(), HTTPSHandler(context=context))
    watcher = threading.Thread(target=cancel_on_stop, daemon=True)
    watcher.start()
    try:
        with opener.open(request, timeout=timeout) as response:
            yield response
    finally:
        finished.set()
        watcher.join(timeout=0.2)
