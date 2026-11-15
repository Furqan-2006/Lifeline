"""TEMP: test-only stand-in for M2's envelope codec. Delete when envelope.py merges.

It is not the C1 wire format. It round-trips a small dataclass so the node
runtime can be tested before M2's codec exists. Malformed input is any byte
string that fails to parse, which is enough to exercise DROP MALFORMED.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from lifeline.context import DecodeError


@dataclass
class TempEnvelope:
    mid: bytes
    type: str
    sender: bytes
    ttl: int
    size: int = 0
    payload: bytes = b""


class TempCodec:
    def encode(self, envelope: TempEnvelope) -> bytes:
        body = json.dumps(
            {
                "mid": envelope.mid.hex(),
                "type": envelope.type,
                "sender": envelope.sender.hex(),
                "ttl": envelope.ttl,
                "payload": envelope.payload.hex(),
            },
            separators=(",", ":"),
        ).encode()
        envelope.size = len(body)
        return body

    def decode(self, data: bytes) -> TempEnvelope:
        try:
            obj = json.loads(data.decode("utf-8"))
            env = TempEnvelope(
                mid=bytes.fromhex(obj["mid"]),
                type=obj["type"],
                sender=bytes.fromhex(obj["sender"]),
                ttl=int(obj["ttl"]),
                payload=bytes.fromhex(obj["payload"]),
                size=len(data),
            )
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, AttributeError) as exc:
            raise DecodeError(str(exc)) from exc
        if len(env.mid) != 8 or len(env.sender) != 32:
            raise DecodeError("bad field length")
        return env


def temp_mid(sender: bytes, seq: int) -> bytes:
    return hashlib.sha256(sender + seq.to_bytes(4, "big")).digest()[:8]