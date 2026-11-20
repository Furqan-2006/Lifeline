import asyncio
import json
from cryptography.hazmat.primitives import serialization

from lifeline.clock import FakeClock
from lifeline.config import NodeConfig
from lifeline.crypto import generate_identity, node_id_of, open_msg, seal, sign, verify
from lifeline.envelope import (
    LIFELINE_VERSION,
    DecodeError,
    Envelope,
    MsgType,
    decode,
    encode,
    signed_bytes,
)
from lifeline.eventlog import EventLogger, MemorySink
from lifeline.reoffer import ReofferScheduler
from lifeline.store import MessageStore
from lifeline.text import TextService


def _pub_bytes(identity) -> bytes:
    return identity.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def test_wp2_encode_decode_roundtrip_and_malformed():
    alice = generate_identity()
    bob = generate_identity()
    alice_pub = _pub_bytes(alice)
    bob_id = node_id_of(_pub_bytes(bob))

    env = Envelope(
        version=LIFELINE_VERSION,
        msg_type=MsgType.TEXT,
        priority=2,
        ttl=16,
        msg_id=b"\x11" * 8,
        seq_num=7,
        sender_pubkey=alice_pub,
        recipient_node_id=bob_id,
        payload_len=20,
        ephemeral_pubkey=b"\x22" * 32,
        nonce=b"\x33" * 12,
        payload=b"\x44" * 20,
        signature=b"\x55" * 64,
    )
    frame = encode(env)
    assert len(frame) == 109 + 20 + 64
    assert decode(frame) == env

    try:
        decode(frame[:50])
        assert False, "Expected DecodeError on truncated frame"
    except DecodeError:
        pass

    bad_ttl = bytearray(frame)
    bad_ttl[3] = 0
    try:
        decode(bytes(bad_ttl))
        assert False, "Expected DecodeError on TTL=0"
    except DecodeError:
        pass

    bad_recip = bytearray(frame)
    bad_recip[48:64] = b"\x00" * 16
    try:
        decode(bytes(bad_recip))
        assert False, "Expected DecodeError on broadcast TEXT"
    except DecodeError:
        pass


def test_wp3_sign_verify_and_ecies_seal_open():
    alice = generate_identity()
    bob = generate_identity()
    eve = generate_identity()

    alice_pub = _pub_bytes(alice)
    bob_pub = _pub_bytes(bob)

    env = Envelope(
        version=LIFELINE_VERSION,
        msg_type=MsgType.SOS,
        priority=0,
        ttl=16,
        msg_id=b"\xaa" * 8,
        seq_num=1,
        sender_pubkey=alice_pub,
        recipient_node_id=b"\x00" * 16,
        payload_len=5,
        payload=b"HELP!",
        signature=b"\x00" * 64,
    )
    raw = encode(env)
    sig = sign(alice, signed_bytes(raw))
    env.signature = sig
    raw_signed = encode(env)

    forwarded = bytearray(raw_signed)
    forwarded[3] = 15
    assert verify(alice_pub, signed_bytes(bytes(forwarded)), sig) is True
    assert verify(bob_pub, signed_bytes(bytes(forwarded)), sig) is False
    assert verify(b"\xff" * 32, signed_bytes(bytes(forwarded)), sig) is False

    aad = b"header-without-ttl"
    eph_pub, nonce, ct = seal(bob_pub, b"Meet at Sector 4", aad)
    assert open_msg(bob, eph_pub, nonce, ct, aad) == b"Meet at Sector 4"
    assert open_msg(eve, eph_pub, nonce, ct, aad) is None
    assert open_msg(bob, eph_pub, nonce, ct, b"tampered-aad") is None


def test_wp7_store_priority_vs_baseline():
    alice = generate_identity()
    alice_pub = _pub_bytes(alice)

    def make_env(seq: int, prio: int) -> Envelope:
        return Envelope(
            version=1,
            msg_type=MsgType.SOS if prio == 0 else MsgType.TEXT,
            priority=prio,
            ttl=16,
            msg_id=seq.to_bytes(8, "big"),
            seq_num=seq,
            sender_pubkey=alice_pub,
            recipient_node_id=b"\x01" * 16,
            payload_len=0,
        )

    relay_store = MessageStore(capacity=2, use_priorities=True)
    relay_store.put(make_env(1, 2), now=1.0)
    relay_store.put(make_env(2, 0), now=2.0)
    assert relay_store.put(make_env(3, 0), now=3.0) == [(alice_pub, 1)]
    assert relay_store.contains((alice_pub, 2)) and relay_store.contains((alice_pub, 3))

    base_store = MessageStore(capacity=2, use_priorities=False)
    base_store.put(make_env(1, 0), now=1.0)
    base_store.put(make_env(2, 2), now=2.0)
    assert base_store.put(make_env(3, 2), now=3.0) == [(alice_pub, 1)]


def test_wp7_partition_reoffer_and_end_to_end_ack():
    async def scenario():
        clock = FakeClock(start=0.0)
        cfg = NodeConfig()

        alice = generate_identity()
        bob = generate_identity()
        bob_pub = _pub_bytes(bob)
        bob_id = node_id_of(bob_pub)

        sink_a = MemorySink()
        sink_b = MemorySink()
        log_a = EventLogger("n01", clock, sink=sink_a)
        log_b = EventLogger("n02", clock, sink=sink_b)

        store_a = MessageStore(
            capacity=cfg.buffer_capacity,
            retention_s=cfg.retention_s,
            use_priorities=cfg.use_priorities,
            reoffer_initial_s=cfg.reoffer_initial_s,
        )
        store_b = MessageStore(capacity=cfg.buffer_capacity, retention_s=cfg.retention_s)

        delivered_at_bob = []
        partition_open = False

        def stub_flood_a(env: Envelope) -> None:
            if partition_open:
                rx_env = decode(encode(env))
                text_b.on_deliver(rx_env)

        def stub_flood_b(env: Envelope) -> None:
            if partition_open:
                rx_env = decode(encode(env))
                text_a.on_deliver(rx_env)

        text_a = TextService(alice, cfg, clock, store_a, log_a, submit_to_flood=stub_flood_a)
        text_b = TextService(
            bob,
            cfg,
            clock,
            store_b,
            log_b,
            submit_to_flood=stub_flood_b,
            on_text_received=lambda sender, body: delivered_at_bob.append((sender, body)),
        )
        reoffer_a = ReofferScheduler(
            cfg,
            clock,
            store_a,
            log_a,
            submit_to_flood=stub_flood_a,
            on_expire_check=text_a.expire_retained,
        )

        task = asyncio.create_task(reoffer_a.run())
        await asyncio.sleep(0)  # Let reoffer_a.run() start and register its first sleep

        mid = text_a.send_text(bob_pub, "Bridge collapsed at 5th Ave")
        assert len(store_a.messages) == 1
        assert len(delivered_at_bob) == 0

        await clock.advance(16.0)
        assert len(delivered_at_bob) == 0
        assert store_a.get_record((_pub_bytes(alice), 1)).reoffer_attempts == 2

        partition_open = True
        reoffer_a.on_new_neighbor(bob_id)

        assert len(delivered_at_bob) == 1
        assert delivered_at_bob[0][1] == "Bridge collapsed at 5th Ave"
        assert len(store_a.messages) == 0
        assert mid not in text_a.retained

        reoffer_a.stop()
        task.cancel()
        await task
        log_a.flush()
        log_b.flush()

        events_a = [
            json.loads(line)["event"]
            for line in "".join(sink_a.lines).splitlines()
            if line.strip()
        ]
        assert "TEXT_SEND" in events_a
        assert "REOFFER" in events_a
        assert "ACK_RX" in events_a
        assert "TEXT_END" in events_a

    asyncio.run(scenario())
