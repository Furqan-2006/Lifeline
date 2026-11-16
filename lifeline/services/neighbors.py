#* Neighbor discovery (WP5, contract C2.6).

#* Each node beacons a signed HELLO (TTL 1, payload: node class, capability
#* flags) every ``hello_interval_s``. A neighbor is a node whose valid HELLO was
#* heard within the last ``hello_interval_s * neighbor_timeout_missed`` seconds.

#* Events written here (C3): NEIGHBOR_UP, NEIGHBOR_DOWN, and DROP with reason
#* BAD_SIG or MALFORMED for a HELLO this module rejects. TX and RX are written
#* by the runtime. The flood engine (WP6) must ignore HELLO frames, because
#* this module owns them.

#* Usage, from inside the running event loop::

#*     neighbors = NeighborManager(node, node_class=0)
#*     neighbors.start()                      # after node.start() or before; timers only
#*     neighbors.on_new_neighbor(callback)    # callback(Neighbor)
#*     neighbors.neighbors()                  # list[Neighbor], sorted by node_id

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from lifeline.protocol.codec import build_envelope, verify_envelope
from lifeline.protocol.crypto import Identity, node_id_of
from lifeline.protocol.envelope import MsgType
from lifeline.runtime.context import Envelope
from lifeline.runtime.node import Node


_log = logging.getLogger("lifeline.neighbor_discovery.neighbors")

NODE_CLASS_HANDHELD = 0
NODE_CLASS_RELAY = 1
_HELLO_PAYLOAD_LEN = 2

@dataclass(frozen=True, slots=True)
class Neighbor:
    node_id: bytes          # 16 bytes
    public_key: bytes       # 32 bytes
    node_class: int         # class: handheld or relay 
    last_heard: float       # clock time of last valid HELLO 

NewNeighborCallback = Callable[[Neighbor], None]

class NeighborManager:
    def __init__(self, node:Node, node_class: int = NODE_CLASS_HANDHELD) -> None:
        identity = node.ctx.identity
        if not isinstance(identity, Identity):
            raise ValueError("NeighborManager needs a node with a signing identity")
        if node_class not in (NODE_CLASS_HANDHELD, NODE_CLASS_RELAY):
            raise ValueError("node_class must be 0 (handheld) or 1 (relay)")

        self._node = node
        self._ctx = node.ctx
        self._identity: Identity = identity 
        self._node_class = node_class
        self._table: dict[bytes, Neighbor] = {}
        self._callbacks: list[NewNeighborCallback] = []
        self._started = False
        node.add_handler(self.on_envelope)

# --- public interface (C2.6) -------------------------------------------------

    @property
    def timeout_s(self) -> float:
        cfg = self._ctx.config
        return cfg.hello_interval_s * cfg.neighbor_timeout_missed

    def neighbors(self) -> list[Neighbor]:
        return sorted(self._table.values(), key=lambda n: n.node_id)

    def get(self, node_id: bytes) -> Neighbor | None:
        return self._table.get(node_id)

    def on_new_neighbor(self, callback: NewNeighborCallback) -> None:
        self._callbacks.append(callback)

# --- timers ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return 

        self._started = True
        interval = self._ctx.config.hello_interval_s     # first beacon at random offset in [0, interval); nodes do not sync
        self._node.every(interval, self.send_hello, start_offset_s=0.0, jitter_s=interval)
        self._node.every(interval / 2, self._sweep, start_offset_s=interval / 2)

    def send_hello(self) -> None:
        envelope = build_envelope(
            self._identity, self._node.next_sequence(), MsgType.HELLO, bytes([self._node_class, 0]), ttl=1)
        self._node.transmit(envelope)

# --- receive -----------------------------------------------------------------

    def on_envelope(self, env: Envelope, tx_lable: str) -> None:
        if env.msg_type != MsgType.HELLO:
            return 
        if env.sender_pubkey == self._identity.public_key:
            return
        if not verify_envelope(env):
            self._ctx.log.drop(reason="BAD_SIG", mid=env.msg_id)
            return
        if len(env.payload) != _HELLO_PAYLOAD_LEN:
            self._ctx.log.drop(reason="MALFORMED", mid=env.msg_id)
            return

        node_id = node_id_of(env.sender_pubkey)
        record = Neighbor(
            node_id=node_id, public_key=env.sender_pubkey, node_class=env.payload[0], last_heard=self._ctx.clock.now())
        is_new = node_id not in self._table
        self._table[node_id] = record
        if is_new:
            self._ctx.log.log("NEIGHBOR_UP", peer=node_id)
            for callback in list(self._callbacks):
                try:
                    callback(record)
                except Exception:
                    _log.exception(f"{self._ctx.name}: on_new_neighbor callback raised")

# --- expiry ----------------------------------------------------------------

    def _sweep(self) -> None:
        now = self._ctx.clock.now()
        timeout = self.timeout_s
        expired = sorted(nid for nid, n in self._table.items() if now - n.last_heard > timeout)
        for node_id in expired:
            del self._table[node_id]
            self._ctx.log.log("NEIGHBOR_DOWN", peer=node_id)
        





