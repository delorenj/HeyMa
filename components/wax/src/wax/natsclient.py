"""Minimal NATS client over raw TCP — stdlib only.

waxd runs on /usr/bin/python3 with no venv (that is deliberate: gi/evdev are
only available there), so `pip install nats-py` is not on the table. The NATS
wire protocol is line-based and small enough to speak directly.

Publishes use request/reply so we get the JetStream **PubAck**, not just "the
socket accepted our bytes". That distinction is the whole point of the outbox:
a fire-and-forget publish that silently dropped would mark events delivered
when they never landed.
"""

import json
import os
import socket
from typing import Any, Optional

HOST = os.environ.get("WAX_NATS_HOST", "127.0.0.1")
PORT = int(os.environ.get("WAX_NATS_PORT", "4222"))
CONNECT_TIMEOUT = 5.0
ACK_TIMEOUT = 10.0


class NatsError(RuntimeError):
    pass


class Nats:
    def __init__(self, host: str = HOST, port: int = PORT):
        self.host, self.port = host, port
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self._sid = 0

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        s = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        s.settimeout(ACK_TIMEOUT)
        self.sock, self.buf = s, b""
        info = self._readline()
        if not info.startswith(b"INFO"):
            raise NatsError(f"expected INFO, got {info[:60]!r}")
        self._send(b'CONNECT {"verbose":false,"pedantic":false,"name":"wax"}\r\n')
        # A PING/PONG round-trip proves the connection is actually usable
        # before we start counting on acks.
        self._send(b"PING\r\n")
        line = self._readline()
        while line.startswith(b"INFO") or line == b"":
            line = self._readline()
        if not line.startswith(b"PONG"):
            raise NatsError(f"handshake failed: {line[:60]!r}")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *a):
        self.close()

    # -- io --------------------------------------------------------------
    def _send(self, data: bytes) -> None:
        if self.sock is None:
            raise NatsError("not connected")
        self.sock.sendall(data)

    def _readline(self) -> bytes:
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise NatsError("connection closed by server")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\r\n")
        return line

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise NatsError("connection closed mid-payload")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    # -- publish ---------------------------------------------------------
    def publish_ack(self, subject: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish and WAIT for the JetStream PubAck. Raises if none arrives."""
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        inbox = f"_INBOX.wax.{os.urandom(8).hex()}"
        self._sid += 1
        sid = self._sid
        self._send(f"SUB {inbox} {sid}\r\n".encode())
        self._send(f"PUB {subject} {inbox} {len(body)}\r\n".encode() + body + b"\r\n")

        try:
            while True:
                line = self._readline()
                if line.startswith(b"PING"):
                    self._send(b"PONG\r\n")
                    continue
                if line.startswith(b"-ERR"):
                    raise NatsError(line.decode(errors="replace"))
                if line.startswith(b"MSG"):
                    parts = line.split()
                    n = int(parts[-1])
                    data = self._read_exact(n + 2)[:-2]
                    try:
                        ack = json.loads(data or b"{}")
                    except json.JSONDecodeError:
                        ack = {"raw": data.decode(errors="replace")}
                    if "error" in ack:
                        raise NatsError(f"jetstream refused: {ack['error']}")
                    return ack
        except socket.timeout as e:
            raise NatsError(
                f"no PubAck for {subject} within {ACK_TIMEOUT}s — "
                "treating as UNDELIVERED (the outbox row stays unpublished)"
            ) from e
        finally:
            try:
                self._send(f"UNSUB {sid}\r\n".encode())
            except Exception:  # noqa: BLE001
                pass


def publish(subject: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """One-shot connect/publish/close. Raises NatsError on any doubt."""
    with Nats() as n:
        return n.publish_ack(subject, envelope)


def reachable() -> bool:
    try:
        with Nats():
            return True
    except (OSError, NatsError):
        return False
