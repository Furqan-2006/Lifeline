"""Foundation checks: run these before starting WP5. From the repo root:

    python -m pytest tests/test_foundation.py -v
"""

import asyncio

from lifeline.protocol import crypto
from lifeline.protocol.codec import CODEC, build_envelope, sign_envelope, verify_envelope
from lifeline.protocol.envelope import MsgType, encode
from lifeline.runtime.clock import FakeClock
from lifeline.runtime.config import NodeConfig
from lifeline.runtime.context import DecodeError
from lifeline.runtime.eventlog import EventLogger, MemorySink
from lifeline.runtime.logschema import assert_valid_log
from lifeline.runtime.node import Node
from lifeline.runtime.rng import Rng
from lifeline.transport.memory import InMemoryNetwork


def _pair():
    clock = FakeClock()
    net = InMemoryNetwork(clock, Rng(1).child("net"))
    net.set_link("n01", "n02")
    nodes, sinks, ids = {}, {}, {}
    for name in ("n01", "n02"):
        ids[name] = crypto.generate_identity()
        sinks[name] = MemorySink()
        nodes[name] = Node(
            name, NodeConfig(), net.create_transport(name), clock, Rng(1).child(name),
            EventLogger(name, clock, sink=sinks[name]), CODEC, identity=ids[name],
        )
    return clock, net, nodes, sinks, ids


def test_identity_shape():
    ident = crypto.generate_identity()
    assert len(ident.public_key) == 32
    assert len(ident.node_id) == 16
    assert ident.node_id == crypto.node_id_of(ident.public_key)


def test_envelope_names_and_roundtrip():
    ident = crypto.generate_identity()
    env = build_envelope(ident, 7, MsgType.HELLO, bytes([0, 0]), ttl=1)
    frame = CODEC.encode(env)
    assert len(frame) == 131
    back = CODEC.decode(frame)
    assert back.msg_type == MsgType.HELLO and back.msg_id == env.msg_id
    assert back.sender_pubkey == ident.public_key
    assert crypto.node_id_of(back.sender_pubkey) == ident.node_id and back.seq_num == 7
    assert verify_envelope(back)


def test_tampered_frame_fails_verification_and_bad_input_raises_decode_error():
    ident = crypto.generate_identity()
    env = build_envelope(ident, 1, MsgType.HELLO, bytes([0, 0]), ttl=1)
    frame = bytearray(CODEC.encode(env))
    frame[66] ^= 0xFF  # flip a payload byte
    assert not verify_envelope(CODEC.decode(bytes(frame)))
    try:
        CODEC.decode(b"\x01\x02")
    except DecodeError:
        pass
    else:
        raise AssertionError("expected DecodeError")


def test_ttl_does_not_affect_signature():
    ident = crypto.generate_identity()
    env = build_envelope(ident, 1, MsgType.SOS, b"x" * 9, ttl=16)
    lower = CODEC.decode(encode(env))
    lower.ttl = 3
    assert verify_envelope(lower)


def test_seal_open_roundtrip_and_wrong_recipient():
    alice, bob = crypto.generate_identity(), crypto.generate_identity()
    eph, nonce, ct = crypto.seal(bob.public_key, b"hello", b"aad")
    assert crypto.open_msg(bob, eph, nonce, ct, b"aad") == b"hello"
    assert crypto.open_msg(alice, eph, nonce, ct, b"aad") is None
    assert crypto.open_msg(bob, eph, nonce, ct, b"other") is None


def test_node_starts_and_stops():
    async def scenario():
        clock, net, nodes, sinks, ids = _pair()
        for n in nodes.values():
            await n.start()
        for n in nodes.values():
            await n.stop()
    asyncio.run(scenario())


def test_hello_crosses_one_hop_and_logs_match_schema():
    async def scenario():
        clock, net, nodes, sinks, ids = _pair()
        seen = []
        nodes["n02"].add_handler(lambda env, tx: seen.append((env, tx)))
        for n in nodes.values():
            await n.start()
        hello = build_envelope(ids["n01"], nodes["n01"].next_sequence(), MsgType.HELLO,
                               bytes([0, 0]), ttl=1)
        nodes["n01"].transmit(hello)
        await clock.advance(0.1)
        for n in nodes.values():
            await n.stop()
        return seen, sinks

    seen, sinks = asyncio.run(scenario())
    assert len(seen) == 1
    env, tx = seen[0]
    assert tx == "n01" and env.msg_type == MsgType.HELLO and verify_envelope(env)
    for sink in sinks.values():
        assert_valid_log(sink.lines)
    assert any('"event":"TX"' in line for line in sinks["n01"].lines)
    assert any('"event":"RX"' in line for line in sinks["n02"].lines)