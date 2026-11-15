"""WP4.9 fuzz: random frames through a Node. No crash; every frame is logged."""

import asyncio
import json
import random

from lifeline.clock import FakeClock
from lifeline.config import NodeConfig
from lifeline.eventlog import EventLogger, MemorySink
from lifeline.logschema import assert_valid_log
from lifeline.node import Node
from lifeline.rng import Rng
from lifeline.transport.memory import InMemoryNetwork
from tests.harness.temp_standins import Identity, TempCodec


async def test_one_thousand_random_frames_never_crash_and_are_all_logged():
    clock = FakeClock()
    net = InMemoryNetwork(clock, Rng(99).child("net"), queue_limit=5000)
    sink = MemorySink()
    logger = EventLogger("n01", clock, sink=sink)
    ident = Identity()
    t = net.create_transport("n01")
    node = Node("n01", NodeConfig(), t, clock, Rng(99).child("n01"), logger,
                TempCodec(ident), identity=ident)
    seen = []
    node.add_handler(lambda env, tx: seen.append(env))
    await node.start()

    gen = random.Random(1234)  # fuzz input only; not protocol randomness
    for _ in range(1000):
        length = gen.randint(0, 400)
        net.inject("n01", bytes(gen.getrandbits(8) for _ in range(length)), label="fuzz")
    for _ in range(20):
        await asyncio.sleep(0)
    await node.stop()

    text = "".join(sink.lines)
    events = [json.loads(line) for line in text.splitlines() if line]
    rx = sum(1 for e in events if e["event"] == "RX")
    drops = sum(1 for e in events if e["event"] == "DROP")
    assert rx + drops == 1000, (rx, drops)
    assert_valid_log(text.splitlines())