"""Node-local TLS terminals, authorized over the existing Control connection."""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import ipaddress
import json
import platform
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .command_session import CommandSession


DIRECT_PORT = 17443
TICKET_SECONDS = 20


def direct_url(host: str, port: int) -> str:
    address = ipaddress.ip_address(host)
    return f"wss://{'[' + str(address) + ']' if address.version == 6 else address}:{port}/v1/terminal/direct"


def local_addresses() -> list[str]:
    """Enumerate interface addresses without an external discovery service."""
    addresses: list[str] = []
    try:
        if platform.system() == "Darwin":
            output = subprocess.check_output(["/sbin/ifconfig"], text=True, timeout=3)
            addresses = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)\b", output)
        else:
            output = subprocess.check_output(["ip", "-j", "address", "show"], text=True, timeout=3)
            addresses = [entry["local"] for item in json.loads(output)
                         for entry in item.get("addr_info", []) if entry.get("family") == "inet"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        try:
            addresses = [item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
        except OSError:
            pass
    result = []
    for host in addresses:
        address = ipaddress.ip_address(host)
        if not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
            if str(address) not in result:
                result.append(str(address))
    return result[:16]


def _server_tls(directory: Path) -> tuple[ssl.SSLContext, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Luma node terminal")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=3650)).sign(key, hashes.SHA256()))
    key_path, cert_path = directory / "key.pem", directory / "cert.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_path, key_path)
    return context, hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


class DirectTerminals:
    def __init__(self, config: dict[str, Any], *, outbound: asyncio.Queue, open_terminal: Callable):
        self.config = config.get("terminalDirect", {})
        self.outbound = outbound
        self.open_terminal = open_terminal
        self.server = None
        self.directory = None
        self.fingerprint = ""
        self.urls: list[str] = []
        self.port = DIRECT_PORT
        self.pending: dict[str, dict[str, Any]] = {}
        self.active: dict[str, Any] = {}
        self.connections: set[Any] = set()
        self.expirations: dict[str, asyncio.TimerHandle] = {}
        self.pending_auth = 0

    async def start(self) -> None:
        from websockets.asyncio.server import serve

        if self.config.get("enabled", True) is False:
            return
        self.port = int(self.config.get("port", DIRECT_PORT))
        host = str(self.config.get("listenHost", "0.0.0.0"))
        self.directory = tempfile.TemporaryDirectory(prefix="luma-terminal-tls-")
        try:
            tls, self.fingerprint = await asyncio.to_thread(_server_tls, Path(self.directory.name))
            self.server = await serve(self.connect, host, self.port, ssl=tls, open_timeout=3,
                                      close_timeout=2, ping_interval=20, ping_timeout=20,
                                      max_size=65536, max_queue=16)
            self.port = self.server.sockets[0].getsockname()[1]
            configured = self.config.get("advertiseUrls", [])
            if not isinstance(configured, list):
                raise ValueError("terminalDirect.advertiseUrls must be a list")
            self.urls = [self._checked_url(url) for url in configured][:16]
            addresses = await asyncio.to_thread(local_addresses) if host in {"0.0.0.0", "::"} else [host]
            # The default listener is IPv4. IPv6 is available with an explicit listenHost.
            self.urls.extend(direct_url(address, self.port) for address in addresses)
            self.urls = list(dict.fromkeys(self.urls))[:16]
        except BaseException:
            await self.stop()
            raise

    @staticmethod
    def _checked_url(value: Any) -> str:
        parsed = urlsplit(str(value))
        if (parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path != "/v1/terminal/direct"):
            raise ValueError("direct terminal URL must be wss://host:port/v1/terminal/direct")
        _ = parsed.port
        return str(value)

    def handle(self, message: dict[str, Any]) -> bool:
        kind, sid = message.get("type"), str(message.get("sessionId") or "")
        if kind == "open" and message.get("transport") in {"auto", "direct"}:
            urls = list(self.urls)
            for host in message.get("directHosts", [])[:8]:
                with contextlib.suppress(ValueError):
                    urls.append(direct_url(host, self.port))
            if not self.server or not urls:
                if message["transport"] == "direct":
                    self.outbound.put_nowait({"type": "error", "sessionId": sid,
                                             "message": "direct terminal listener is unavailable on this node"})
                    return True
                self.outbound.put_nowait({"type": "relay-ready", "sessionId": sid})
                self.open_terminal(message, self.outbound)
                return True
            if sid in self.pending or sid in self.active:
                return True
            token = secrets.token_urlsafe(32)
            self.pending[sid] = {"token": token, "expires": time.monotonic() + TICKET_SECONDS,
                                 "message": dict(message)}
            self.expirations[sid] = asyncio.get_running_loop().call_later(TICKET_SECONDS, self._expire, sid)
            self.outbound.put_nowait({"type": "direct-offer", "sessionId": sid, "ticket": token,
                                     "fingerprint": self.fingerprint, "urls": list(dict.fromkeys(urls))[:16]})
            return True
        if kind == "relay":
            pending = self._pop(sid)
            if pending and pending["message"].get("transport") == "auto":
                self.outbound.put_nowait({"type": "relay-ready", "sessionId": sid})
                self.open_terminal(pending["message"], self.outbound)
            else:
                self.outbound.put_nowait({"type": "error", "sessionId": sid,
                                         "message": "terminal route already selected or ticket expired; reconnect"})
            return True
        if kind == "close":
            self._pop(sid)
            connection = self.active.get(sid)
            if connection:
                asyncio.create_task(connection.close())
        return False

    def _pop(self, sid: str) -> dict[str, Any] | None:
        timer = self.expirations.pop(sid, None)
        if timer:
            timer.cancel()
        return self.pending.pop(sid, None)

    def _expire(self, sid: str) -> None:
        if self._pop(sid):
            self.outbound.put_nowait({"type": "error", "sessionId": sid,
                                     "message": "direct terminal ticket expired; reconnect"})

    async def connect(self, websocket: Any) -> None:
        if websocket.request.path != "/v1/terminal/direct" or self.pending_auth >= 16:
            await websocket.close(code=1008)
            return
        self.connections.add(websocket)
        self.pending_auth += 1
        sid = ""
        terminal = None
        sender = None
        try:
            try:
                auth = json.loads(await asyncio.wait_for(websocket.recv(), 3))
                candidate = str(auth.get("sessionId") or "")
                pending = self.pending.get(candidate)
                if (auth.get("type") != "auth" or not pending or pending["expires"] <= time.monotonic()
                        or not secrets.compare_digest(str(auth.get("ticket") or ""), pending["token"])):
                    await websocket.close(code=1008)
                    return
                # Consume only after verification, before any await or process creation.
                self._pop(candidate)
                sid = candidate
                self.active[sid] = websocket
            finally:
                self.pending_auth -= 1
            output: asyncio.Queue = asyncio.Queue()
            await websocket.send(json.dumps({"type": "open", "sessionId": sid, "transport": "direct"}))
            terminal = self.open_terminal(pending["message"], output)
            last_activity = 0.0

            def touch():
                nonlocal last_activity
                if time.monotonic() - last_activity >= 10:
                    self.outbound.put_nowait({"type": "activity", "sessionId": sid})
                    last_activity = time.monotonic()

            async def send_output():
                while True:
                    message = await output.get()
                    await websocket.send(json.dumps(message, separators=(",", ":")))
                    touch()
                    if message.get("type") in {"exit", "error"}:
                        await websocket.close()
                        return

            sender = asyncio.create_task(send_output())
            async for raw in websocket:
                message = json.loads(raw)
                if isinstance(terminal, CommandSession) and message.get("type") in {"stdin", "stdin-eof", "exec-ack"}:
                    terminal.receive(message)
                    touch()
                elif message.get("type") == "input":
                    terminal.write(str(message.get("data") or ""))
                    touch()
                elif message.get("type") == "resize":
                    terminal.resize(rows=int(message.get("rows") or 32), cols=int(message.get("cols") or 120))
                elif message.get("type") == "close":
                    break
        except Exception as exc:
            with contextlib.suppress(Exception):
                if sid:
                    await websocket.send(json.dumps({"type": "error", "message": str(exc)}))
                await websocket.close(code=1011)
        finally:
            if sender:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            if terminal:
                await asyncio.to_thread(terminal.close)
                if isinstance(terminal, CommandSession):
                    await terminal.wait_closed()
            if sid:
                self.active.pop(sid, None)
                self.outbound.put_nowait({"type": "close", "sessionId": sid})
            self.connections.discard(websocket)

    async def reset(self) -> None:
        for sid in list(self.pending):
            self._pop(sid)
        await asyncio.gather(*(ws.close() for ws in list(self.connections)), return_exceptions=True)

    async def stop(self) -> None:
        await self.reset()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None
