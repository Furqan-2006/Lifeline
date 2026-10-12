# Lifeline: Contracts

**Version:** v0.1-draft (not frozen)
**Purpose:** The four agreements that let three people build modules independently. After the freeze, code is written against this document, and the document changes only through the process in section 6.

| Contract                      | Fixes                                              | Main consumers                                |
| ----------------------------- | -------------------------------------------------- | --------------------------------------------- |
| C1 Envelope                   | Byte layout, types, priorities, signature coverage | Everyone                                      |
| C2 Module interfaces          | Function contracts between modules                 | Everyone                                      |
| C3 Event log schema           | What every node and the controller write to disk   | Metrics, dashboard, tests                     |
| C4 Config and scenario format | Parameters and scenario description                | Node runtime, scenario generator, run scripts |

**Approvals** (write the date when you approve in the pull request):

| Member         | Approved |
| -------------- | -------- |
| Furqan (M1)    | Yes      |
| Abdullah (M2)  |          |
| Abu Bakar (M3) |          |

---

## C1. Envelope

All integers are **big-endian**. All sizes are in bytes. A frame is one envelope in one UDP datagram.

### 1.1 Layout

| Offset        | Size | Field                | Notes                                                                            |
| ------------- | ---- | -------------------- | -------------------------------------------------------------------------------- |
| 0             | 1    | Version              | Always 1                                                                         |
| 1             | 1    | Type                 | Table 1.2                                                                        |
| 2             | 1    | Priority             | Table 1.3                                                                        |
| 3             | 1    | TTL                  | Remaining hops; **excluded from the signature**                                  |
| 4             | 8    | Message ID           | First 8 bytes of SHA-256 of (sender public key, then sequence number as 4 bytes) |
| 12            | 4    | Sequence number      | Per-sender counter, unsigned, persisted across restarts                          |
| 16            | 32   | Sender public key    | Ed25519                                                                          |
| 48            | 16   | Recipient NodeID     | All zeros means broadcast                                                        |
| 64            | 1    | Payload length       | 0 to 160                                                                         |
| 65            | 32   | Ephemeral public key | **TEXT only** (X25519)                                                           |
| 97            | 12   | Nonce                | **TEXT only**                                                                    |
| 65 or 109     | N    | Payload              | Starts at 109 for TEXT, at 65 for every other type                               |
| after payload | 64   | Signature            | Ed25519                                                                          |

**NodeID** is the first 16 bytes of SHA-256 of the sender public key. The largest frame (a TEXT with a full payload) is 109 + 160 + 64 = 333 bytes.

### 1.2 Types

| Value | Type        | Direction                      | Forwarded        |
| ----- | ----------- | ------------------------------ | ---------------- |
| 1     | HELLO       | One hop, broadcast             | Never (TTL is 1) |
| 2     | TEXT        | Unicast to recipient NodeID    | Yes              |
| 3     | SOS         | Broadcast                      | Yes              |
| 4     | ACK         | Unicast to the original sender | Yes              |
| 5     | KEY_REQUEST | Broadcast                      | Yes              |
| 6     | KEY_REPLY   | Unicast to the requester       | Yes              |

### 1.3 Priorities (lower number is more important)

| Value | Used for               |
| ----- | ---------------------- |
| 0     | SOS                    |
| 1     | ACK                    |
| 2     | TEXT                   |
| 3     | KEY_REQUEST, KEY_REPLY |
| 4     | HELLO                  |

### 1.4 Payloads

| Type        | Payload                                                                                                                                                    |
| ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| HELLO       | Node class (1 byte: 0 handheld, 1 relay), capability flags (1 byte, 0 for now)                                                                             |
| TEXT        | Ciphertext with its 16-byte authentication tag; at most 160 bytes, so at most 144 bytes of plaintext                                                       |
| ACK         | The 8-byte Message ID being acknowledged                                                                                                                   |
| SOS         | Latitude (signed 32-bit, degrees times 10 million), longitude (same), status (1 byte: 0 active, 1 cancel), then an optional UTF-8 note of at most 64 bytes |
| KEY_REQUEST | The 16-byte NodeID being asked about                                                                                                                       |
| KEY_REPLY   | The 32-byte public key of the NodeID that was asked about                                                                                                  |

### 1.5 Signature and encryption bytes

The **signed bytes** are the ASCII prefix `LIFELINE-v1`, then bytes 0 to 2 of the frame, then bytes 4 up to the start of the signature. In words: everything except the TTL byte and the signature itself, with a fixed prefix so a signature can never be reused in another context. TTL is excluded because relays must decrement it.

For TEXT, encryption is one-shot: generate an ephemeral X25519 key, derive a shared secret with the recipient's key, derive a 32-byte key with HKDF-SHA256 (salt is the ephemeral public key followed by the recipient public key; info is `lifeline-text-v1`), then encrypt with ChaCha20-Poly1305 using the 12-byte nonce. The **associated data** is header bytes 0 to 64 with the TTL byte removed. M2 owns the crypto module and may refine the derivation details, but not the interface in C2 or the byte layout above.

### 1.6 Validation rules (any failure means drop and log, never crash)

Reject a frame if: it is shorter than the minimum for its type, the version is not 1, the type is unknown, the payload length disagrees with the frame length or exceeds 160, TTL is 0 on a non-HELLO frame, HELLO has TTL other than 1, or a TEXT has a zero recipient NodeID.

---

## C2. Module interfaces

Interfaces are described as contracts, not code. Python signatures are in the repository; this section fixes what each call promises.

### 2.1 Transport

| Call          | Contract                                                                                                                                      |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `send(frame)` | Transmit to all current neighbors. Non-blocking. May lose the frame silently; loss belongs to the medium                                      |
| `recv()`      | Async. Returns the next received frame and an opaque transmitter label (an IP address in the UDP transport, a node name in the in-memory one) |
| `close()`     | Stop and release resources                                                                                                                    |

Everything is broadcast. Who a message is for is decided in the envelope, never in the transport.

### 2.2 Clock and randomness

Nodes never read system time or global randomness directly. They receive a **Clock** (`now()` in seconds, monotonic) and a seeded **Rng**. Tests supply a fake clock and a fixed seed, which is what makes harness tests deterministic.

### 2.3 Envelope codec

| Call                              | Contract                                              |
| --------------------------------- | ----------------------------------------------------- |
| `encode(envelope) -> bytes`       | Produces a frame that satisfies C1                    |
| `decode(bytes) -> envelope`       | Raises `DecodeError` on any validation failure in 1.6 |
| `signed_bytes(envelope) -> bytes` | The bytes defined in 1.5                              |

### 2.4 Crypto and identity

| Call                                                           | Contract                                                               |
| -------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `generate_identity()`                                          | New Ed25519 key pair; exposes `public_key` (32 B) and `node_id` (16 B) |
| `node_id_of(public_key)`                                       | The 16-byte NodeID                                                     |
| `sign(identity, signed_bytes)`                                 | 64-byte signature                                                      |
| `verify(public_key, signed_bytes, signature)`                  | True or false; never raises on bad input                               |
| `seal(recipient_public_key, plaintext, aad)`                   | Returns ephemeral public key, nonce, ciphertext                        |
| `open(identity, ephemeral_public_key, nonce, ciphertext, aad)` | Plaintext, or none on any failure                                      |

### 2.5 Message store

Messages are keyed by (sender public key, sequence number).

| Call                         | Contract                                                                                                         |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `put(envelope, now)`         | Stores the message; if full, evicts lowest priority first and oldest within a priority; returns the evicted keys |
| `contains(key)` / `get(key)` | Lookup                                                                                                           |
| `remove(key)`                | Used when an end-to-end ACK is overheard                                                                         |
| `due(now)`                   | Messages whose re-offer time has arrived                                                                         |
| `purge_expired(now)`         | Removes entries past retention                                                                                   |

### 2.6 Flood engine and neighbor manager

| Call                        | Contract                                                                                              |
| --------------------------- | ----------------------------------------------------------------------------------------------------- |
| `submit(envelope)`          | A service hands over a signed envelope for transmission and forwarding                                |
| `on_deliver(callback)`      | The engine calls it for every accepted message addressed to this node or broadcast, with the envelope |
| `neighbors()`               | List of neighbor records: NodeID, public key, node class, last-heard time                             |
| `on_new_neighbor(callback)` | Called when a neighbor appears; the re-offer scheduler uses this                                      |

### 2.7 Event logger

`log(event, **fields)` writes one line as defined in C3. It never blocks the protocol and never raises.

---

## C3. Event log schema

Each node writes one **JSONL** file: one JSON object per line.

### 3.1 Common fields

| Field   | Meaning                                                                     |
| ------- | --------------------------------------------------------------------------- |
| `t`     | Seconds, from the host's monotonic clock (shared by all nodes in emulation) |
| `node`  | Node name, for example `n07`                                                |
| `event` | One of the names in 3.2                                                     |

### 3.2 Events and their extra fields

| Event                          | When                                   | Extra fields                                                                 |
| ------------------------------ | -------------------------------------- | ---------------------------------------------------------------------------- |
| `TX`                           | A frame is transmitted                 | `mid`, `type`, `sender`, `ttl`, `size`                                       |
| `RX`                           | A frame is received                    | `mid`, `type`, `sender`, `ttl`, `size`, `tx` (transmitter label)             |
| `DROP`                         | A frame is discarded                   | `mid` if known, `reason`                                                     |
| `DELIVER`                      | A message reaches the service layer    | `mid`, `type`, `sender`                                                      |
| `TEXT_SEND`                    | A user text is created                 | `mid`, `recipient`, `size`                                                   |
| `ACK_RX`                       | The sender receives the end-to-end ACK | `mid`                                                                        |
| `TEXT_END`                     | A text is finished                     | `mid`, `outcome` (`delivered` or `expired`)                                  |
| `BUF_PUT`, `BUF_EVICT`         | Buffer changes                         | `mid`, `type`, `buffer_size`                                                 |
| `REOFFER`                      | A buffered message is retransmitted    | `mid`, `attempt`, `trigger` (`timer` or `encounter`)                         |
| `NEIGHBOR_UP`, `NEIGHBOR_DOWN` | Neighbor table changes                 | `peer`                                                                       |
| `KEY_LEARNED`                  | A key enters the cache                 | `peer`, `via` (`hello`, `reply`, `qr`), `trust` (`verified` or `unverified`) |
| `SOS_STATE`                    | An SOS is sent or cancelled            | `mid`, `status`                                                              |

`reason` values for `DROP`: `MALFORMED`, `RATE_LIMIT`, `DUPLICATE`, `BAD_SIG`, `REPLAY`, `TTL`. Identifiers (`mid`, `sender`, `recipient`, `peer`) are lowercase hex.

### 3.3 Other files in a run directory

| File              | Written by          | Content                                                                            |
| ----------------- | ------------------- | ---------------------------------------------------------------------------------- |
| `positions.jsonl` | Scenario controller | `t`, `node`, `x`, `y` once per second per node                                     |
| `manifest.json`   | Scenario controller | Scenario name, seed, protocol variant, all parameters, git commit hash, start time |

---

## C4. Config and scenario format

Both use **TOML**, which Python reads with its standard library.

### 4.1 Node parameters (initial values from the architecture document; tuned in week 5)

| Key                                                           | Default  | Meaning                                                                                                                          |
| ------------------------------------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `variant`                                                     | `relay`  | `relay` or `baseline`                                                                                                            |
| `hello_interval_s`                                            | 2.0      | HELLO period, with random offset                                                                                                 |
| `neighbor_timeout_missed`                                     | 3        | Missed HELLOs before a neighbor is dropped                                                                                       |
| `default_ttl`                                                 | 16       | TTL for new messages                                                                                                             |
| `jitter_ms`                                                   | 10 to 80 | Rebroadcast delay range                                                                                                          |
| `counter_suppression_k`                                       | 2        | Cancel a rebroadcast after hearing it this many times                                                                            |
| `seq_window`                                                  | 256      | Per-sender window size                                                                                                           |
| `buffer_capacity`                                             | 200      | Messages kept                                                                                                                    |
| `retention_s`                                                 | 1800     | Local expiry                                                                                                                     |
| `reoffer_initial_s`, `reoffer_factor`, `reoffer_max_attempts` | 5, 2, 5  | Re-offer backoff                                                                                                                 |
| `rate_limit_tx_fps`                                           | 20       | Per-transmitter frame limit                                                                                                      |
| `rate_limit_sos_origin_s`                                     | 10       | Minimum seconds between SOS from one origin                                                                                      |
| `sos_repeat_interval_s`                                       | 15       | Seconds between repeats of an active SOS; must stay above `rate_limit_sos_origin_s` so a legitimate repeat is never rate limited |

In `baseline`, jitter, counter suppression, rate limits, and priorities are off; duplicate suppression stays on. Relay adds exactly those four.

### 4.2 Scenario file

| Section      | Keys                                                        |
| ------------ | ----------------------------------------------------------- |
| `[run]`      | `name`, `seed`, `duration_s`                                |
| `[area]`     | `width_m`, `height_m`                                       |
| `[nodes]`    | `count`, `relay_count`, `handheld_range_m`, `relay_range_m` |
| `[mobility]` | `model`, `speed_min_mps`, `speed_max_mps`                   |
| `[loss]`     | `receiver_loss_probability`, `per_hop_delay_ms`             |
| `[workload]` | `texts`, `text_min_separation_m`, `sos_beacons`             |
| `[contacts]` | `verified_pairs`: which node pairs start with verified keys |

Node placement and mobility are derived from the seed. The scenario shape (clustered or sparse) is an open question and changes only values here, not the keys.

---

## 5. Decision record

Accepted by the whole team in a team meeting:

| ID  | Decision                                                                                                             |
| --- | -------------------------------------------------------------------------------------------------------------------- |
| D3  | Baseline is flooding with duplicate suppression but none of Relay's extra mechanisms                                 |
| D4  | Reliability is retention plus re-offer timers, encounter triggers, and overhearing; no hop-level ACK frame           |
| D5  | KEY_REPLY is a sixth message type                                                                                    |
| D6  | Each SOS repeat is a fresh message with a new sequence number, sent every 15 seconds; cancel is an SOS with status 1 |
| D7  | Duplicates and replays are checked by (sender, sequence) in one window; Message ID is for logs and ACK references    |

Not contract-affecting and still open: D1 and D8 (the Mininet-WiFi spike, M1), D2 (replay window size, tested in the week 2 harness), D9 and D10 (deferred).

## 6. Change control

1. A change starts as a pull request that edits this file, with a one-paragraph reason.
2. Every member who builds or consumes the affected contract must approve in the pull request.
3. The minor version number goes up (v0.2, v0.3) and the merge is tagged.
4. Code written against an old version is fixed in the same pull request or immediately after.

If a change only affects the inside of one module, it does not belong here.

## 7. Questions to settle in the freeze meeting

1. Does every module have everything it needs from C2 to do its work, with nothing left to ask another member?
2. Is anyone unhappy with a byte layout or numbering in C1?
3. Is the TOML key set in C4 enough for the scenario generator and the node runtime?
4. Does every metric in the architecture document's metrics table come from a field in C3?
5. Does everyone agree with D3 to D7 as written?
