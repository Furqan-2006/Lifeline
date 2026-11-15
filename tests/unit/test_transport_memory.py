import asyncio

import pytest

from lifeline.clock import FakeClock
from lifeline.rng import Rng
from lifeline.transport.base import Transport, TransportClosed
from lifeline.transport.memory import InMemoryNetwork


def make_net(seed: int = 1, **kwargs) -> tuple[FakeClock, InMemoryNetwork]:
    clock = FakeClock()
    return clock, InMemoryNetwork(clock, Rng(seed).child("net"), **kwargs)


def line_net(seed: int = 1, **kwargs):
    clock, net = make_net(seed, **kwargs)
    t = {n: net.create_transport(n) for n in ("n01", "n02", "n03")}
    net.set_adjacency({"n01": {"n02"}, "n02": {"n01", "n03"}, "n03": {"n02"}})
    return clock, net, t


async def drain(transport) -> list[tuple[bytes, str]]:
    """Return everything currently queued without blocking."""
    out = []
    while transport._queue:
        out.append(transport._queue.popleft())
    return out


def test_transport_is_abstract():
    with pytest.raises(TypeError):
        Transport()  # type: ignore[abstract]


async def test_three_node_line_n01_reaches_n02_only():
    _, net, t = line_net()
    t["n01"].send(b"hello")
    assert await drain(t["n02"]) == [(b"hello", "n01")]
    assert await drain(t["n03"]) == []
    assert await drain(t["n01"]) == []


async def test_broadcast_reaches_exactly_the_linked_neighbors():
    _, net, t = line_net()
    t["n02"].send(b"x")
    assert await drain(t["n01"]) == [(b"x", "n02")]
    assert await drain(t["n03"]) == [(b"x", "n02")]
    assert await drain(t["n02"]) == []


async def test_no_self_delivery_even_with_self_link_requested():
    clock, net = make_net()
    t = net.create_transport("solo")
    net.set_adjacency({"solo": {"solo"}})
    t.send(b"echo")
    assert await drain(t) == []


async def test_set_link_rejects_self_link():
    _, net = make_net()
    net.create_transport("a")
    with pytest.raises(ValueError):
        net.set_link("a", "a")


async def test_asymmetric_link_only_delivers_one_way():
    clock, net = make_net()
    a = net.create_transport("a")
    b = net.create_transport("b")
    net.set_link("a", "b", bidirectional=False)
    a.send(b"ab")
    b.send(b"ba")
    assert await drain(b) == [(b"ab", "a")]
    assert await drain(a) == []


async def test_neighbors_of_reports_outgoing_links():
    _, net, _ = line_net()
    assert net.neighbors_of("n01") == {"n02"}
    assert net.neighbors_of("n02") == {"n01", "n03"}


async def test_loss_one_drops_everything_and_counts_it():
    clock, net, t = line_net(default_loss=1.0)
    for _ in range(5):
        t["n01"].send(b"x")
    assert await drain(t["n02"]) == []
    assert net.counters.lost_to_loss == 5
    assert t["n01"].counters.lost_to_loss == 5
    assert net.counters.delivered == 0


async def test_loss_zero_delivers_everything():
    clock, net, t = line_net(default_loss=0.0)
    for _ in range(5):
        t["n01"].send(b"x")
    assert len(await drain(t["n02"])) == 5
    assert net.counters.lost_to_loss == 0


def _delivery_set(seed: int) -> list[tuple[str, int]]:
    """Send 200 frames n02 -> {n01, n03} at loss 0.3; record which arrived."""

    async def run():
        clock, net, t = line_net(seed=seed, default_loss=0.3)
        for i in range(200):
            t["n02"].send(i.to_bytes(2, "big"))
        out = []
        for name in ("n01", "n03"):
            for frame, _ in await drain(t[name]):
                out.append((name, int.from_bytes(frame, "big")))
        return out

    return asyncio.run(run())


def test_loss_is_reproducible_for_a_fixed_seed():
    assert _delivery_set(7) == _delivery_set(7)


def test_loss_differs_for_another_seed():
    assert _delivery_set(7) != _delivery_set(8)


def test_loss_is_roughly_the_configured_rate():
    received = len(_delivery_set(7))
    total = 200 * 2
    assert 0.5 * total < received < 0.9 * total


def test_loss_does_not_depend_on_other_pairs_traffic():
    async def run(extra_traffic: bool):
        clock, net, t = line_net(seed=3, default_loss=0.3)
        if extra_traffic:
            for _ in range(50):
                t["n03"].send(b"noise")
        for i in range(100):
            t["n02"].send(i.to_bytes(2, "big"))
        return [int.from_bytes(f, "big") for f, _ in await drain(t["n01"])]

    assert asyncio.run(run(False)) == asyncio.run(run(True))


async def test_delay_zero_delivers_immediately_in_send_order():
    clock, net, t = line_net(default_delay_ms=0.0)
    for i in range(3):
        t["n01"].send(bytes([i]))
    assert [f for f, _ in await drain(t["n02"])] == [b"\x00", b"\x01", b"\x02"]


async def test_delayed_frames_arrive_in_due_order_under_fake_clock():
    clock, net = make_net()
    a = net.create_transport("a")
    b = net.create_transport("b")
    net.set_link("a", "b", delay_ms=100.0)

    a.send(b"first")
    await asyncio.sleep(0)
    await clock.advance(0.05)
    a.send(b"second")
    await asyncio.sleep(0)
    assert await drain(b) == []

    await clock.advance(0.06)  # t=0.11: first due at 0.1
    assert await drain(b) == [(b"first", "a")]

    await clock.advance(0.1)  # t=0.21: second due at 0.15
    assert await drain(b) == [(b"second", "a")]


async def test_delayed_frame_not_delivered_before_its_due_time():
    clock, net = make_net()
    a = net.create_transport("a")
    b = net.create_transport("b")
    net.set_link("a", "b", delay_ms=500.0)
    a.send(b"x")
    await asyncio.sleep(0)
    await clock.advance(0.499)
    assert await drain(b) == []
    await clock.advance(0.001)
    assert await drain(b) == [(b"x", "a")]


async def test_link_removed_while_frame_in_flight_drops_it():
    clock, net = make_net()
    a = net.create_transport("a")
    b = net.create_transport("b")
    net.set_link("a", "b", delay_ms=200.0)
    a.send(b"doomed")
    await asyncio.sleep(0)
    net.set_link("a", "b", up=False)
    await clock.advance(1.0)
    assert await drain(b) == []
    assert net.counters.dropped_link_down == 1


async def test_topology_change_mid_run_affects_later_sends_only():
    _, net, t = line_net()
    t["n02"].send(b"before")
    net.set_adjacency({"n01": {"n02"}, "n02": {"n01"}, "n03": set()})
    t["n02"].send(b"after")
    assert [f for f, _ in await drain(t["n01"])] == [b"before", b"after"]
    assert await drain(t["n03"]) == [(b"before", "n02")]


async def test_set_adjacency_replaces_all_links():
    _, net, _ = line_net()
    net.set_adjacency({"n01": {"n03"}})
    assert net.neighbors_of("n01") == {"n03"}
    assert net.neighbors_of("n02") == set()


async def test_set_link_up_false_removes_both_directions():
    _, net, _ = line_net()
    net.set_link("n01", "n02", up=False)
    assert net.neighbors_of("n01") == set()
    assert "n01" not in net.neighbors_of("n02")


async def test_queue_overflow_drops_newest_and_counts():
    clock, net, t = line_net(queue_limit=3)
    for i in range(5):
        t["n01"].send(bytes([i]))
    kept = [f for f, _ in await drain(t["n02"])]
    assert kept == [b"\x00", b"\x01", b"\x02"]
    assert t["n02"].counters.dropped_full == 2
    assert net.counters.dropped_full == 2


async def test_inject_puts_frame_into_one_receiver_with_label():
    _, net, t = line_net()
    assert net.inject("n03", b"forged", label="mallory")
    assert await drain(t["n03"]) == [(b"forged", "mallory")]
    assert await drain(t["n02"]) == []


async def test_inject_ignores_topology_and_loss():
    _, net, t = line_net(default_loss=1.0)
    net.set_adjacency({})
    assert net.inject("n01", b"replayed")
    assert [f for f, _ in await drain(t["n01"])] == [b"replayed"]


async def test_inject_respects_queue_bound():
    _, net, t = line_net(queue_limit=1)
    assert net.inject("n01", b"a")
    assert not net.inject("n01", b"b")
    assert net.counters.dropped_full == 1


async def test_recv_returns_frame_and_label():
    _, net, t = line_net()
    t["n01"].send(b"hi")
    frame, label = await t["n02"].recv()
    assert (frame, label) == (b"hi", "n01")


async def test_recv_waits_until_a_frame_arrives():
    _, net, t = line_net()
    task = asyncio.create_task(t["n02"].recv())
    await asyncio.sleep(0)
    assert not task.done()
    t["n01"].send(b"late")
    assert await asyncio.wait_for(task, 1.0) == (b"late", "n01")


async def test_close_wakes_blocked_recv_with_transport_closed():
    _, net, t = line_net()
    task = asyncio.create_task(t["n02"].recv())
    await asyncio.sleep(0)
    t["n02"].close()
    with pytest.raises(TransportClosed):
        await asyncio.wait_for(task, 1.0)


async def test_recv_after_close_raises():
    _, net, t = line_net()
    t["n02"].close()
    with pytest.raises(TransportClosed):
        await t["n02"].recv()


async def test_send_after_close_is_silent_noop():
    _, net, t = line_net()
    t["n02"].close()
    t["n01"].send(b"ignored")  # must not raise
    t["n02"].send(b"also ignored")
    assert net.counters.sent == 1  # only n01's send was counted at all


async def test_close_is_idempotent():
    _, net, t = line_net()
    t["n01"].close()
    t["n01"].close()


async def test_close_cancels_pending_delayed_deliveries_for_that_node():
    clock, net = make_net()
    a = net.create_transport("a")
    b = net.create_transport("b")
    net.set_link("a", "b", delay_ms=100.0)
    a.send(b"x")
    await asyncio.sleep(0)
    b.close()
    await clock.advance(1.0)
    assert net.counters.delivered == 0


async def test_duplicate_names_rejected():
    _, net = make_net()
    net.create_transport("dup")
    with pytest.raises(ValueError):
        net.create_transport("dup")


async def test_frames_of_any_length_carried_untouched():
    _, net, t = line_net(queue_limit=10)
    payloads = [b"", b"\x00", bytes(range(256)), b"\xff" * 1500]
    for p in payloads:
        t["n01"].send(p)
    assert [f for f, _ in await drain(t["n02"])] == payloads


async def test_sender_buffer_mutation_after_send_does_not_affect_receiver():
    _, net, t = line_net()
    buf = bytearray(b"abc")
    t["n01"].send(bytes(buf))
    buf[0] = ord("z")
    assert [f for f, _ in await drain(t["n02"])] == [b"abc"]


async def test_inject_copies_frame_bytes():
    _, net, t = line_net()
    buf = bytearray(b"abc")
    net.inject("n02", bytes(buf))
    buf[0] = ord("z")
    assert [f for f, _ in await drain(t["n02"])] == [b"abc"]


def test_network_rejects_invalid_parameters():
    clock = FakeClock()
    rng = Rng(1)
    with pytest.raises(ValueError):
        InMemoryNetwork(clock, rng, default_loss=1.5)
    with pytest.raises(ValueError):
        InMemoryNetwork(clock, rng, default_delay_ms=-1)
    with pytest.raises(ValueError):
        InMemoryNetwork(clock, rng, queue_limit=0)