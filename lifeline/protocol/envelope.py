"""Envelope codec: the wire format of contract C1 and the codec calls of C2.3.

Field names are the WP2 names (msg_id, msg_type, sender_pubkey, seq_num).
Additions over the first version, none of which changes an existing call:
compute_msg_id (C1.1), MSG_TYPE_NAMES (for the C3 log), signed_bytes also
accepting an Envelope (C2.3), and a payload-length check in encode.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Optional

# > big-endian, B 1 byte, 8s 8 bytes, I 4-byte unsigned, 32s / 16s byte strings
BASE_HEADER_FORMAT = struct.Struct(">B B B B 8s I 32s 16s B")
TEXT_EXT_FORMAT = struct.Struct(">32s 12s")

BASE_HEADER_LEN = BASE_HEADER_FORMAT.size  # 65
TEXT_EXT_LEN = TEXT_EXT_FORMAT.size        # 44
SIGNATURE_LEN = 64
MAX_PAYLOAD_LEN = 160
BROADCAST = bytes(16)

LIFELINE_VERSION = 1
SIGNED_BYTES_PREFIX = b"LIFELINE-v1"


class MsgType:
    HELLO = 1
    TEXT = 2
    SOS = 3
    ACK = 4
    KEY_REQUEST = 5
    KEY_REPLY = 6


#: Type names as written in the C3 log, for example 1 -> "HELLO".
MSG_TYPE_NAMES = {v: k for k, v in vars(MsgType).items() if k.isupper()}


class DecodeError(Exception):
    """Raised on any validation failure. The runtime drops the frame and logs MALFORMED."""


@dataclass(slots=True)
class Envelope:
    version: int
    msg_type: int
    priority: int
    ttl: int
    msg_id: bytes             # 8 bytes
    seq_num: int              # 4 bytes unsigned
    sender_pubkey: bytes      # 32 bytes
    recipient_node_id: bytes  # 16 bytes
    payload_len: int

    # TEXT only (type 2)
    ephemeral_pubkey: Optional[bytes] = None  # 32 bytes
    nonce: Optional[bytes] = None             # 12 bytes

    payload: bytes = b""      # up to 160 bytes
    signature: bytes = b""    # 64 bytes


def compute_msg_id(sender_pubkey: bytes, seq_num: int) -> bytes:
    """C1.1: first 8 bytes of SHA-256 of (sender public key, sequence as 4 bytes)."""
    return hashlib.sha256(sender_pubkey + struct.pack(">I", seq_num)).digest()[:8]


def _pack(env: Envelope, signature: bytes) -> bytes:
    if len(env.payload) != env.payload_len or env.payload_len > MAX_PAYLOAD_LEN:
        raise ValueError("payload_len must equal len(payload) and be at most 160")
    if len(signature) != SIGNATURE_LEN:
        raise ValueError("signature must be 64 bytes")
    base = BASE_HEADER_FORMAT.pack(
        env.version, env.msg_type, env.priority, env.ttl, env.msg_id, env.seq_num,
        env.sender_pubkey, env.recipient_node_id, env.payload_len,
    )
    ext = b""
    if env.msg_type == MsgType.TEXT:
        if not env.ephemeral_pubkey or not env.nonce:
            raise ValueError("TEXT messages must include ephemeral_pubkey and nonce")
        ext = TEXT_EXT_FORMAT.pack(env.ephemeral_pubkey, env.nonce)
    return base + ext + env.payload + signature


def encode(env: Envelope) -> bytes:
    """Serialize an envelope to a frame (C1.1). Raises ValueError if it is malformed."""
    return _pack(env, env.signature)


def signed_bytes(source: "Envelope | bytes") -> bytes:
    """The signed bytes (C1.5): prefix, bytes 0 to 2, then bytes 4 up to the signature.

    Takes a raw frame (as before) or an Envelope. For an Envelope the signature
    field is ignored, so it works before signing and after.
    """
    raw = _pack(source, bytes(SIGNATURE_LEN)) if isinstance(source, Envelope) else source
    if len(raw) < BASE_HEADER_LEN + SIGNATURE_LEN:
        raise DecodeError("frame too small to contain a signature")
    return SIGNED_BYTES_PREFIX + raw[0:3] + raw[4:-SIGNATURE_LEN]


def decode(raw_frame: bytes) -> Envelope:
    """Validate untrusted bytes (C1.6) and return an Envelope, or raise DecodeError."""
    if len(raw_frame) < BASE_HEADER_LEN + SIGNATURE_LEN:
        raise DecodeError("frame smaller than the absolute minimum (129 bytes)")

    (version, msg_type, priority, ttl, msg_id, seq_num,
     sender_pubkey, recipient, payload_len) = BASE_HEADER_FORMAT.unpack_from(raw_frame, 0)

    if version != LIFELINE_VERSION:
        raise DecodeError(f"unsupported protocol version: {version}")
    if not (MsgType.HELLO <= msg_type <= MsgType.KEY_REPLY):
        raise DecodeError(f"unknown message type: {msg_type}")
    if payload_len > MAX_PAYLOAD_LEN:
        raise DecodeError(f"payload length {payload_len} exceeds 160")

    expected_len = BASE_HEADER_LEN + payload_len + SIGNATURE_LEN
    if msg_type == MsgType.TEXT:
        expected_len += TEXT_EXT_LEN
    if len(raw_frame) != expected_len:
        raise DecodeError(f"frame length {len(raw_frame)} != expected {expected_len}")

    if msg_type == MsgType.HELLO and ttl != 1:
        raise DecodeError("HELLO must have TTL 1")
    if msg_type != MsgType.HELLO and ttl == 0:
        raise DecodeError("non-HELLO message cannot have TTL 0")
    if msg_type == MsgType.TEXT and recipient == BROADCAST:
        raise DecodeError("TEXT cannot have a broadcast recipient")

    ephemeral_pubkey = None
    nonce = None
    payload_start = BASE_HEADER_LEN
    if msg_type == MsgType.TEXT:
        ephemeral_pubkey, nonce = TEXT_EXT_FORMAT.unpack_from(raw_frame, payload_start)
        payload_start += TEXT_EXT_LEN

    return Envelope(
        version=version, msg_type=msg_type, priority=priority, ttl=ttl, msg_id=msg_id,
        seq_num=seq_num, sender_pubkey=sender_pubkey, recipient_node_id=recipient,
        payload_len=payload_len, ephemeral_pubkey=ephemeral_pubkey, nonce=nonce,
        payload=bytes(raw_frame[payload_start:payload_start + payload_len]),
        signature=bytes(raw_frame[-SIGNATURE_LEN:]),
    )