"""Test-only forwarder. NOT the flood engine: M1's WP6 replaces this.

Verification is done by a codec wrapper, so the handler only sees envelopes
that passed the signature check (DROP BAD_SIG is logged at the codec layer).
For each accepted frame:
  - seen (sender, sequence) -> DROP DUPLICATE
  - else record as seen; DELIVER if broadcast or addressed to this node
  - forward with TTL - 1 through node.transmit while TTL > 1
Forwarding goes through node.transmit, so TX is logged there.
"""

from __future__ import annotations

from lifeline.node import Node

from tests.harness import temp_standins as ts

BROADCAST = b"\x00" * 16


class VerifyingCodec:
    """Wraps the TEMP codec: rejects bad signatures and logs DROP BAD_SIG."""

    def __init__(self, inner: ts.TempCodec, log) -> None:
        self._inner = inner
        self._log = log

    def encode(self, env):
        return self._inner.encode(env)

    def decode(self, data: bytes):
        env = self._inner.decode(data)
        if not ts.verify(data):
            self._log.drop(reason="BAD_SIG", mid=env.mid)
            raise ts.DecodeError("bad signature")
        return env


class ToyRelay:
    def __init__(self, node: Node, self_id: bytes) -> None:
        self._node = node
        self._self_id = self_id
        self._seen: set[tuple[bytes, int]] = set()
        self.delivered: list[ts.Envelope] = []
        node.add_handler(self._on_envelope)

    def _on_envelope(self, env: ts.Envelope, tx_label: str) -> None:
        key = (env.sender, env.sequence)
        if key in self._seen:
            self._node._log.drop(reason="DUPLICATE", mid=env.mid)
            return
        self._seen.add(key)
        if env.recipient in (BROADCAST, self._self_id):
            self._node._log.log("DELIVER", mid=env.mid, type=env.type, sender=env.sender)
            self.delivered.append(env)
        if env.ttl > 1:
            fwd = ts.Envelope(
                mid=env.mid, type=env.type, sender=env.sender, ttl=env.ttl - 1,
                sequence=env.sequence, payload=env.payload, recipient=env.recipient,
            )
            self._node.transmit(_Signed(fwd, env.signature))

class _Signed:
    """Wraps an envelope so the codec emits the original signature."""

    def __init__(self, env: ts.Envelope, signature: bytes) -> None:
        self.__dict__.update(env.__dict__)
        self.signature = signature