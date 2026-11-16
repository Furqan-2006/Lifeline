# Flood engine (WP6, contract C2.6 and design section 5).

# Receive pipeline, in this order (cheap checks before the costly signature check):

#   1. HELLO frames are ignored: the neighbor manager owns them
#   2. per-transmitter rate limit                       DROP RATE_LIMIT
#   3. already accepted? (sender, sequence) lookup      DROP DUPLICATE
#   4. verify signature                                 DROP BAD_SIG
#   5. replay window check, then mark as accepted       DROP REPLAY
#   6. SOS per-origin rate limit                        DROP RATE_LIMIT
#   7. deliver if addressed to this node or broadcast   DELIVER
#   8. forward unless addressed to this node: TTL above 1, else DROP TTL

# The accepted mark is written only after the signature verifies, so forged
# frames cannot make a node refuse the genuine message with the same sequence.

# Forwarding sends a copy with TTL minus one (TTL is not signed). In ``relay`` the
# copy waits a random jitter and is cancelled if ``counter_suppression_k``
# duplicates are heard first. In ``baseline`` it is sent at once, with no rate
# limits; duplicate suppression stays on.

# Usage::

#     engine = FloodEngine(node, neighbors)     # registers itself with the node
#     engine.on_deliver(callback)               # callback(envelope)
#     engine.submit(signed_envelope)            # send our own message, or a re-offer

# The optional ``store`` is the seam with M2's message store (C2.5): it receives ``put(envelope, now)`` for every 
# accepted message not addressed to this node.


from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from lifeline.protocol.codec import verify_envelope
from lifeline.protocol.crypto import node_id_of
from lifeline.protocol.envelope import BROADCAST, MSG_TYPE_NAMES, MsgType
from lifeline.runtime.context import Envelope
from lifeline.runtime.node import Node
from lifeline.services.neighbors import NeighborManager

_log = logging.getLogger("lifeline.flood")

DeliverCallback = Callable[[Envelope], None]

class StoreHook(Protocol):
    def put(self, envelope:Envelope, now: float) -> object: ...

@dataclass
class _Window:       # sliding window of accepted sequence numbers for one sender
    size: int
    highest: int = -1
    accepted: set[int] = field(default_factory=set)

    def seen(self, seq: int) -> bool:
        return seq in self.accepted

    def accept(self, seq:int) -> bool:
        if seq in self.accepted or seq <= self.highest - self.size:
            return False
        if seq > self.highest: 
            self.highest = seq
            floor = seq - self.size
            self.accepted = {s for s in self.accepted if s > floor}
        self.accepted.add(seq)
        return True

class _TokenBucket:     # peer-key limit of rate events per second, with burst of capacity
    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, key:str, now: float) -> bool:
        tokens, last = self._state.get(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - last) * self._rate)
        if tokens >= 1.0:
            self._state[key] = (tokens - 1.0, now)
            return True
        self._state[key] = (tokens, now)
        return False

@dataclass
class _Pending:
    envelope: Envelope
    duplicates: int = 0
    task: asyncio.Task | None = None

class FloodEngine:
    def __init__(self, node:Node, neighbors: NeighborManager, store:StoreHook | None = None) -> None:
        identity = node.ctx.identity
        if identity is None:
            raise ValueError("FloodEngine needs a node with identity")

        cfg = node.ctx.config
        self._node = node
        self._ctx = node.ctx
        self._cfg = cfg
        self._neighbors = neighbors
        self._store = store
        self._me = identity.node_id
        self._rng = node.ctx.rng.child("flood")
        self._windows: dict[bytes, _Window] = {}
        self._pending: dict[tuple[bytes, int], _Pending] = {}
        self._tx_bucket = _TokenBucket(cfg.rate_limit_tx_fps, cfg.rate_limit_tx_fps)
        self._sos_last: dict[bytes, float] = {}
        self._deliver_callbacks: list[DeliverCallback] = []
        self.stats = {"delivered": 0, "forwarded": 0, "suppressed":0}

        node.add_handler(self.on_envelope)

# helpers
    def _window_for(self, sender: bytes) -> _Window:
        window = self._windows.get(sender)
        if window is None:
            window = _Window(size=self._cfg.seq_window)
            self._windows[sender] = window
        return window
    
    def _drop(self, reason: str, env: Envelope) -> None:
        self._ctx.log.drop(reason=reason, mid=env.msg_id)

# interface
    def submit(self, envelope: Envelope) -> None:
        self._window_for(envelope.sender_pubkey).accept(envelope.seq_num)
        self._node.transmit(envelope)

    def on_deliver(self, callback: DeliverCallback) -> None:
        self._deliver_callbacks.append(callback)

    def neighbors(self):
        return self._neighbors.neighbors()
    
    def on_new_neighbor(self, callback) -> None:
        self._neighbors.on_new_neighbor(callback)

# recieve pipeline
    def on_envelope(self, env:Envelope, tx_label: str) -> None:
        if env.msg_type == MsgType.HELLO:
            return
        cfg = self._cfg
        now = self._ctx.clock.now()

        if cfg.use_rate_limits and not self._tx_bucket.allow(tx_label, now):
            self._drop("RATE_LIMIT", env)
            return

        key = (env.sender_pubkey, env.seq_num)
        window = self._windows.get(env.sender_pubkey)
        if window is not None and window.seen(env.seq_num):
            self._drop("DUPLICATE", env)
            self._note_duplicate(key)
            return 

        if not verify_envelope(env):
            self._drop("BAD SIG", env)
            return

        if not self._window_for(env.sender_pubkey).accept(env.seq_num):
            self._drop("REPLAY", env)
            return

        if cfg.use_rate_limits and env.msg_type == MsgType.SOS:
            last = self._sos_last.get(env.sender_pubkey)
            if last is not None and now - last < cfg.rate_limit_sos_origin_s:
                self._drop("RATE_LIMIT", env)
                return
            self._sos_last[env.sender_pubkey] = now

        addressed_to_me = env.recipient_node_id == self._me
        if addressed_to_me or env.recipient_node_id == BROADCAST:
            self._deliver(env)
        if addressed_to_me:
            return

        if self._store is not None:
            try:
                self._store.put(env, now)
            except Exception:
                _log.exception(f"{self._ctx.name}: message store put failed")

        if env.ttl > 1:
            self._forward(env)
        else:
            self._drop("TTL", env)

# delivery
    def _deliver(self, env: Envelope) -> None:
        self.stats["delivered"] += 1
        self._ctx.log.log(
            "DELIVER", mid=env.msg_id, type=MSG_TYPE_NAMES.get(env.msg_type, str(env.msg_type)), sender=node_id_of(env.sender_pubkey))
        for callback in list(self._deliver_callbacks):
            try:
                callback(env)
            except Exception:
                _log.exception(f"{self._ctx.name}: on_deliver callback raised")
# forwarding
    def _forward(self, env:Envelope) -> None:
        copy = replace(env, ttl=env.ttl - 1)
        if not self._cfg.use_jitter:
            self._send_forward(copy)
            return
        key = (env.sender_pubkey, env.seq_num)
        lo, hi = self._cfg.jitter_ms
        delay_s = self._rng.uniform(lo, hi) / 1000.0
        pending = _Pending(envelope=copy)
        self._pending[key] = pending
        pending.task = self._node.call_later(delay_s, lambda: self._fire(key))

    def _fire(self, key:tuple[bytes, int]) -> None:
        pending = self._pending.pop(key, None)
        if pending is not None:
            self._send_forward(pending.envelope)

    def _send_forward(self, env: Envelope) -> None:
        self.stats["forwarded"] += 1
        self._node.transmit(env)

    def _note_duplicate(self, key: tuple[bytes, int]) -> None:
        pending = self._pending.get(key)
        if pending is None or not self._cfg.use_counter_suppression:
            return
        pending.duplicates += 1
        if pending.duplicates >= self._cfg.counter_suppression_k:
            del self._pending[key]
            if pending.task is not None:
                pending.task.cancel()
            self.stats["suppressed"] += 1


