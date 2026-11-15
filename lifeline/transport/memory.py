"""Deterministic in-memory broadcast network for tests and the harness.

Setup, for a 3-node line n01 - n02 - n03::

    from lifeline.clock import FakeClock
    from lifeline.rng import Rng
    from lifeline.transport.memory import InMemoryNetwork

    clock = FakeClock()
    net = InMemoryNetwork(clock, Rng(1).child("net"))
    t1 = net.create_transport("n01")
    t2 = net.create_transport("n02")
    t3 = net.create_transport("n03")
    net.set_adjacency({"n01": {"n02"}, "n02": {"n01", "n03"}, "n03": {"n02"}})
"""

from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass, field

from lifeline.clock import Clock
from lifeline.rng import Rng
from lifeline.transport.base import Transport, TransportClosed

# Proposed default for contract question 3: the loss stream for a directed
# pair is an Rng child named by the pair, so it is independent of every
# other pair and of the order in which frames are sent.
_LOSS_CHILD_FORMAT = "loss:{sender}>{receiver}"


@dataclass
class NetworkCounters:
    sent: int = 0
    delivered: int = 0
    lost_to_loss: int = 0
    dropped_full: int = 0
    dropped_link_down: int = 0


@dataclass
class _LinkSpec:
    """Per-direction link properties. ``None`` means use the network default.

    Proposed default for contract question 1.
    """

    loss: float | None = None
    delay_ms: float | None = None


@dataclass
class _Pending:
    """A frame scheduled for delayed delivery, so it can be cancelled on close."""

    task: asyncio.Task


class InMemoryTransport(Transport):
    """One node's view of an :class:`InMemoryNetwork`."""

    def __init__(self, network: "InMemoryNetwork", name: str, queue_limit: int) -> None:
        self._network = network
        self.name = name
        self._queue: collections.deque[tuple[bytes, str]] = collections.deque()
        self._queue_limit = queue_limit
        self._waiter: asyncio.Future | None = None
        self._pending: set[asyncio.Task] = set()
        self._closed = False
        self.counters = NetworkCounters()

    # --- Transport interface -------------------------------------------------

    def send(self, frame: bytes) -> None:
        if self._closed:
            return
        self._network._broadcast(self, bytes(frame))

    async def recv(self) -> tuple[bytes, str]:
        while True:
            if self._queue:
                return self._queue.popleft()
            if self._closed:
                raise TransportClosed(self.name)
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
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        self._wake()

    # --- internal ------------------------------------------------------------

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    def _enqueue(self, frame: bytes, label: str) -> bool:
        """Put a frame in the queue. Returns False if the queue was full."""
        if self._closed:
            return False
        if len(self._queue) >= self._queue_limit:
            self.counters.dropped_full += 1
            self._network.counters.dropped_full += 1
            return False
        self._queue.append((frame, label))
        self.counters.delivered += 1
        self._network.counters.delivered += 1
        self._wake()
        return True

    def _track(self, task: asyncio.Task) -> None:
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


class InMemoryNetwork:
    """Owns the topology, loss decisions and delayed deliveries for a set of nodes."""

    def __init__(
        self,
        clock: Clock,
        rng: Rng,
        default_loss: float = 0.0,
        default_delay_ms: float = 0.0,
        queue_limit: int = 1000,
    ) -> None:
        if not 0.0 <= default_loss <= 1.0:
            raise ValueError("default_loss must be in [0, 1]")
        if default_delay_ms < 0:
            raise ValueError("default_delay_ms must be non-negative")
        if queue_limit < 1:
            raise ValueError("queue_limit must be at least 1")
        self._clock = clock
        self._rng = rng
        self._default_loss = default_loss
        self._default_delay_ms = default_delay_ms
        self._queue_limit = queue_limit
        self._transports: dict[str, InMemoryTransport] = {}
        self._links: dict[tuple[str, str], _LinkSpec] = {}
        self._loss_rngs: dict[tuple[str, str], Rng] = {}
        self.counters = NetworkCounters()

    # --- nodes ---------------------------------------------------------------

    def create_transport(self, name: str) -> InMemoryTransport:
        if name in self._transports:
            raise ValueError(f"duplicate node name: {name}")
        transport = InMemoryTransport(self, name, self._queue_limit)
        self._transports[name] = transport
        return transport

    # --- topology ------------------------------------------------------------

    def set_link(
        self,
        a: str,
        b: str,
        up: bool = True,
        bidirectional: bool = True,
        loss: float | None = None,
        delay_ms: float | None = None,
    ) -> None:
        """Create, update or remove the link a -> b (and b -> a if bidirectional)."""
        if a == b:
            raise ValueError("a node cannot link to itself")
        if loss is not None and not 0.0 <= loss <= 1.0:
            raise ValueError("loss must be in [0, 1]")
        if delay_ms is not None and delay_ms < 0:
            raise ValueError("delay_ms must be non-negative")
        pairs = [(a, b), (b, a)] if bidirectional else [(a, b)]
        for src, dst in pairs:
            if up:
                self._links[(src, dst)] = _LinkSpec(loss=loss, delay_ms=delay_ms)
            else:
                self._links.pop((src, dst), None)

    def set_adjacency(self, mapping: dict[str, set[str]]) -> None:
        """Replace all links with ``mapping`` (name to set of neighbor names).

        The mapping is taken as directed: ``mapping["a"] == {"b"}`` creates
        a -> b only. Pass symmetric sets for symmetric links.
        """
        self._links = {
            (src, dst): _LinkSpec()
            for src, dsts in mapping.items()
            for dst in dsts
            if src != dst
        }

    def neighbors_of(self, name: str) -> set[str]:
        return {dst for (src, dst) in self._links if src == name}

    # --- delivery ------------------------------------------------------------

    def _loss_rng(self, sender: str, receiver: str) -> Rng:
        key = (sender, receiver)
        rng = self._loss_rngs.get(key)
        if rng is None:
            rng = self._rng.child(_LOSS_CHILD_FORMAT.format(sender=sender, receiver=receiver))
            self._loss_rngs[key] = rng
        return rng

    def _effective_loss(self, spec: _LinkSpec) -> float:
        return self._default_loss if spec.loss is None else spec.loss

    def _effective_delay_ms(self, spec: _LinkSpec) -> float:
        return self._default_delay_ms if spec.delay_ms is None else spec.delay_ms

    def _broadcast(self, sender: InMemoryTransport, frame: bytes) -> None:
        self.counters.sent += 1
        sender.counters.sent += 1
        receivers = sorted(self.neighbors_of(sender.name))
        for receiver_name in receivers:
            spec = self._links[(sender.name, receiver_name)]
            loss = self._effective_loss(spec)
            if loss > 0.0 and self._loss_rng(sender.name, receiver_name).random() < loss:
                self.counters.lost_to_loss += 1
                sender.counters.lost_to_loss += 1
                continue
            receiver = self._transports[receiver_name]
            delay_ms = self._effective_delay_ms(spec)
            if delay_ms <= 0:
                self._deliver(sender.name, receiver, frame)
            else:
                self._schedule(sender.name, receiver, frame, delay_ms / 1000.0)

    def _deliver(self, sender_name: str, receiver: InMemoryTransport, frame: bytes) -> None:
        """Hand a frame to a receiver, re-checking that the link still exists."""
        if (sender_name, receiver.name) not in self._links:
            self.counters.dropped_link_down += 1
            return
        receiver._enqueue(frame, sender_name)

    def _schedule(
        self, sender_name: str, receiver: InMemoryTransport, frame: bytes, seconds: float
    ) -> None:
        async def later() -> None:
            await self._clock.sleep(seconds)
            self._deliver(sender_name, receiver, frame)

        task = asyncio.get_running_loop().create_task(later())
        receiver._track(task)

    def inject(self, to_name: str, frame: bytes, label: str = "attacker") -> bool:
        """Put a frame straight into one receiver's queue.

        Bypasses topology, loss and delay. Used by forgery, replay and flood
        tests. Returns False if the queue was full or the receiver is closed.
        """
        receiver = self._transports[to_name]
        accepted = receiver._enqueue(bytes(frame), label)
        return accepted