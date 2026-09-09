#!/usr/bin/env python3
"""Expose loopback VictoriaMetrics on Nomad/Docker bridge IPs only.

Control runs in Nomad bridge mode, so it cannot scrape 127.0.0.1:8428.
Binding 0.0.0.0 or eth0/Tailscale would put metrics on a routable NIC.
This proxy keeps the public and Tailscale addresses closed.
"""
from __future__ import annotations

import fcntl
import ipaddress
import os
import selectors
import socket
import struct
import sys
import threading
import time
from typing import Iterable

UPSTREAM_HOST = os.environ.get("LUMA_OBSERVE_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.environ.get("LUMA_OBSERVE_UPSTREAM_PORT", "8428"))
LISTEN_PORT = int(os.environ.get("LUMA_OBSERVE_PORT", "8428"))
IFACES = tuple(
    name.strip()
    for name in os.environ.get("LUMA_OBSERVE_GATEWAY_IFACES", "nomad,docker0").split(",")
    if name.strip()
)


def ipv4_of_interface(name: str) -> str | None:
    if not name or any(character in name for character in ("\0", "/", " ")):
        return None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            encoded = struct.pack("256s", name.encode("ascii")[:15])
            raw = fcntl.ioctl(sock.fileno(), 0x8915, encoded)  # SIOCGIFADDR
        finally:
            sock.close()
    except (OSError, UnicodeEncodeError, struct.error):
        return None
    candidate = socket.inet_ntoa(raw[20:24])
    try:
        parsed = ipaddress.IPv4Address(candidate)
    except ipaddress.AddressValueError:
        return None
    if parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast:
        return None
    return str(parsed)


def gateway_bind_ips(names: Iterable[str] = IFACES) -> list[str]:
    addresses: list[str] = []
    seen: set[str] = set()
    for name in names:
        address = ipv4_of_interface(name)
        if not address or address in seen:
            continue
        try:
            parsed = ipaddress.IPv4Address(address)
        except ipaddress.AddressValueError:
            continue
        if parsed.is_loopback or parsed.is_unspecified or parsed.is_multicast or parsed.is_link_local:
            continue
        seen.add(address)
        addresses.append(address)
    return addresses


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket) -> None:
    upstream = None
    try:
        upstream = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=3)
        upstream.settimeout(None)
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        _pipe(upstream, client)
    except OSError:
        pass
    finally:
        for sock in (client, upstream):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass


def _serve(bind_ip: str, stop: threading.Event) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((bind_ip, LISTEN_PORT))
        server.listen(128)
        server.setblocking(False)
    except OSError as exc:
        print(f"luma-observe host-gateway bind failed {bind_ip}:{LISTEN_PORT}: {exc}", file=sys.stderr, flush=True)
        server.close()
        return
    print(f"luma-observe host-gateway {bind_ip}:{LISTEN_PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}", flush=True)
    selector = selectors.DefaultSelector()
    selector.register(server, selectors.EVENT_READ)
    try:
        while not stop.is_set():
            for key, _ in selector.select(timeout=1.0):
                if key.fileobj is not server:
                    continue
                try:
                    client, _addr = server.accept()
                except OSError:
                    continue
                client.settimeout(None)
                threading.Thread(target=_handle, args=(client,), daemon=True).start()
    finally:
        selector.close()
        server.close()


def main() -> int:
    stop = threading.Event()
    workers: dict[str, threading.Thread] = {}
    try:
        while True:
            wanted = set(gateway_bind_ips())
            for address, thread in list(workers.items()):
                if address not in wanted or not thread.is_alive():
                    workers.pop(address, None)
            for address in wanted:
                thread = workers.get(address)
                if thread is not None and thread.is_alive():
                    continue
                worker_stop = threading.Event()
                thread = threading.Thread(target=_serve, args=(address, worker_stop), daemon=True)
                thread.start()
                workers[address] = thread
            if not workers:
                print("luma-observe host-gateway waiting for nomad/docker0", file=sys.stderr, flush=True)
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
