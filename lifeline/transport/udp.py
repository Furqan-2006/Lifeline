"""UDP transport (contract C2.1) for nodes running inside Mininet-WiFi.

One process per station. Exactly one addressing mode:
  - broadcast_addr: one datagram to (broadcast_addr, port). Emulation.
  - peers: one datagram to each (host, port). Loopback tests on any OS.

Usage::

    t = UdpTransport("0.0.0.0", 5005, SystemClock(), Rng(seed).child("n07"),
                     broadcast_addr="10.0.0.255", own_addrs=("10.0.0.7",))
    await t.start()      # TransportBindError is raised here, not later

The transport never logs C3 events. It exposes ``counters`` instead.
"""

from __future__ import annotations

import asyncio
import socket
from collections import deque
from collections.abc import Sequence

from lifeline.clock import Clock
from lifeline.runtime.rng import Rng
from lifeline.transport.base import Transport, TransportClosed

COUNTER_KEYS = (
    "sent",
    "send_errors",
    "send_backpressure",
    "oversized_tx",
    "received",
    "own_ignored",
    "oversized_rx",
    "lost_to_loss",
    "dropped_full",
    "delivered",
    "socket_errors",
)

# Contract question 5: asyncio buffers unsent datagrams without bound.
_WRITE_BUFFER_LIMIT = 256 * 1024


class TransportBindError(RuntimeError):
    """Raised by ``start()`` when the socket cannot be bound. A setup error."""


class UdpTransport(Transport):
    def __init__(
        self,
        bind_addr: str,
        port: int,
        clock: Clock,
        rng: Rng,
        broadcast_addr: str | None = None,
        peers: Sequence[tuple[str, int]] | None = None,
        own_addrs: Sequence[str] = (),
        receiver_loss: float = 0.0,
        max_frame: int = 1500,
        queue_limit: int = 1000,
    ) -> None:
        if (broadcast_addr is None) == (peers is None):
            raise ValueError("give exactly one of broadcast_addr or peers")
        if not 0 <= port <= 65535:
            raise ValueError("port must be in [0, 65535]")
        if not 0.0 <= receiver_loss <= 1.0:
            raise ValueError("receiver_loss must be in [0, 1]")
        if max_frame < 1 or queue_limit < 1:
            raise ValueError("max_frame and queue_limit must be at least 1")

        self._clock = clock  # reserved: the transport reads no time
        self._bind_addr = bind_addr
        self._port = port
        self._broadcast_addr = broadcast_addr
        self._peers: tuple[tuple[str, int], ...] = tuple(
            (str(host), int(p)) for host, p in (peers or ())
        )
        self._own_addrs = frozenset(own_addrs)
        self._loss = receiver_loss
        self._loss_rng = rng.child("udp-loss")
        self._max_frame = max_frame
        self._queue_limit = queue_limit
        self._queue: deque[tuple[bytes, str]] = deque()
        self._waiter: asyncio.Future | None = None
        self._udp: asyncio.DatagramTransport | None = None
        self._closed = False
        self.counters: dict[str, int] = dict.fromkeys(COUNTER_KEYS, 0)

    # --- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Bind the socket. Idempotent. Raises TransportBindError on failure."""
        if self._closed:
            raise TransportClosed(self._where())
        if self._udp is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self._bind_addr, self._port))
            sock.setblocking(False)
        except OSError as exc:
            sock.close()
            raise TransportBindError(
                f"cannot bind UDP socket {self._where()}: {exc}"
            ) from exc
        loop = asyncio.get_running_loop()
        udp, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), sock=sock)
        self._udp = udp

    @property
    def local_port(self) -> int | None:
        """The bound port, useful when port 0 (ephemeral) was requested."""
        if self._udp is None:
            return None
        return self._udp.get_extra_info("sockname")[1]

    # --- Transport interface ------------------------------------------------

    def send(self, frame: bytes) -> None:
        if self._closed:
            return
        c = self.counters
        if self._udp is None:
            c["send_errors"] += 1
            return
        if len(frame) > self._max_frame:
            c["oversized_tx"] += 1
            return
        if self._udp.get_write_buffer_size() > _WRITE_BUFFER_LIMIT:
            c["send_backpressure"] += 1
            return
        data = bytes(frame)
        if self._broadcast_addr is not None:
            targets = [(self._broadcast_addr, self._port)]
        else:
            targets = list(self._peers)
        for addr in targets:
            try:
                self._udp.sendto(data, addr)
                c["sent"] += 1
            except Exception:  # never raise into the protocol
                c["send_errors"] += 1

    async def recv(self) -> tuple[bytes, str]:
        while True:
            if self._queue:
                return self._queue.popleft()
            if self._closed:
                raise TransportClosed(self._where())
            loop = asyncio.get_running_loop()
            self._waiter = loop.create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._udp is not None:
            self._udp.close()  # releases the socket
        self._wake()

    # --- internal -----------------------------------------------------------

    def _where(self) -> str:
        return f"{self._bind_addr}:{self._port}"

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def _on_datagram(self, data: bytes, source_ip: str) -> None:
        """Receive path. Called by the asyncio protocol; tests may call it directly."""
        if self._closed:
            return
        c = self.counters
        c["received"] += 1
        if source_ip in self._own_addrs:
            c["own_ignored"] += 1
            return
        if len(data) > self._max_frame:
            c["oversized_rx"] += 1
            return
        # Same rule as the in-memory model: draw only when loss is enabled.
        if self._loss > 0.0 and self._loss_rng.random() < self._loss:
            c["lost_to_loss"] += 1
            return
        if len(self._queue) >= self._queue_limit:
            c["dropped_full"] += 1
            return
        self._queue.append((bytes(data), source_ip))
        c["delivered"] += 1
        self._wake()


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: UdpTransport) -> None:
        self._owner = owner

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._owner._on_datagram(data, addr[0])

    def error_received(self, exc: Exception) -> None:
        # asyncio routes send-side OSErrors here too; count, never raise.
        self._owner.counters["socket_errors"] += 1