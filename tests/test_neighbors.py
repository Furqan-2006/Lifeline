"""WP5 checks: neighbors are learned from signed HELLOs and expire correctly.

    python -m pytest tests/test_neighbors.py -v
"""

import asyncio
import hashlib
import json

from lifeline.protocol.codec import CODEC, build_envelope
from lifeline.protocol.crypto import identity_from_seed
from lifeline.protocol.envelope import MsgType
from lifeline.runtime.clock import FakeClock
from lifeline.runtime.config import NodeConfig
from lifeline.runtime.eventlog import EventLogger, MemorySink
from lifeline.runtime.logschema import assert_valid_log
from lifeline.runtime.node import Node
from lifeline.runtime.rng import Rng
from lifeline.services.neighbors import NODE_CLASS_RELAY, NeighborManager
from lifeline.transport.memory import InMemoryNetwork


class _Harness:
    """Named nodes on one in-memory network, each with a neighbor manager."""

    def __init__(self, names, classes=None):
        classes = classes or {}
        self.clock = FakeClock()
        self.net = InMemoryNetwork(self.clock, Rng(1).child("net"))
        self.ids, self.sinks, self.nodes, self.mgrs = {}, {}, {}, {}
        for name in names:
            self.ids[name] = identity_from_seed(hashlib.sha256(name.encode()).digest())
            self.sinks[name] = MemorySink()
            self.nodes[name] = Node(
                name, NodeConfig(), self.net.create_transport(name), self.clock,
                Rng(1).child(name), EventLogger(name, self.clock, sink=self.sinks[name]),
                CODEC, identity=self.ids[name],
            )
            self.mgrs[name] = NeighborManager(self.nodes[name], node_class=classes.get(name, 0))

    async def start(self):
        for node in self.nodes.values():
            await node.start()
        for mgr in self.mgrs.values():
            mgr.start()
        # Let every timer task run once so its first sleep is registered with the
        # clock. Without this, the first advance() jumps over the first beacons.
        await self.clock.advance(0.0)

    async def stop(self):
        for node in self.nodes.values():
            await node.stop()  # flushes the logs into the sinks


def _lines(sink):
    """One JSON string per event, whether the sink stored lines or flushed chunks."""
    return [line for chunk in sink.lines for line in chunk.splitlines() if line]


def _events(sink, name=None):
    parsed = [json.loads(line) for line in _lines(sink)]
    return [e for e in parsed if name is None or e["event"] == name]


def _neighbor_events(sink):
    return [e["event"] for e in _events(sink) if e["event"].startswith("NEIGHBOR")]


def test_learns_each_other_and_reports_a_new_neighbor_once():
    async def scenario():
        h = _Harness(("n01", "n02"))
        h.net.set_link("n01", "n02")
        seen = []
        h.mgrs["n01"].on_new_neighbor(seen.append)
        await h.start()
        await h.clock.advance(5.0)
        record = h.mgrs["n01"].get(h.ids["n02"].node_id)
        known = {n: [x.node_id for x in h.mgrs[n].neighbors()] for n in h.mgrs}
        await h.stop()
        return h, seen, record, known

    h, seen, record, known = asyncio.run(scenario())
    assert known["n01"] == [h.ids["n02"].node_id]
    assert known["n02"] == [h.ids["n01"].node_id]
    assert len(seen) == 1 and seen[0].node_id == h.ids["n02"].node_id
    assert record is not None
    assert record.public_key == h.ids["n02"].public_key
    assert record.node_class == 0
    assert 0 < record.last_heard <= 5.0
    assert _neighbor_events(h.sinks["n01"]) == ["NEIGHBOR_UP"]  # refreshes do not log


def test_first_hello_is_sent_at_startup_not_after_an_interval():
    async def scenario():
        h = _Harness(("n01", "n02"))
        h.net.set_link("n01", "n02")
        await h.start()
        await h.clock.advance(0.0)  # no time passes: only the startup beacons can have arrived
        known = len(h.mgrs["n01"].neighbors()), len(h.mgrs["n02"].neighbors())
        await h.stop()
        return known

    assert asyncio.run(scenario()) == (1, 1)


def test_relay_class_is_recorded():
    async def scenario():
        h = _Harness(("n01", "n02"), classes={"n01": NODE_CLASS_RELAY})
        h.net.set_link("n01", "n02")
        await h.start()
        await h.clock.advance(3.0)
        record = h.mgrs["n02"].get(h.ids["n01"].node_id)
        await h.stop()
        return record

    record = asyncio.run(scenario())
    assert record is not None and record.node_class == NODE_CLASS_RELAY


def test_neighbor_expires_after_missed_hellos_not_before():
    async def scenario():
        h = _Harness(("n01", "n02"))
        h.net.set_link("n01", "n02")
        await h.start()
        await h.clock.advance(3.0)
        known_before = len(h.mgrs["n01"].neighbors())
        h.net.set_link("n01", "n02", up=False)
        await h.clock.advance(2.0)
        still_known = len(h.mgrs["n01"].neighbors())  # 2 s of silence: under the 6 s timeout
        await h.clock.advance(8.0)
        gone = h.mgrs["n01"].neighbors()
        await h.stop()
        return h, known_before, still_known, gone

    h, known_before, still_known, gone = asyncio.run(scenario())
    assert known_before == 1
    assert still_known == 1
    assert gone == []
    assert _neighbor_events(h.sinks["n01"]) == ["NEIGHBOR_UP", "NEIGHBOR_DOWN"]
    for sink in h.sinks.values():
        assert_valid_log(_lines(sink))


def test_partition_then_heal_gives_down_then_up_again():
    async def scenario():
        h = _Harness(("n01", "n02"))
        h.net.set_link("n01", "n02")
        seen = []
        h.mgrs["n01"].on_new_neighbor(seen.append)
        await h.start()
        await h.clock.advance(3.0)
        h.net.set_link("n01", "n02", up=False)
        await h.clock.advance(10.0)
        h.net.set_link("n01", "n02")
        await h.clock.advance(5.0)
        back = len(h.mgrs["n01"].neighbors())
        await h.stop()
        return h, seen, back

    h, seen, back = asyncio.run(scenario())
    assert _neighbor_events(h.sinks["n01"]) == ["NEIGHBOR_UP", "NEIGHBOR_DOWN", "NEIGHBOR_UP"]
    assert len(seen) == 2  # the re-offer scheduler hears about the returning neighbor
    assert back == 1


def test_forged_hello_is_dropped_and_creates_no_neighbor():
    async def scenario():
        h = _Harness(("n01", "n02"))  # no link: only the injected frame reaches n02
        await h.start()
        hello = build_envelope(h.ids["n01"], 0, MsgType.HELLO, bytes([0, 0]), ttl=1)
        frame = bytearray(CODEC.encode(hello))
        frame[65] ^= 0x01  # flip the node-class byte after signing
        h.net.inject("n02", bytes(frame))
        await h.clock.advance(0.1)
        table = h.mgrs["n02"].neighbors()
        await h.stop()
        return h, table

    h, table = asyncio.run(scenario())
    log = _lines(h.sinks["n02"])
    assert table == [], log
    reasons = [d["reason"] for d in _events(h.sinks["n02"], "DROP")]
    assert reasons == ["BAD_SIG"], log  # the message shows what n02 actually logged
    assert_valid_log(log)


def test_own_hello_and_non_hello_frames_are_ignored():
    h = _Harness(("n01", "n02"))
    mgr = h.mgrs["n01"]
    own = build_envelope(h.ids["n01"], 0, MsgType.HELLO, bytes([0, 0]), ttl=1)
    sos = build_envelope(h.ids["n02"], 0, MsgType.SOS, b"x" * 9, ttl=16)
    mgr.on_envelope(own, "n01")
    mgr.on_envelope(sos, "n02")
    assert mgr.neighbors() == []


def test_hello_with_wrong_payload_length_is_malformed():
    h = _Harness(("n01", "n02"))
    mgr = h.mgrs["n01"]
    bad = build_envelope(h.ids["n02"], 0, MsgType.HELLO, b"\x00\x00\x00", ttl=1)
    mgr.on_envelope(bad, "n02")
    h.nodes["n01"].ctx.log.flush()
    assert mgr.neighbors() == []
    assert [e["reason"] for e in _events(h.sinks["n01"], "DROP")] == ["MALFORMED"]