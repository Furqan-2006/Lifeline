"""WP4.6 scenario over real UDP loopback (WP4.7 done-when).

Checks counts and ordering only. Logs are not byte-identical here because
real sockets and wall-clock time are not deterministic.

Topology n01 - n02 - n03, via peers lists. Expected counts match the
corrected WP4.6 derivation.
"""

from __future__ import annotations

import asyncio
import json
import socket

from lifeline.clock import SystemClock
from lifeline.config import NodeConfig
from lifeline.eventlog import EventLogger, MemorySink
from lifeline.logschema import assert_valid_log
from lifeline.node import Node
from lifeline.rng import Rng
from lifeline.transport.udp import UdpTransport
from tests.harness import temp_standins as ts
from tests.harness.toy_relay import ToyRelay, VerifyingCodec

LOOP = "127.0.0.1"
NAMES = ("n01", "n02", "n03")
LINKS = {"n01": ("n02",), "n02": ("n01", "n03"), "n03": ("n02",)}


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((LOOP, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line]


def by_event(text: str, name: str) -> list[dict]:
    return [e for e in events(text) if e["event"] == name]


async def wait_until(pred, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() >= end:
            raise AssertionError("timed out waiting for the line to settle")
        await asyncio.sleep(0.01)


async def run_udp_line(seed: int = 1):
    clock = SystemClock()
    ports = {n: free_port() for n in NAMES}
    ids = {n: ts.Identity() for n in NAMES}
    sinks = {n: MemorySink() for n in NAMES}
    transports, nodes, relays = {}, {}, {}

    for n in NAMES:
        peers = [(LOOP, ports[m]) for m in LINKS[n]]
        t = UdpTransport(LOOP, ports[n], clock, Rng(seed).child(n), peers=peers)
        await t.start()
        transports[n] = t
        logger = EventLogger(n, clock, sink=sinks[n])
        codec = VerifyingCodec(ts.TempCodec(ids[n]), logger)
        node = Node(n, NodeConfig(), t, clock, Rng(seed).child(n), logger, codec,
                    identity=ids[n])
        nodes[n] = node
        relays[n] = ToyRelay(node, ids[n].node_id)

    for n in NAMES:
        await nodes[n].start()

    env = ts.Envelope(mid=ts.message_id(ids["n01"].public_key, 0), type="SOS",
                      sender=ids["n01"].public_key, ttl=3, sequence=0,
                      payload=b"\x00" * 9)
    relays["n01"].mark_origin(env)
    nodes["n01"].transmit(env)

    # Settle: n01 hears n02's forward, n02 hears n01 and n03, n03 hears n02.
    await wait_until(lambda: transports["n01"].counters["received"] >= 1
                     and transports["n02"].counters["received"] >= 2
                     and transports["n03"].counters["received"] >= 1)
    await asyncio.sleep(0.1)  # let the nodes process the last datagrams

    for n in NAMES:
        await nodes[n].stop()
    logs = {n: "".join(sinks[n].lines) for n in NAMES}
    return logs, relays, ids


async def test_udp_three_node_line_counts_and_ordering():
    logs, relays, ids = await run_udp_line()

    assert len(by_event(logs["n01"], "TX")) == 1
    assert len(by_event(logs["n01"], "RX")) == 1
    assert len(by_event(logs["n01"], "DELIVER")) == 0
    assert [e["reason"] for e in by_event(logs["n01"], "DROP")] == ["DUPLICATE"]

    assert len(by_event(logs["n02"], "TX")) == 1
    assert len(by_event(logs["n02"], "RX")) == 2
    assert len(by_event(logs["n02"], "DELIVER")) == 1
    assert [e["reason"] for e in by_event(logs["n02"], "DROP")] == ["DUPLICATE"]

    assert len(by_event(logs["n03"], "TX")) == 1
    assert len(by_event(logs["n03"], "RX")) == 1
    assert len(by_event(logs["n03"], "DELIVER")) == 1


async def test_udp_n03_receives_reduced_ttl_with_valid_signature():
    logs, relays, ids = await run_udp_line()
    delivered = relays["n03"].delivered
    assert len(delivered) == 1
    env = delivered[0]
    assert env.sender == ids["n01"].public_key
    assert env.sequence == 0
    assert env.ttl == 2


async def test_udp_rx_precedes_deliver_at_relays():
    logs, _, _ = await run_udp_line()
    for n in ("n02", "n03"):
        names = [e["event"] for e in events(logs[n]) if e["event"] in ("RX", "DELIVER")]
        assert names.index("RX") < names.index("DELIVER")


async def test_udp_all_logs_pass_validation():
    logs, _, _ = await run_udp_line()
    for text in logs.values():
        assert_valid_log(text.splitlines())