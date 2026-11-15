"""Node runtime: the composition root.

The only code that calls ``transport.send`` and ``transport.recv``. Every
TX and RX event is written here, so counts are identical across variants.

Setup for a harness, with nothing but a transport, config, codec and one handler::

    from lifeline.clock import FakeClock
    from lifeline.config import NodeConfig
    from lifeline.eventlog import EventLogger, MemorySink
    from lifeline.node import Node
    from lifeline.rng import Rng
    from lifeline.transport.memory import InMemoryNetwork

    clock = FakeClock()
    net = InMemoryNetwork(clock, Rng(1).child("net"))
    t = net.create_transport("n01")
    node = Node("n01", NodeConfig(), t, clock, Rng(1).child("n01"),
                EventLogger("n01", clock, sink=MemorySink()), codec)
    node.add_handler(lambda env, tx: print(env.mid.hex(), tx))
    await node.start()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from clock import Clock
from config import NodeConfig
from context import Codec, DecodeError, Envelope, EnvelopeHandler, Identity, NodeContext
from eventlog import EventLogger
from lifeline.runtime.rng import Rng
from lifeline.transport.base import Transport, TransportClosed

_log = logging.getLogger("lifeline.node")

# --- Sequence numbers (contract question 5 and C1.1) ----------------------------
SEQ_MAX = 2**32 - 1          # largest unsigned 4-byte value
SEQ_BLOCK = 100              # high-water mark is reserved this far ahead


def _seq_path(state_dir: Path, name: str) -> Path:
    """Contract question 5: state file location. Isolated here."""
    return state_dir / f"seq_{name}.json"


class Node:
    """One Lifeline node: owns its transport, timers and handlers."""

    def __init__(
        self,
        name: str,
        config: NodeConfig,
        transport: Transport,
        clock: Clock,
        rng: Rng,
        logger: EventLogger,
        codec: Codec,
        identity: Identity | None = None,
        state_dir: str | Path | None = None,
    ) -> None:
        self.name = name
        self.config = config
        self._transport = transport
        self._clock = clock
        self._rng = rng
        self._log = logger
        self._codec = codec
        self._identity = identity
        self._state_dir = Path(state_dir) if state_dir is not None else None

        self._handlers: list[EnvelopeHandler] = []
        self._tasks: set[asyncio.Task] = set()
        self._receive_task: asyncio.Task | None = None
        self._started = False
        self._stopped = False
        self._stopped_event: asyncio.Event | None = None

        self._seq_next = 0
        self._seq_reserved_until = 0
        self._load_sequence()

        self.ctx = NodeContext(
            name=name,
            config=config,
            clock=clock,
            rng=rng,
            log=logger,
            codec=codec,
            identity=identity,
            egress=self.transmit,
        )

    # --- identity ---------------------------------------------------------------

    @property
    def public_key(self) -> bytes | None:
        return None if self._identity is None else self._identity.public_key

    @property
    def node_id(self) -> bytes | None:
        return None if self._identity is None else self._identity.node_id

    # --- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopped_event = asyncio.Event()
        self._receive_task = asyncio.get_running_loop().create_task(self._receive_loop())
        self._receive_task = self.spawn(self._receive_loop(), "receive")
        self._receive_task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            self._transport.close()
        except Exception:
            _log.exception("transport close failed for %s", self.name)
        self._log.close()
        if self._stopped_event is not None:
            self._stopped_event.set()

    async def run_forever(self) -> None:
        """Start if needed and block until ``stop()`` is called."""
        await self.start()
        assert self._stopped_event is not None
        await self._stopped_event.wait()

    # --- registration -------------------------------------------------------------

    def add_handler(self, handler: EnvelopeHandler) -> None:
        self._handlers.append(handler)

    # --- frame in -----------------------------------------------------------------

    async def _receive_loop(self) -> None:
        while True:
            try:
                frame, tx_label = await self._transport.recv()
            except TransportClosed:
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("%s: transport recv failed", self.name)
                await self._clock.sleep(0)
                continue
            self._on_frame(frame, tx_label)

    def _on_frame(self, frame: bytes, tx_label: str) -> None:
        try:
            envelope = self._codec.decode(frame)
        except DecodeError:
            self._log.drop(reason="MALFORMED")
            return
        except Exception:
            _log.exception("%s: unexpected decode failure", self.name)
            self._log.drop(reason="MALFORMED")
            return

        self._log.rx(
            mid=envelope.mid,
            type=envelope.type,
            sender=envelope.sender,
            ttl=envelope.ttl,
            size=envelope.size,
            tx=tx_label,
        )
        for handler in list(self._handlers):
            try:
                handler(envelope, tx_label)
            except Exception:
                _log.exception("%s: handler %r raised", self.name, handler)

    # --- frame out ----------------------------------------------------------------

    def transmit(self, envelope: Envelope) -> None:
        """The only egress. Encodes, sends, logs TX. Never raises into a caller."""
        try:
            frame = self._codec.encode(envelope)
        except Exception:
            _log.exception("%s: encode failed", self.name)
            return
        try:
            self._transport.send(frame)
        except Exception:
            _log.exception("%s: transport send failed", self.name)
            return
        self._log.tx(
            mid=envelope.mid,
            type=envelope.type,
            sender=envelope.sender,
            ttl=envelope.ttl,
            size=envelope.size,
        )

    # --- timers -------------------------------------------------------------------

    def spawn(self, coro: Awaitable[Any], name: str) -> asyncio.Task:
        """Start an owned task. Cancelled on stop(); exceptions are reported."""
        async def run() -> Any:
            return await coro
        task = asyncio.get_running_loop().create_task(run(), name=f"{self.name}:{name}")
        self._receive_task = self.spawn(self._receive_loop(), "receive")
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error("%s: task %s failed", self.name, task.get_name(), exc_info=exc)

    def call_later(self, delay_s: float, fn: Callable[[], Any]) -> asyncio.Task:
        """Run ``fn`` once after ``delay_s`` of clock time."""

        async def once() -> None:
            await self._clock.sleep(delay_s)
            _invoke(fn, self.name, "call_later")

        return self.spawn(once(), "call_later")

    def every(
        self,
        interval_s: float,
        fn: Callable[[], Any],
        start_offset_s: float = 0.0,
        jitter_s: float = 0.0,
    ) -> asyncio.Task:
        """Run ``fn`` every ``interval_s`` of clock time.

        The first call happens after ``start_offset_s`` plus a uniform random
        offset in [0, jitter_s) drawn from this node's Rng, so HELLO beacons
        can be desynchronised deterministically.
        """
        if interval_s <= 0:
            raise ValueError("interval_s must be greater than zero")
        if start_offset_s < 0 or jitter_s < 0:
            raise ValueError("offsets must be non-negative")

        async def periodic() -> None:
            first = start_offset_s + (self._rng.uniform(0.0, jitter_s) if jitter_s > 0 else 0.0)
            if first > 0:
                await self._clock.sleep(first)
            while True:
                _invoke(fn, self.name, "every")
                await self._clock.sleep(interval_s)

        return self.spawn(periodic(), "every")

    # --- sequence numbers ---------------------------------------------------------

    def next_sequence(self) -> int:
        """Return the next unsigned 4-byte sequence number for this sender."""
        if self._seq_next > SEQ_MAX:
            raise OverflowError(
                f"{self.name}: sequence numbers exhausted (reached 2**32); restart with a new identity"
            )
        if self._seq_next >= self._seq_reserved_until:
            self._reserve_block()
        value = self._seq_next
        self._seq_next += 1
        return value

    def _load_sequence(self) -> None:
        if self._state_dir is None:
            return
        path = _seq_path(self._state_dir, self.name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            resume = int(data["high_water"])
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError):
            _log.warning("%s: unreadable sequence state at %s; starting from reserved mark", self.name, path)
            return
        self._seq_next = resume
        self._seq_reserved_until = resume

    def _reserve_block(self) -> None:
        new_mark = min(self._seq_next + SEQ_BLOCK, SEQ_MAX + 1)
        self._seq_reserved_until = new_mark
        if self._state_dir is None:
            return
        self._state_dir.mkdir(parents=True, exist_ok=True)
        path = _seq_path(self._state_dir, self.name)
        fd, tmp = tempfile.mkstemp(dir=self._state_dir, prefix=".seq_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"high_water": new_mark}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _invoke(fn: Callable[[], Any], node: str, what: str) -> None:
    try:
        fn()
    except Exception:
        _log.exception("%s: %s callback raised", node, what)