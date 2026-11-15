"""What the runtime and the services use: the codec object plus envelope helpers.

``CODEC`` is what ``Node`` receives. ``build_envelope`` makes a fully signed
envelope (the runtime does not sign). ``verify_envelope`` checks the
signature of a decoded one. Everything else lives in envelope.py / crypto.py.
"""

from __future__ import annotations

from dataclasses import replace

from lifeline.protocol import crypto
from lifeline.protocol.envelope import (
    BROADCAST,
    LIFELINE_VERSION,
    SIGNATURE_LEN,
    Envelope,
    MsgType,
    compute_msg_id,
    decode,
    encode,
    signed_bytes,
)

#: C1.3 priorities (lower is more important).
DEFAULT_PRIORITY = {
    MsgType.SOS: 0,
    MsgType.ACK: 1,
    MsgType.TEXT: 2,
    MsgType.KEY_REQUEST: 3,
    MsgType.KEY_REPLY: 3,
    MsgType.HELLO: 4,
}


class Codec:
    """Codec as the runtime expects it (C2.3): encode, decode."""

    def encode(self, envelope: Envelope) -> bytes:
        return encode(envelope)

    def decode(self, data: bytes) -> Envelope:
        return decode(data)  # raises lifeline.runtime.context.DecodeError

    def signed_bytes(self, envelope: Envelope) -> bytes:
        return signed_bytes(envelope)


CODEC = Codec()


def sign_envelope(identity: crypto.Identity, envelope: Envelope) -> Envelope:
    """Return a copy of ``envelope`` carrying a valid signature from ``identity``."""
    return replace(envelope, signature=crypto.sign(identity, signed_bytes(envelope)))


def verify_envelope(envelope: Envelope) -> bool:
    """True if the signature matches the sender key. Never raises."""
    try:
        data = signed_bytes(envelope)
    except Exception:
        return False
    return crypto.verify(envelope.sender_pubkey, data, envelope.signature)


def build_envelope(
    identity: crypto.Identity,
    seq: int,
    msg_type: int,
    payload: bytes,
    *,
    ttl: int,
    recipient: bytes = BROADCAST,
    priority: int | None = None,
    ephemeral_pubkey: bytes | None = None,
    nonce: bytes | None = None,
) -> Envelope:
    """Build and sign a new envelope. ``seq`` comes from ``node.next_sequence()``."""
    unsigned = Envelope(
        version=LIFELINE_VERSION,
        msg_type=msg_type,
        priority=DEFAULT_PRIORITY[msg_type] if priority is None else priority,
        ttl=ttl,
        msg_id=compute_msg_id(identity.public_key, seq),
        seq_num=seq,
        sender_pubkey=identity.public_key,
        recipient_node_id=recipient,
        payload_len=len(payload),
        ephemeral_pubkey=ephemeral_pubkey,
        nonce=nonce,
        payload=payload,
        signature=bytes(SIGNATURE_LEN),
    )
    return sign_envelope(identity, unsigned)