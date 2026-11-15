import asyncio
import json
import logging

import pytest

from lifeline.clock import FakeClock
from lifeline.config import NodeConfig
from lifeline.eventlog import EventLogger, MemorySink
from lifeline.logschema import assert_valid_log
from lifeline.node import SEQ_BLOCK, SEQ_MAX, Node
from lifeline.rng import Rng
from lifeline.transport.memory import InMemoryNetwork
from tests.harness.temp_standins import TempCodec, TempEnvelope, temp_mid

SENDER = b"\x11" * 32


def make_env(seq: int, *, ttl: int = 3, payload: bytes = b"hi") -> TempEnvelope:
    return TempEnvelope(mid=temp_mid(SENDER, seq), type="TEXT", sender=SENDER, ttl=ttl, payload=payload)


class Identity:
    def __init__(self) -> None:
        self.public_key = SENDER
        self.node_id = b"\x22" * 16


def build(name="n01", *, clock=None, transport=None, state_dir=None, identity=None, config=None):
    clock = clock or FakeClock()
    net = InMemoryNetwork(clock, Rng(1).child("net"))
    transport = transport or net.create_transport(name)
    sink = MemorySink()
    logger = EventLogger(name, clock, sink=sink)
    node = Node(
        name,
        config or NodeConfig(),
        transport,
        clock,
        Rng(1).child(name),
        logger,
        TempCodec(),
        identity=identity,
        state_dir=state_dir,
    )
    return node, net, clock, sink


def log_events(sink: MemorySink, logger_flush=None) -> list[dict]:
    return [json.loads(line) for chunk in sink.lines for line in chunk.splitlines()]


async def settle(clock: FakeClock, turns: int = 5) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


# --- lifecycle ----------------------------------------------------------------

async def test_start_and_stop_cleanly():
    node, net, clock, sink = build()
    await node.start()
    await node.stop()
    assert node._receive_task is not None and node._receive_task.done()


async def test_stop_twice_is_harmless():
    node, *_ = build()
    await node.start()
    await node.stop()
    await node.stop()


async def test_start_twice_is_harmless():
    node, *_ = build()
    await node.start()
    await node.start()
    await node.stop()


async def test_stop_closes_transport_and_flushes_log():
    node, net, clock, sink = build()
    await node.start()
    node._log.log("ACK_RX", mid=b"\x00" * 8)
    await node.stop()
    assert len(sink.lines) == 1
    assert node._transport._closed


async def test_run_forever_returns_after_stop():
    node, *_ = build()
    task = asyncio.create_task(node.run_forever())
    await settle(FakeClock())
    await node.stop()
    await asyncio.wait_for(task, 1.0)


def test_node_identity_properties_come_from_identity():
    node, *_ = build(identity=Identity())
    assert node.public_key == SENDER
    assert node.node_id == b"\x22" * 16


def test_node_identity_properties_are_none_without_identity():
    node, *_ = build()
    assert node.public_key is None
    assert node.node_id is None


def test_context_exposes_shared_state():
    node, *_ = build(identity=Identity())
    assert node.ctx.name == "n01"
    assert node.ctx.codec is not None
    assert node.ctx.identity is not None
    assert node.ctx.egress == node.transmit


# --- frames in and out --------------------------------------------------------

async def test_malformed_frame_logs_drop_and_next_frame_still_processed():
    node, net, clock, sink = build("n02")
    seen = []
    node.add_handler(lambda env, tx: seen.append(env.mid))
    await node.start()

    net.inject("n02", b"not json at all")
    net.inject("n02", TempCodec().encode(make_env(1)))
    await settle(clock)
    await node.stop()

    events = log_events(sink)
    drops = [e for e in events if e["event"] == "DROP"]
    assert drops == [{"t": 0.0, "node": "n02", "event": "DROP", "reason": "MALFORMED"}]
    assert seen == [make_env(1).mid]


async def test_rx_log_has_expected_fields():
    node, net, clock, sink = build("n02")
    await node.start()
    frame = TempCodec().encode(make_env(7, ttl=4))
    net.inject("n02", frame, label="n01")
    await settle(clock)
    await node.stop()

    rx = [e for e in log_events(sink) if e["event"] == "RX"][0]
    assert set(rx) == {"t", "node", "event", "mid", "type", "sender", "ttl", "size", "tx"}
    assert rx["mid"] == make_env(7).mid.hex()
    assert rx["type"] == "TEXT"
    assert rx["sender"] == SENDER.hex()
    assert rx["ttl"] == 4
    assert rx["size"] == len(frame)
    assert rx["tx"] == "n01"


async def test_two_handlers_both_called_and_raising_handler_does_not_break_others(caplog):
    node, net, clock, sink = build("n02")
    calls = []

    def bad(env, tx):
        raise RuntimeError("boom")

    node.add_handler(bad)
    node.add_handler(lambda env, tx: calls.append(("a", tx)))
    node.add_handler(lambda env, tx: calls.append(("b", tx)))
    await node.start()

    with caplog.at_level(logging.ERROR, logger="lifeline.node"):
        net.inject("n02", TempCodec().encode(make_env(2)), label="n01")
        await settle(clock)
    await node.stop()

    assert calls == [("a", "n01"), ("b", "n01")]
    assert any("raised" in r.message for r in caplog.records)


async def test_handler_exception_does_not_stop_receive_loop():
    node, net, clock, sink = build("n02")
    seen = []

    def flaky(env, tx):
        if env.mid == make_env(1).mid:
            raise ValueError("first one fails")
        seen.append(env.mid)

    node.add_handler(flaky)
    await node.start()
    net.inject("n02", TempCodec().encode(make_env(1)))
    net.inject("n02", TempCodec().encode(make_env(2)))
    await settle(clock)
    await node.stop()
    assert seen == [make_env(2).mid]


async def test_transmit_sends_and_logs_tx_fields():
    node, net, clock, sink = build("n01")
    peer = net.create_transport("n02")
    net.set_link("n01", "n02")
    env = make_env(3, ttl=5)
    node.transmit(env)
    frame, label = await peer.recv()
    assert label == "n01"
    assert TempCodec().decode(frame).mid == env.mid
    node._log.flush()

    tx = [e for e in log_events(sink) if e["event"] == "TX"][0]
    assert set(tx) == {"t", "node", "event", "mid", "type", "sender", "ttl", "size"}
    assert tx["mid"] == env.mid.hex()
    assert tx["ttl"] == 5
    assert tx["size"] == env.size


async def test_transmit_encode_failure_is_reported_not_raised(caplog):
    node, net, clock, sink = build("n01")

    class Broken:
        def encode(self, env):
            raise RuntimeError("cannot encode")

        def decode(self, data):
            raise AssertionError("unused")

    node._codec = Broken()
    with caplog.at_level(logging.ERROR, logger="lifeline.node"):
        node.transmit(make_env(1))  # must not raise
    assert any("encode failed" in r.message for r in caplog.records)


async def test_transmit_send_failure_is_reported_not_raised(caplog):
    node, net, clock, sink = build("n01")

    def explode(frame):
        raise OSError("no buffer space")

    node._transport.send = explode
    with caplog.at_level(logging.ERROR, logger="lifeline.node"):
        node.transmit(make_env(1))
    assert any("send failed" in r.message for r in caplog.records)


async def test_baseline_and_relay_count_tx_identically():
    """TX is only written in transmit(), so counts depend only on sends."""
    for variant in ("relay", "baseline"):
        node, net, clock, sink = build("n01", config=NodeConfig(variant=variant))
        net.create_transport("n02")
        net.set_link("n01", "n02")
        for seq in range(4):
            node.transmit(make_env(seq))
        node._log.flush()
        tx = [e for e in log_events(sink) if e["event"] == "TX"]
        assert len(tx) == 4


# --- timers -------------------------------------------------------------------

async def test_call_later_fires_once_at_due_time():
    node, net, clock, sink = build()
    fired = []
    await node.start()
    node.call_later(2.0, lambda: fired.append(clock.now()))
    await settle(clock)
    await clock.advance(1.9)
    assert fired == []
    await clock.advance(0.2)
    assert fired == [2.0]
    await clock.advance(10.0)
    assert fired == [2.0]
    await node.stop()


async def test_every_fires_on_schedule_with_offset():
    node, net, clock, sink = build()
    fired = []
    await node.start()
    node.every(2.0, lambda: fired.append(clock.now()), start_offset_s=1.0)
    await settle(clock)
    await clock.advance(7.0)
    assert fired == [1.0, 3.0, 5.0, 7.0]
    await node.stop()


async def test_every_jitter_is_deterministic_and_bounded():
    def run():
        node, net, clock, sink = build()
        fired = []

        async def go():
            await node.start()
            node.every(10.0, lambda: fired.append(clock.now()), jitter_s=5.0)
            await settle(clock)
            await clock.advance(30.0)
            await node.stop()

        return asyncio.get_running_loop().create_task(go()), fired

    async def main():
        t1, first = run()
        await t1
        t2, second = run()
        await t2
        return first, second

    first, second = await main()
    assert first == second                    # same seed, same offsets
    assert 0.0 <= first[0] < 5.0              # first call within the jitter window
    assert len(first) >= 3


async def test_every_stops_on_stop():
    node, net, clock, sink = build()
    fired = []
    await node.start()
    node.every(1.0, lambda: fired.append(clock.now()))
    await settle(clock)
    await clock.advance(2.0)
    await node.stop()
    count = len(fired)
    await clock.advance(10.0)
    assert len(fired) == count


async def test_every_rejects_non_positive_interval():
    node, *_ = build()
    with pytest.raises(ValueError):
        node.every(0.0, lambda: None)


async def test_timer_exception_is_reported_not_fatal(caplog):
    node, net, clock, sink = build()
    fired = []

    def bad():
        fired.append("x")
        raise RuntimeError("timer broke")

    await node.start()
    node.every(1.0, bad)
    with caplog.at_level(logging.ERROR, logger="lifeline.node"):
        await settle(clock)
        await clock.advance(3.0)
    assert len(fired) == 4  # kept running after each failure
    assert any("raised" in r.message for r in caplog.records)
    await node.stop()


async def test_spawn_task_cancelled_on_stop():
    node, net, clock, sink = build()
    started = []

    async def forever():
        started.append(1)
        await clock.sleep(10_000)

    await node.start()
    task = node.spawn(forever(), "forever")
    await settle(clock)
    await node.stop()
    assert task.cancelled()


# --- sequence numbers ---------------------------------------------------------

def test_sequence_numbers_increase_from_zero():
    node, *_ = build()
    assert [node.next_sequence() for _ in range(5)] == [0, 1, 2, 3, 4]


def test_sequence_persists_across_restart_without_reuse(tmp_path):
    first, *_ = build(state_dir=tmp_path)
    used = [first.next_sequence() for _ in range(7)]
    first._log.close()

    second, *_ = build(state_dir=tmp_path)  # simulated restart, no stop()
    resumed = [second.next_sequence() for _ in range(3)]
    assert min(resumed) > max(used)
    assert len(set(used + resumed)) == len(used) + len(resumed)


def test_sequence_survives_crash_without_stop(tmp_path):
    node, *_ = build(state_dir=tmp_path)
    before_crash = [node.next_sequence() for _ in range(SEQ_BLOCK + 5)]  # forces a second reservation
    # no stop(): the object is simply abandoned
    restarted, *_ = build(state_dir=tmp_path)
    after = [restarted.next_sequence() for _ in range(3)]
    assert min(after) > max(before_crash)


def test_state_file_is_written_atomically(tmp_path):
    node, *_ = build(state_dir=tmp_path)
    node.next_sequence()
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["seq_n01.json"]
    assert json.loads((tmp_path / "seq_n01.json").read_text())["high_water"] == SEQ_BLOCK


def test_unreadable_state_file_falls_back_without_crashing(tmp_path, caplog):
    (tmp_path / "seq_n01.json").write_text("{corrupt")
    with caplog.at_level(logging.WARNING, logger="lifeline.node"):
        node, *_ = build(state_dir=tmp_path)
    assert node.next_sequence() == 0
    assert any("unreadable sequence state" in r.message for r in caplog.records)


def test_sequence_exhaustion_raises_clear_error():
    node, *_ = build()
    node._seq_next = SEQ_MAX + 1
    node._seq_reserved_until = SEQ_MAX + 1
    with pytest.raises(OverflowError, match="exhausted"):
        node.next_sequence()


def test_sequence_last_value_is_allowed():
    node, *_ = build()
    node._seq_next = SEQ_MAX
    node._seq_reserved_until = SEQ_MAX + 1
    assert node.next_sequence() == SEQ_MAX


# --- log validity -------------------------------------------------------------

async def test_every_log_line_passes_validation():
    node, net, clock, sink = build("n02")
    peer = net.create_transport("n01")
    net.set_link("n01", "n02")
    await node.start()
    node.add_handler(lambda env, tx: None)

    net.inject("n02", b"garbage")
    net.inject("n02", TempCodec().encode(make_env(1)), label="n01")
    node.transmit(make_env(2))
    await settle(clock)
    await node.stop()

    text = "".join(sink.lines)
    assert_valid_log(text.splitlines())
    assert text  # something was actually written