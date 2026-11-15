# Lifeline runtime: how to build against the skeleton

This page is enough to write a module, register it on a node, and test it on the in-memory network. You should not need to read `lifeline/node.py`.

## 1. What the runtime already does

Every node writes these events itself. Do not log them again.

| Event | When | Written by |
| --- | --- | --- |
| `RX` | A frame decoded successfully | `Node`, once per frame received |
| `TX` | A frame was handed to the transport | `Node.transmit`, the only egress |
| `DROP` with `reason=MALFORMED` | A frame failed to decode | `Node`, no `mid` |

`DROP` for `BAD_SIG`, `DUPLICATE`, `REPLAY`, `RATE_LIMIT` and `TTL` are the responsibility of the module that makes that decision (M1's flood engine, or the codec wrapper for `BAD_SIG`). `DELIVER`, `TEXT_*`, `ACK_RX`, `BUF_*`, `REOFFER`, `NEIGHBOR_*`, `KEY_LEARNED` and `SOS_STATE` belong to the module that owns the behaviour.

## 2. NodeContext

Modules are built as `Module(ctx)`. `node.ctx` is a frozen dataclass:

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | `str` | Node name, for example `n07` |
| `config` | `NodeConfig` | Parameters, with `use_*` flags for the variant |
| `clock` | `Clock` | `now()` and `await sleep(s)`. Never `time` directly |
| `rng` | `Rng` | This node's random stream. Use `rng.child("yourmodule")` |
| `log` | `EventLogger` | `log(event, **fields)`. Use only for events you own |
| `codec` | `Codec` | `encode`, `decode`. Raises `DecodeError` |
| `identity` | `Identity \| None` | `public_key` (32 B), `node_id` (16 B) |
| `egress` | callable | Same as `node.transmit` |

## 3. Register a handler

A handler is `fn(envelope, tx_label)`. It runs for every frame that decoded successfully, including your own echoed messages, so it must do its own duplicate check.

```python
class Flood:
    def __init__(self, ctx):
        self.ctx = ctx
        self.seen = set()

    def on_envelope(self, env, tx_label):
        key = (env.sender, env.sequence)
        if key in self.seen:
            self.ctx.log.drop(reason="DUPLICATE", mid=env.mid)
            return
        self.seen.add(key)
        # ... your logic ...

# In build_node, after the node exists:
flood = Flood(node.ctx)
node.add_handler(flood.on_envelope)
```

A handler that raises is reported through `logging` and does not stop the others. Do not catch errors you want to see.

## 4. Send with node.transmit

```python
node.transmit(envelope)  # encodes, sends, logs TX
```

`transmit` is the only egress. Never call `transport.send` yourself, or TX counts diverge between Relay and baseline. Pass a fully signed envelope: the runtime does not sign.

## 5. Schedule timers

Use the injected clock only.

```python
node.every(2.0, self.send_hello, start_offset_s=0.0, jitter_s=2.0)   # periodic, jittered
node.call_later(5.0, self.reoffer)                                   # once
node.spawn(self.long_task(), "name")                                 # owned coroutine
```

- `every` draws its first offset from `ctx.rng`, so runs are reproducible.
- Everything is cancelled by `node.stop()`.
- An exception in a timer is logged and the timer keeps running (for `every`) or ends (for `call_later`).

## 6. Sequence numbers

```python
seq = node.next_sequence()   # unsigned 4-byte, never reused
```

Pass a `state_dir` to the node to persist across restarts. Without it, a restart reuses numbers, which breaks duplicate suppression. Numbers are reserved in blocks of 100, so a restart may skip up to 99 values, which is harmless.

## 7. A harness in about ten lines

```python
from lifeline.clock import FakeClock
from lifeline.config import NodeConfig
from lifeline.eventlog import EventLogger, MemorySink
from lifeline.node import Node
from lifeline.rng import Rng
from lifeline.transport.memory import InMemoryNetwork

clock = FakeClock()
net = InMemoryNetwork(clock, Rng(1).child("net"))
sink = MemorySink()
node = Node("n01", NodeConfig(), net.create_transport("n01"), clock, Rng(1).child("n01"),
            EventLogger("n01", clock, sink=sink), codec, identity=identity)
net.set_adjacency({"n01": {"n02"}, "n02": {"n01"}})
await node.start()
# ... drive the test; advance time with: await clock.advance(seconds)
await node.stop()
```

`codec` and `identity` come from M2's module, or from `tests/harness/temp_standins.py` until it merges.

## 8. Contract questions and their resolutions

| # | Question | Resolution in this skeleton | Status |
| --- | --- | --- | --- |
| 1 | `Clock.sleep` is not in C2.2 | Added to the protocol; a one-line change if rejected | Proposed, awaiting M1 and M2 |
| 2 | `Rng.child` derivation not specified | SHA-256 of (parent seed, name) | Proposed |
| 3 | C3 value formats (`type`, `status`, identifiers) | Uppercase type name, `active`/`cancel`, 32-hex NodeID | Proposed, M2 reads the logs |
| 4 | Who logs TX, RX, MALFORMED | The runtime, once per frame | Proposed, affects M1's receive pipeline |
| 5 | Sequence counter ownership | `node.next_sequence()`, persisted in `state_dir` | Proposed |
| 6 | Envelope attribute names (`mid`, `type`, `sender`, `ttl`, `size`) | Attributes on the decoded envelope | Proposed, M2 |
| 7 | `verified_pairs` shape | List of two-element lists of node names | Proposed |
| 8 | `jitter_ms` type | Two-element array, any non-negative numbers | Proposed |
| 9 | UDP parameters not in C4 | Come from the CLI, not the node TOML | Proposed |
| 10 | `start()` on transports | Additive; in-memory needs none | Proposed |
| 11 | Handler and timer exception reporting | `logging` at ERROR; no C3 event | Proposed |

Nothing in the table is accepted yet. Each proposal is isolated in one place in the code, so a rejection is a small change.

## 9. Copy-paste harness for M1: two nodes, one handler

```python
from lifeline.clock import FakeClock
from lifeline.config import NodeConfig
from lifeline.eventlog import EventLogger, MemorySink
from lifeline.node import Node
from lifeline.rng import Rng
from lifeline.transport.memory import InMemoryNetwork

async def test_m1_two_nodes():
    clock = FakeClock()
    net = InMemoryNetwork(clock, Rng(1).child("net"))
    net.set_link("n01", "n02")
    nodes = {}
    for name in ("n01", "n02"):
        nodes[name] = Node(name, NodeConfig(), net.create_transport(name), clock,
                           Rng(1).child(name), EventLogger(name, clock, sink=MemorySink()),
                           codec, identity=identities[name])
    seen = []
    nodes["n02"].add_handler(lambda env, tx: seen.append((env.mid, tx)))
    for n in nodes.values():
        await n.start()
    nodes["n01"].transmit(envelope)   # a signed envelope built with the codec
    await clock.advance(0.1)
    assert seen == [(envelope.mid, "n01")]
    for n in nodes.values():
        await n.stop()
```

(`codec`, `identities`, `envelope` come from the codec module, or from the TEMP stand-in.)

## 10. Copy-paste harness for M2: partition, then delivery

```python
async def test_m2_partition_then_delivery():
    clock = FakeClock()
    net = InMemoryNetwork(clock, Rng(2).child("net"))
    net.set_link("n01", "n02")
    a = Node("n01", NodeConfig(), net.create_transport("n01"), clock, Rng(2).child("n01"),
             EventLogger("n01", clock, sink=MemorySink()), codec, identity=ids["n01"])
    b = Node("n02", NodeConfig(), net.create_transport("n02"), clock, Rng(2).child("n02"),
             EventLogger("n02", clock, sink=MemorySink()), codec, identity=ids["n02"])
    got = []
    b.add_handler(lambda env, tx: got.append(env.mid))
    await a.start(); await b.start()

    net.set_link("n01", "n02", up=False)      # partition
    a.transmit(env_while_partitioned)         # lost: link is down
    await clock.advance(0.1)
    assert got == []

    net.set_link("n01", "n02")                # heal
    a.transmit(env_after_heal)
    await clock.advance(0.1)
    assert got == [env_after_heal.mid]

    await a.stop(); await b.stop()
```

M2's text service would hold the message in its buffer and re-offer it on the heal. That re-offer logic is M2's, so this harness shows only the link transition.

## 11. Known limits

- The production CLI cannot sign until M2's codec and crypto are merged; `main` reports this and exits 2.
- A bind failure leaves an empty but valid log file.
- Run scripts must give each station a unique `--name`, and a port that is free on that station, because a port collision is not detected.