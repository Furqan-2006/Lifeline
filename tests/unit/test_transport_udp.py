import asyncio
import socket

import pytest

from lifeline.clock import FakeClock
from lifeline.rng import Rng
from lifeline.transport.base import TransportClosed
from lifeline.transport.udp import TransportBindError, UdpTransport

LOOP = "127.0.0.1"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((LOOP, 0))
        return s.getsockname()[1]
    finally:
        s.close()


def make(port: int, *, bind: str = LOOP, peers=None, broadcast=None, seed: int = 1, **kw) -> UdpTransport:
    return UdpTransport(bind, port, FakeClock(), Rng(seed).child("udp"),
                        broadcast_addr=broadcast, peers=peers, **kw)


async def start(created: list, t: UdpTransport) -> UdpTransport:
    await t.start()
    created.append(t)
    return t


async def wait_for(pred, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() >= end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def drain(t: UdpTransport) -> list[tuple[bytes, str]]:
    out = []
    while t._queue:
        out.append(t._queue.popleft())
    return out


@pytest.fixture
async def open_transports():
    created: list[UdpTransport] = []
    yield created
    for t in created:
        t.close()


# --- construction --------------------------------------------------------------

def test_exactly_one_addressing_mode_required():
    with pytest.raises(ValueError):
        UdpTransport(LOOP, 0, FakeClock(), Rng(1))
    with pytest.raises(ValueError):
        UdpTransport(LOOP, 0, FakeClock(), Rng(1), broadcast_addr="10.0.0.255", peers=[])


def test_invalid_parameters_rejected():
    with pytest.raises(ValueError):
        make(0, peers=[], receiver_loss=1.5)
    with pytest.raises(ValueError):
        make(0, peers=[], max_frame=0)
    with pytest.raises(ValueError):
        make(0, peers=[], queue_limit=0)
    with pytest.raises(ValueError):
        make(70000, peers=[])


async def test_bind_failure_raises_clear_error():
    t = make(free_port(), bind="192.0.2.1", peers=[])  # TEST-NET, not local
    with pytest.raises(TransportBindError, match="cannot bind"):
        await t.start()


# --- loopback exchange ---------------------------------------------------------

async def test_three_transports_exchange_frames(open_transports):
    ports = [free_port() for _ in range(3)]
    ts = []
    for i, p in enumerate(ports):
        peers = [(LOOP, q) for j, q in enumerate(ports) if j != i]
        ts.append(await start(open_transports, make(p, peers=peers)))

    ts[0].send(b"hello")
    f1, src1 = await asyncio.wait_for(ts[1].recv(), 2)
    f2, src2 = await asyncio.wait_for(ts[2].recv(), 2)
    assert (f1, src1) == (b"hello", LOOP)
    assert (f2, src2) == (b"hello", LOOP)


async def test_empty_frame_is_carried(open_transports):
    p1, p2 = free_port(), free_port()
    a = await start(open_transports, make(p1, peers=[(LOOP, p2)]))
    b = await start(open_transports, make(p2, peers=[(LOOP, p1)]))
    a.send(b"")
    frame, _ = await asyncio.wait_for(b.recv(), 2)
    assert frame == b""


async def test_own_frames_are_ignored(open_transports):
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.2", 0))
        probe.close()
    except OSError:
        pytest.skip("127.0.0.2 not available on this host")
    port = free_port()
    t = await start(open_transports, make(
        port, bind="127.0.0.2", peers=[("127.0.0.2", port)], own_addrs=("127.0.0.2",)))
    t.send(b"echo")
    await wait_for(lambda: t.counters["own_ignored"] == 1)
    assert t.counters["delivered"] == 0
    assert drain(t) == []


async def test_oversized_frame_not_sent_and_counted(open_transports):
    p1, p2 = free_port(), free_port()
    sender = await start(open_transports, make(p1, peers=[(LOOP, p2)], max_frame=100))
    receiver = await start(open_transports, make(p2, peers=[(LOOP, p1)]))
    sender.send(b"x" * 101)
    assert sender.counters["oversized_tx"] == 1
    assert sender.counters["sent"] == 0
    sender.send(b"y" * 100)  # exactly the limit is allowed
    frame, _ = await asyncio.wait_for(receiver.recv(), 2)
    assert frame == b"y" * 100


async def test_oversized_received_datagram_discarded(open_transports):
    p1, p2 = free_port(), free_port()
    small = await start(open_transports, make(p1, peers=[(LOOP, p2)], max_frame=50))
    big = await start(open_transports, make(p2, peers=[(LOOP, p1)]))
    big.send(b"z" * 200)
    await wait_for(lambda: small.counters["received"] == 1)
    assert small.counters["oversized_rx"] == 1
    assert small.counters["delivered"] == 0


async def test_full_loss_drops_every_datagram(open_transports):
    p1, p2 = free_port(), free_port()
    sender = await start(open_transports, make(p1, peers=[(LOOP, p2)]))
    receiver = await start(open_transports, make(p2, peers=[], receiver_loss=1.0))
    for i in range(5):
        sender.send(bytes([i]))
    await wait_for(lambda: receiver.counters["received"] == 5)
    assert receiver.counters["lost_to_loss"] == 5
    assert receiver.counters["delivered"] == 0


async def test_zero_loss_delivers_every_datagram(open_transports):
    p1, p2 = free_port(), free_port()
    sender = await start(open_transports, make(p1, peers=[(LOOP, p2)]))
    receiver = await start(open_transports, make(p2, peers=[], receiver_loss=0.0))
    for i in range(5):
        sender.send(bytes([i]))
    await wait_for(lambda: receiver.counters["delivered"] == 5)
    assert receiver.counters["lost_to_loss"] == 0


# --- deterministic drop pattern (no sockets) ------------------------------------

def drop_pattern(seed: int, loss: float, n: int = 300) -> list[int]:
    t = UdpTransport(LOOP, 0, FakeClock(), Rng(seed).child("node"), peers=[],
                     receiver_loss=loss)
    for i in range(n):
        t._on_datagram(i.to_bytes(2, "big"), "10.0.0.9")
    return [int.from_bytes(frame, "big") for frame, _ in drain(t)]


def test_fixed_seed_gives_same_drop_pattern_twice():
    assert drop_pattern(7, 0.3) == drop_pattern(7, 0.3)


def test_another_seed_gives_a_different_drop_pattern():
    assert drop_pattern(7, 0.3) != drop_pattern(8, 0.3)


def test_loss_rate_is_roughly_configured():
    kept = len(drop_pattern(7, 0.3))
    assert 300 * 0.5 < kept < 300 * 0.9


def test_queue_overflow_drops_newest_and_counts():
    t = UdpTransport(LOOP, 0, FakeClock(), Rng(1), peers=[], queue_limit=2)
    for i in range(4):
        t._on_datagram(bytes([i]), "10.0.0.9")
    assert [f for f, _ in drain(t)] == [b"\x00", b"\x01"]
    assert t.counters["dropped_full"] == 2


# --- send path -----------------------------------------------------------------

async def test_broadcast_mode_sends_one_datagram_to_broadcast_address(open_transports, monkeypatch):
    port = free_port()
    t = await start(open_transports, make(port, broadcast="10.0.0.255"))
    calls = []
    monkeypatch.setattr(t._udp, "sendto", lambda data, addr=None: calls.append((data, addr)))
    t.send(b"b")
    assert calls == [(b"b", ("10.0.0.255", port))]


async def test_socket_error_on_send_is_swallowed_and_counted(open_transports, monkeypatch):
    t = await start(open_transports, make(free_port(), peers=[(LOOP, free_port())]))

    def boom(data, addr=None):
        raise OSError(105, "No buffer space available")

    monkeypatch.setattr(t._udp, "sendto", boom)
    t.send(b"x")  # must not raise
    assert t.counters["send_errors"] == 1
    assert t.counters["sent"] == 0


async def test_error_received_callback_is_counted(open_transports):
    t = await start(open_transports, make(free_port(), peers=[]))
    t._udp.get_protocol().error_received(OSError("icmp unreachable"))
    assert t.counters["socket_errors"] == 1


def test_send_before_start_is_counted_not_raised():
    t = make(free_port(), peers=[(LOOP, free_port())])
    t.send(b"early")
    assert t.counters["send_errors"] == 1


# --- close ----------------------------------------------------------------------

async def test_close_wakes_blocked_recv_with_transport_closed(open_transports):
    t = await start(open_transports, make(free_port(), peers=[]))
    task = asyncio.create_task(t.recv())
    await asyncio.sleep(0)
    t.close()
    with pytest.raises(TransportClosed):
        await asyncio.wait_for(task, 2)


async def test_close_is_idempotent_and_releases_socket(open_transports):
    t = await start(open_transports, make(free_port(), peers=[]))
    t.close()
    t.close()
    assert t._udp.is_closing()


async def test_recv_after_close_raises(open_transports):
    t = await start(open_transports, make(free_port(), peers=[]))
    t.close()
    with pytest.raises(TransportClosed):
        await t.recv()


async def test_send_after_close_is_silent_noop(open_transports):
    t = await start(open_transports, make(free_port(), peers=[(LOOP, free_port())]))
    t.close()
    before = dict(t.counters)
    t.send(b"ignored")  # must not raise
    assert t.counters == before


def test_close_without_start_is_safe():
    make(free_port(), peers=[]).close()