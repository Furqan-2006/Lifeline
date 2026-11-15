"""Shared context and the Protocols the runtime depends on.

The runtime adapts to the protocol package, not the other way round: the
envelope type and DecodeError come from lifeline.protocol.envelope.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from lifeline.protocol.envelope import DecodeError, Envelope
from lifeline.runtime.clock import Clock
from lifeline.runtime.config import NodeConfig
from lifeline.runtime.eventlog import EventLogger
from lifeline.runtime.rng import Rng


class Codec(Protocol):
    """Envelope codec, C2.3."""

    def encode(self, envelope: Envelope) -> bytes: ...
    def decode(self, data: bytes) -> Envelope: ...  # raises DecodeError


class Identity(Protocol):
    """Own key pair, C2.4."""

    public_key: bytes  # 32 bytes
    node_id: bytes     # 16 bytes


EnvelopeHandler = Callable[[Envelope, str], None]
Egress = Callable[[Envelope], None]


@dataclass(frozen=True)
class NodeContext:
    """Everything a module needs from its node. Modules are built as Module(ctx)."""

    name: str
    config: NodeConfig
    clock: Clock
    rng: Rng
    log: EventLogger
    codec: Codec
    identity: Identity | None
    egress: Egress


__all__ = ["Codec", "DecodeError", "Egress", "Envelope", "EnvelopeHandler", "Identity", "NodeContext"]