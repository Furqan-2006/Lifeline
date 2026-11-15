"""Shared context and the Protocols the runtime depends on.

Nothing here imports M2's or M1's code. The runtime receives a codec and an
identity by injection, so the same node code runs with the real envelope
module, with a TEMP stand-in, or with a test double.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from lifeline.clock import Clock
from lifeline.config import NodeConfig
from lifeline.eventlog import EventLogger
from lifeline.rng import Rng


class DecodeError(Exception):
    """Raised by ``Codec.decode`` on any validation failure (C2.3)."""


class Envelope(Protocol):
    """The attributes the runtime reads from a decoded or outgoing envelope.

    Contract question 1: M2's envelope object must expose these names.
    ``type`` is the C2.3 type name (for example "TEXT"); ``mid`` is the
    8-byte Message ID; ``sender`` is the 32-byte public key; ``size`` is the
    frame length in bytes (contract question 2).
    """

    mid: bytes
    type: str
    sender: bytes
    ttl: int
    size: int


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


__all__ = [
    "Codec",
    "DecodeError",
    "Egress",
    "Envelope",
    "EnvelopeHandler",
    "Identity",
    "NodeContext",
]

_ = Any  # keeps the import used for type checkers in some editors
_ = Awaitable