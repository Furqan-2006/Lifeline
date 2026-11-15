"""WP4.6 done-when: three nodes pass a signed envelope; logs match C3.

Topology: n01 - n02 - n03 (n01 and n03 cannot hear each other).
n01 sends one signed SOS, broadcast, TTL 3.

Expected traffic, derived by hand:
  n01 TX: 1 (its SOS)
  n02 RX: 1 from n01 -> DELIVER -> forwards TTL 2 (TX 1)
  n01 RX: 1 from n02 (its own message back) -> DROP DUPLICATE
  n03 RX: 1 from n02 -> DELIVER -> forwards TTL 1? No: TTL 1 is not forwarded.
  n02 RX again from n03? n03 does not forward (TTL 1), so no.
So: n01 TX=1, RX=1; n02 TX=1, RX=1; n03 TX=0, RX=1.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lifeline.clock import FakeClock
from lifeline.config import NodeConfig
from lifeline.eventlog import EventLogger, MemorySink
from lifeline.logschema import assert_valid_log
from lifeline.node import Node
from lifeline.rng import Rng
from lifeline.transport.memory import InMemoryNetwork
from tests.harness import temp_standins as ts
from tests.harness.toy_relay import ToyRelay, VerifyingCodec


def build_line(seed: int, loss: float = 0.0):
    clock = FakeClock()
    net = InMemoryNetwork(clock, Rng(seed).child("net"), default_loss=loss)
    names = ("n01", "n02", "n03")
    ids = {n: ts.Identity() for n in names}
    sinks = {n: MemorySink() for n in names}
    nodes = {}
    relays = {}
    for n in names:
        transport = net.create_transport(n)
        logger = EventLogger(n, clock, sink=sinks[n])
        codec = VerifyingCodec(ts.TempCodec(ids[n]), logger)
        node = Node(n, NodeConfig(), transport, clock, Rng(seed).child(n), logger,
                    codec, identity=ids[n])
        nodes[n] = node
        relays[n] = ToyRelay(node, ids[n].node_id)
    net.set_adjacency({"n01": {"n02"}, "n02": {"n01", "n03"}, "n03": {"n02"}})
    return clock, net, ids, nodes, relays, sinks


async def run_sos(seed: int = 1, loss: float = 0.0):
    clock, net, ids, nodes, relays, sinks = build_line(seed, loss)
    for n in nodes.values():
        await n.start()
    env = ts.Envelope(
        mid=ts.message_id(ids["n01"].public_key, 0), type="SOS",
        sender=ids["n01"].public_key, ttl=3, sequence=0,
        payload=b"\x00" * 9,
    )
    nodes["n01"].transmit(_Pre(env, ids["n01"]))
    for _ in range(30):
        await asyncio.sleep(0)
        await clock.advance(0.01)
    for n in nodes.values():
        await n.stop()
    logs = {name: "".join(s.lines) for name, s in sinks.items()}
    return logs, relays, ids


class _Pre:
    """Sign the origin envelope via the codec before transmit (origin path)."""

    def __init__(self, env, identity):
        frame = ts.encode(env, identity)
        self.mid, self.type, self.sender = env.mid, env.type, env.sender
        self.ttl, self.size = env.ttl, len(frame)
        self.sequence = env.sequence
        self.payload = env.payload
        self.recipient = env.recipient
        self.signature = env.signature
        self._frame = frame


def events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line]


def by_event(text: str, name: str) -> list[dict]:
    return [e for e in events(text) if e["event"] == name]


async def test_three_node_line_delivery_and_counts():
    logs, relays, ids = await run_sos()
    # Expected counts, derived in the module docstring.
    assert len(by_event(logs["n01"], "TX")) == 1
    assert len(by_event(logs["n02"], "TX")) == 1
    assert len(by_event(logs["n03"], "TX")) == 0
    assert len(by_event(logs["n01"], "RX")) == 1
    assert len(by_event(logs["n02"], "RX")) == 1
    assert len(by_event(logs["n03"], "RX")) == 1
    assert len(by_event(logs["n01"], "DROP")) == 1
    assert by_event(logs["n01"], "DROP")[0]["reason"] == "DUPLICATE"


async def test_n03_delivers_same_message_with_reduced_ttl_and_valid_signature():
    logs, relays, ids = await run_sos()
    delivered = relays["n03"].delivered
    assert len(delivered) == 1
    env = delivered[0]
    assert env.sender == ids["n01"].public_key
    assert env.sequence == 0
    assert env.payload == b"\x00" * 9
    assert env.ttl == 1  # sent TTL 3, relayed by n02 as 2, decremented on forward


async def test_rx_precedes_deliver_at_relays():
    logs, relays, ids = await run_sos()
    for name in ("n02", "n03"):
        names = [e["event"] for e in events(logs[name]) if e["event"] in ("RX", "DELIVER")]
        assert names.index("RX") < names.index("DELIVER")


async def test_flipping_one_payload_byte_gives_bad_sig_and_no_delivery():
    clock, net, ids, nodes, relays, sinks = build_line(1)
    for n in nodes.values():
        await n.start()
    env = ts.Envelope(mid=ts.message_id(ids["n01"].public_key, 0), type="TEXT",
                      sender=ids["n01"].public_key, ttl=3, sequence=0, payload=b"hello")
    frame = bytearray(ts.encode(env, ids["n01"]))
    frame[65] ^= 0x01  # first payload byte
    assert net.inject("n02", bytes(frame), label="n01")
    for _ in range(10):
        await asyncio.sleep(0)
    for n in nodes.values():
        await n.stop()
    assert len(by_event(("".join(sinks["n02"].lines)), "DROP")) == 1
    assert by_event("".join(sinks["n02"].lines), "DROP")[0]["reason"] == "BAD_SIG"
    assert relays["n02"].delivered == []


async def test_ttl_one_is_delivered_but_not_forwarded():
    clock, net, ids, nodes, relays, sinks = build_line(1)
    for n in nodes.values():
        await n.start()
    env = ts.Envelope(mid=ts.message_id(ids["n01"].public_key, 5), type="SOS",
                      sender=ids["n01"].public_key, ttl=1, sequence=5, payload=b"")
    frame = ts.encode(env, ids["n01"])
    net.inject("n02", frame, label="n01")
    for _ in range(10):
        await asyncio.sleep(0)
    for n in nodes.values():
        await n.stop()
    assert len(relays["n02"].delivered) == 1
    assert len(by_event("".join(sinks["n02"].lines), "TX")) == 0
    assert len(relays["n03"].delivered) == 0


async def test_unsigned_forged_frame_is_dropped():
    clock, net, ids, nodes, relays, sinks = build_line(1)
    for n in nodes.values():
        await n.start()
    forged = bytearray(b"\x01\x03\x03\x03" + b"\x00" * 61 + b"\x00" * 64)
    net.inject("n02", bytes(forged), label="mallory")
    for _ in range(10):
        await asyncio.sleep(0)
    for n in nodes.values():
        await n.stop()
    drops = by_event("".join(sinks["n02"].lines), "DROP")
    assert drops and drops[0]["reason"] in ("BAD_SIG", "MALFORMED")
    assert relays["n02"].delivered == []


async def test_all_logs_pass_assert_valid_log():
    logs, _, _ = await run_sos()
    for text in logs.values():
        assert_valid_log(text.splitlines())


async def test_same_seed_gives_byte_identical_logs():
    first, *_ = await run_sos(seed=42)
    second, *_ = await run_sos(seed=42)
    for name in ("n01", "n02", "n03"):
        assert first[name] == second[name]


async def test_fifty_percent_loss_fixed_seed_is_reproducible_and_valid():
    first, *_ = await run_sos(seed=7, loss=0.5)
    second, *_ = await run_sos(seed=7, loss=0.5)
    for name in ("n01", "n02", "n03"):
        assert first[name] == second[name]
        assert_valid_log(first[name].splitlines())


def test_summary_for_integration_chat(capsys):
    logs, _, _ = asyncio.run(run_sos(seed=1))
    lines = ["WP4.6 summary (seed 1, no loss)"]
    for name in ("n01", "n02", "n03"):
        ev = [e["event"] for e in events(logs[name])]
        lines.append(f"{name}: " + ", ".join(f"{k}={ev.count(k)}" for k in sorted(set(ev))))
    print("\n".join(lines))
    assert "n03" in capsys.readouterr().out