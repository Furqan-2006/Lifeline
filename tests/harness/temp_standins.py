"""TEMP: test-only stand-in for M2's envelope codec and crypto. DELETE when M2 merges.

Implements only what WP4.6 needs, following C1 field offsets and the C1.5
signing rule. Not extended beyond the test. Does not cover TEXT encryption.

C1.1 header (fixed offsets):
  0 version, 1 type, 2 priority, 3 TTL, 4..11 mid, 12..15 sequence,
  16..47 sender public key, 48..63 recipient NodeID, 64 payload length,
  65.. payload, then 64-byte signature.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

from lifeline.context import DecodeError

VERSION = 1
TYPE_CODES = {"HELLO": 1, "TEXT": 2, "SOS": 3, "ACK": 4, "KEY_REQUEST": 5, "KEY_REPLY": 6}
TYPE_NAMES = {v: k for k, v in TYPE_CODES.items()}
PRIORITY = {"SOS": 0, "ACK": 1, "TEXT": 2, "KEY_REQUEST": 3, "KEY_REPLY": 3, "HELLO": 4}
HEADER_LEN = 65
SIG_LEN = 64
ZERO_RECIPIENT = b"\x00" * 16
SIGN_PREFIX = b"LIFELINE-v1"


@dataclass
class Envelope:
    mid: bytes
    type: str
    sender: bytes
    ttl: int
    sequence: int
    payload: bytes = b""
    recipient: bytes = ZERO_RECIPIENT
    size: int = 0
    signature: bytes = b""


def message_id(sender: bytes, sequence: int) -> bytes:
    """C1.1: first 8 bytes of SHA-256 of (sender public key, sequence as 4 bytes)."""
    return hashlib.sha256(sender + sequence.to_bytes(4, "big")).digest()[:8]


def node_id_of(public_key: bytes) -> bytes:
    return hashlib.sha256(public_key).digest()[:16]


class Identity:
    """TEMP stand-in for C2.4 identity."""

    def __init__(self, private_key: Ed25519PrivateKey | None = None) -> None:
        self._private = private_key or Ed25519PrivateKey.generate()
        self.public_key = self._private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.node_id = node_id_of(self.public_key)

    def sign(self, data: bytes) -> bytes:
        return self._private.sign(data)


def signed_bytes(frame_without_sig: bytes) -> bytes:
    """C1.5: prefix, bytes 0 to 2, then bytes 4 up to the signature (TTL excluded)."""
    return SIGN_PREFIX + frame_without_sig[0:3] + frame_without_sig[4:]


def encode(self, env: Envelope) -> bytes:
        if env.signature:  # forwarded envelope keeps the origin's signature
            return _assemble(env)
        return encode(env, self._identity)

def _assemble(env: Envelope) -> bytes:
    payload = env.payload
    header = bytearray(HEADER_LEN)
    header[0] = VERSION
    header[1] = TYPE_CODES[env.type]
    header[2] = PRIORITY[env.type]
    header[3] = env.ttl
    header[4:12] = env.mid
    header[12:16] = env.sequence.to_bytes(4, "big")
    header[16:48] = env.sender
    header[48:64] = env.recipient
    header[64] = len(payload)
    frame = bytes(header) + payload + env.signature
    env.size = len(frame)
    return frame


def decode(frame: bytes) -> Envelope:
    """Parse a frame. Structural checks only (C1.6); does not verify the signature."""
    if len(frame) < HEADER_LEN + SIG_LEN:
        raise DecodeError("frame shorter than minimum")
    if frame[0] != VERSION:
        raise DecodeError("bad version")
    type_name = TYPE_NAMES.get(frame[1])
    if type_name is None:
        raise DecodeError("unknown type")
    ttl = frame[3]
    if ttl == 0 and type_name != "HELLO":
        raise DecodeError("TTL 0 on non-HELLO")
    payload_len = frame[64]
    if payload_len > 160:
        raise DecodeError("payload too long")
    if len(frame) != HEADER_LEN + payload_len + SIG_LEN:
        raise DecodeError("payload length disagrees with frame length")
    payload = frame[HEADER_LEN : HEADER_LEN + payload_len]
    signature = frame[-SIG_LEN:]
    return Envelope(
        mid=frame[4:12],
        type=type_name,
        sender=frame[16:48],
        ttl=ttl,
        sequence=int.from_bytes(frame[12:16], "big"),
        payload=payload,
        recipient=frame[48:64],
        size=len(frame),
        signature=signature,
    )


def verify(frame: bytes) -> bool:
    """Verify the signature against the sender key carried in the frame. Never raises."""
    try:
        env = decode(frame)
        pub = Ed25519PublicKey.from_public_bytes(env.sender)
        pub.verify(env.signature, signed_bytes(frame[:-SIG_LEN]))
        return True
    except (DecodeError, InvalidSignature, ValueError):
        return False


class TempCodec:
    """Adapter so the WP4.5 Node runtime can use this module as its codec.

    The runtime calls encode(envelope) -> bytes and decode(bytes) -> envelope.
    An envelope here must already carry a signature, so encode needs the
    identity; it is bound at construction.
    """

    def __init__(self, identity: Identity) -> None:
        self._identity = identity

    def encode(self, env: Envelope) -> bytes:
        return encode(env, self._identity)

    def decode(self, data: bytes) -> Envelope:
        return decode(data)