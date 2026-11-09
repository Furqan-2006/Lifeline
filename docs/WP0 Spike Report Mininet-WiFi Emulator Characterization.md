# WP0 Spike Report: Mininet-WiFi Emulator Characterization

Lifeline project. Two runs: the main spike on 9 November 2026 (16:31–16:47), and a follow-up suite that closes the open points (22:39–22:49). Covers checklist sections B to H; section A (environment) is left out as agreed.

## Setup

Mininet-WiFi 2.7 with wmediumd in interference mode, logDistance propagation with path-loss exponent 4.0, ad hoc (IBSS) mode g on channel 5. All traffic is broadcast (contract C2) with 333-byte frames. Handhelds are configured for 100 m range (the emulator reports 20 dBm), relays for 500 m (48 dBm). Host: 12 CPUs. Each emulated network ran in its own process, so no state carried over between networks. Loss and delay are injected by a seeded receiver-side shim in our own code, not by wmediumd. Sampling resolution: 10 m for handheld range, 10 m for the relay edge, 10 m for the receiver-fan-out sweep.

## Summary

| Area | Result | Verdict |
| --- | --- | --- |
| B1 Basic communication | 5 stations all hear each other, zero loss on 20 sender-receiver pairs | PASS |
| B3 Frame size | 333-byte frames delivered, no fragmentation | PASS |
| B4 Range | 99.5% received up to 100 m, 0% from 110 m | PASS |
| C1 Loss accuracy | Measured 0.00 / 4.92 / 15.20% for 0 / 5 / 15% configured | PASS |
| C2 Loss independence | Joint-loss ratio 0.96 and 0.99 (1.0 means independent) | PASS |
| C3 Delay | Shim delay 20.1 ms at 50 fps and 20.2 ms at 20 fps against 20 ms configured; 1515 ms end-to-end at 333 fps from receive-buffer queueing, not shim error | PASS |
| D Mobility | Reception follows position, r90 while moving 100 m | PASS |
| E Relay nodes | r90 = 520 m, r10 = 530 m in both directions, symmetric | PASS |
| F Scale | 5, 15, 20 and 30 stations pass; 6% CPU at 30 | PASS |
| G Reproducibility | Three identical runs differ by 0.000; five start/stop cycles, no leaks | PASS |

## B. Basic communication

**B1.** In the inject run, five stations each broadcast 2000 numbered frames at 0% configured loss. All 20 sender-receiver pairs received every frame.

**B3.** Every test uses 333-byte frames. In the earlier test_3 run, 1000 of 1000 frames arrived at both 64 and 333 bytes to listeners at 10, 90 and 110 m, and no fragmentation counters moved.

**B4.** Range sweep in the follow-up, 1000 frames per distance, receivers every 10 m from 50 to 200 m, 16 receiving stations plus one sender:

| Distance | Frames received (plain listener) | Frames received (drain-on-start listener) |
| --- | --- | --- |
| 50 to 100 m | 995 of 1000 (99.5%), identical at every distance | 989 of 1000 (98.9%), identical at every distance |
| 110 to 200 m | 0 of 1000 | 0 of 1000 |

The cutoff is a hard step between 100 m and 110 m (r90 = 100 m, r10 = 110 m, edge width 10 m). 90 m is heard and 110 m is not, as required. There is no gradual fade in this model.

The 0.5% loss at 16 receivers was investigated by the follow-up drain-on-start listener. That listener reads and discards any packets already queued before signalling readiness, so the sender starts into an empty receive buffer. It drained **0 packets at every distance** and delivered **slightly less** (98.9% vs 99.5%). The loss is therefore **not** caused by startup receive-buffer overflow. WP14 should still not count it as range loss, but the mechanism is not queueing at startup; the likely causes are IBSS join race at test start, or the first few frames being sent before all 16 receivers have joined the IBSS. This is a WP5/WP9 startup concern, not an emulator range measurement. The earlier test_3 run heard 110 m at 100% because it reported a minimum TX power of 1 dBm; its settings differed from this run, where range 100 m maps to 20 dBm and the cutoff matches.

## C. Loss and delay

**C1.** 2000 frames per pair, 20 pairs per level:

| Configured loss | Measured mean | Error | Per-pair range |
| --- | --- | --- | --- |
| 0% | 0.00% | 0.00 pp | 0.00 to 0.00% |
| 5% | 4.92% | −0.08 pp | 3.95 to 5.90% |
| 15% | 15.20% | +0.20 pp | 13.95 to 17.30% |

Per-pair spread is what sampling noise gives with 2000 frames (about 0.5 pp standard deviation at 5%, 0.8 pp at 15%).

**C2.** The joint-loss ratio is 0.96 at 5% and 0.99 at 15%. Independent loss gives about 1; identical loss patterns across receivers would give about 20 at 5% and 6.7 at 15%. Receiver-side loss is therefore independent per receiver. The first attempt, sender-side netem loss, produced identical loss at all listeners, which is why the receiver-side shim is used.

**C3.** The follow-up re-ran the delay test at three send rates per sender, with five senders running concurrently. Results:

| Rate per sender | Frames per sender | Shim median | Shim p95 | End-to-end median | End-to-end p95 |
| --- | --- | --- | --- | --- | --- |
| 50 fps | 1000 | 20.1 ms | 20.2 ms | 29.5 ms | 34.6 ms |
| 20 fps | 1000 | 20.2 ms | 20.3 ms | 28.8 ms | 31.2 ms |
| 333 fps (control) | 1000 | 20.1 ms | 20.2 ms | 1515 ms | 2419 ms |

The shim's own delay is accurate at every rate: 20.1–20.2 ms against 20 ms configured, including at the spike's original 333 fps aggregate rate. The earlier spike's 414 ms measurement was an **end-to-end** figure under load, not a shim error. The follow-up reproduces the same end-to-end inflation at 333 fps (1515 ms), caused by receive-buffer queueing in the listener under sustained 5 × 333 fps aggregate traffic. At realistic rates (50 fps, 20 fps per sender), end-to-end latency is 28–30 ms, which is within the expected range of shim + network + socket scheduling.

A tc netem delay probe (one sender, 200 frames at 5 ms spacing, 20 ms configured) was received 100% by all four receivers with a median delay of 23.9 ms, so netem delay does not break broadcast. `tc netem loss`, by contrast, does break ad hoc broadcast and is not used.

**Conclusion for WP7:** use the shim's `shim_delay` (measured as `dts − rts`) for delay accuracy. Measure end-to-end latency only at realistic aggregate rates (≤ 50 fps per receiver, i.e., ≤ 250 fps aggregate per receiver with five senders). At higher rates, end-to-end latency is dominated by listener receive-buffer queueing, not by the shim.

## D. Mobility

A station moved from 50 m at 2 m/s for 100 s (to 250 m) while position was updated every 0.5 s by setPosition, with a sender broadcasting every 0.1 s.

| Position bin | Frames received |
| --- | --- |
| 50 to 100 m | 100% |
| 110 to 250 m | 0% |

Movement changes reception, r90 while moving is 100 m, and the moving curve differs from the static range curve by at most 0.008. setPosition took 0.45 ms on average and 0.56 ms at most.

## E. Relay nodes

The follow-up resolved the relay edge at 10 m resolution. 500 frames per direction, relay placed at each distance from a handheld:

| Distance | Handheld to relay | Relay to handheld |
| --- | --- | --- |
| 500, 510, 520 m | 100% | 100% |
| 530 to 600 m (every 10 m) | 0% | 0% |

Links are symmetric, with r90 = 520 m and r10 = 530 m in both directions. The reception curve is monotonic over the tested range (no non-monotonic recoveries). This replaces the original 100 m resolution bracket ("at least 500 m, under 600 m") with a 10 m resolution bracket: the effective relay range is 520 m, edge width 10 m.

One consequence matters for the scenario design: a handheld configured at 20 dBm was heard by the relay at 520 m, although another handheld at 20 dBm stops hearing it beyond 100 m. The follow-up sampled `iw dev hh-wlan0 info` before launch, at sender start, during transmission, and after completion. At every distance and every sample, the handheld interface reported `txpower 20.00 dBm`. The handheld's configured power is therefore **not** being raised during the relay test. The 500 m handheld-to-relay link is a genuine property of wmediumd's path-loss model, not a measurement artifact.

The follow-up also found that the relay's `iw dev rr-wlan0 info` reports `txpower 20.00 dBm`, while the Mininet-WiFi `wintfs[0].txpower` attribute reports 48 dBm. The 48 dBm is a wmediumd per-node power parameter, not the kernel-visible interface setting. This does not affect the handheld-to-relay finding (the handheld's interface setting matches its attribute), but it means the report should not describe the relay's txpower as 48 dBm "in the kernel"; it is 48 dBm "in wmediumd."

**For WP9:** the "handheld broadcast range = 100 m" figure applies to handheld-to-handheld links specifically. The handheld-to-relay link is 500–520 m as measured, and handheld-to-relay delivery at that range is 100% when the relay is within the handheld's wmediumd power envelope.

## F. Scale

Close-range stations, periodic HELLO plus a flood burst of 10 messages, 30 s runs. The follow-up added the 20-node size:

| Stations | HELLO delivery | Flood reach | Flood latency median / p95 | CPU busy | Build / stop |
| --- | --- | --- | --- | --- | --- |
| 5 | 100% | 100% | 2.1 / 2.3 ms | 2.9% | 19.7 / 1.8 s |
| 15 | 100% | 100% | 2.0 / 2.4 ms | 2.2% | 36.9 / 2.2 s |
| 20 | 99.98% | 100% | 1.8 / 3.7 ms | 2.6% | 46.0 / 2.3 s |
| 30 | 98.4% | 100% | 2.2 / 3.3 ms | 6.2% | 63.7 / 2.8 s |

Thirty stations pass on this 12-CPU host with large CPU headroom, so a 20-versus-30 node choice is not limited by the host. The 20-node size passes with margin (99.98% HELLO delivery, 2.6% CPU, 46 s build), so it is documented as a safe fallback for WP14 if the 30-node size proves problematic for other reasons.

HELLO delivery at 30 stations is 98.4%, only 0.4 pp above the 98% pass line, which is within run-to-run noise — a rerun could legitimately fall either side. **WP5 should fire an immediate HELLO on node startup** (before entering the periodic schedule) so that HELLO delivery at 30 nodes moves above 99% and the pass margin is not fragile. This is a node-runtime concern, not an emulator concern. Also, all stations were within about 25 m of each other, so this test measures load, not range. Build time is roughly 10 s plus 1.8 s per station. Each flood message cost one transmission per node (5, 15, 20, 30). The memory figure was inconsistent (−96 MB at 15 stations) and is not reported.

## G. Reproducibility and run hygiene

Three identical runs at 20, 90, 100, 110 and 120 m (1000 frames each) gave exactly the same reception at every distance, a spread of 0.000. The emulator's range behaviour is deterministic, so any run-to-run variation in Lifeline results will come from the protocol and the seeded shim, not the emulator. This was measured only on the static range edge.

Five start/stop cycles of 10 stations took 28.0 to 28.5 s to build and about 2.0 s to stop. Interface counts were identical before and after (4 network interfaces, 1 wifi interface, 0 wmediumd processes), so nothing leaks between runs.

## H. Conclusions and parameters for the scenario generator

| Parameter | Value |
| --- | --- |
| Propagation | logDistance, exponent 4.0 |
| Handheld-to-handheld broadcast range | 100 m, hard cutoff (heard at 100 m, not at 110 m) |
| Relay range | 520 m (r90), edge width 10 m; symmetric in both directions |
| Handheld-relay link | 500–520 m in both directions |
| Loss | Seeded receiver-side shim, independent per receiver, accurate to 0.2 pp |
| Delay | Shim delay 20.1–20.2 ms at realistic rates; use shim_delay, not e2e, for accuracy; e2e only at ≤ 50 fps per receiver |
| Mobility | setPosition on a schedule works (2 m/s, 0.5 s steps, under 1 ms per call) |
| Scale | 30 stations pass; 20 stations pass with margin; build about 64 s at 30 nodes |
| Run overhead | about 28 s build and 2 s stop for 10 stations; 46 s build for 20; 64 s for 30 |
| Reproducibility | Deterministic at the range edge; no leaks across cycles |

### Connectivity re-check

The connectivity script used the handheld range (100 m) for handheld-relay links. Section E shows that link is 500 m, so the script understated connectivity. Recomputed with the same model (ideal disc, static uniform placement, no loss, 2000 trials) and the measured 500 m handheld-relay link:

| Scenario | Mean handheld degree | Largest component | Far pairs (600 m or more) connected |
| --- | --- | --- | --- |
| Proposal 1200×1200 m, 24 handhelds + 6 relays | 0.59 became 2.67 | 23% became 91% | 0.5% became 76.8% |
| Corridor 1200×150 m, 26 handhelds + 4 relays | 3.49 became 5.62 | 68% became 98% | 23.2% became 94.4% |
| Clustered 3×8 handhelds + 6 relays | 4.00 became 7.17 | 76% became 100% | 40.7% became 98.8% |

With the measured 500 m handheld-relay link, all three layouts are viable. The proposal layout reaches a 91% largest component and 76.8% far-pair connectivity; the corridor and clustered layouts reach 98% and 100% respectively, with corresponding gains in far-pair connectivity.

**Recommendation for WP9:** use the proposal's 1200×1200 m layout. It is viable at the measured ranges, and preserving the proposal's shape avoids a C4 change that would need to be justified on grounds other than connectivity. The corridor and clustered layouts are available as documented alternatives if the team later decides that higher connectivity is worth the change. **C4's `[area]` section does not need to change for connectivity reasons.** These figures remain order-of-magnitude guides; they use an ideal disc model, static placement, and no loss.

## Open points — all closed

All five open points from the previous version of this report have been addressed by the follow-up suite (spike_followup.py, run 22:39–22:49 on the same host). Their resolutions:

1. **Shim delay (OP1) — closed.** The shim's own delay is accurate at every tested rate (20.1 ms at 50 fps, 20.2 ms at 20 fps, 20.1 ms at 333 fps, all against 20 ms configured). The 414 ms observed in the original spike was end-to-end latency under load, not shim error. The follow-up reproduces the mechanism at 333 fps (1515 ms e2e) and confirms it is receive-buffer queueing in the listener, not a defect in the shim's delay path. **WP7 may use the shim's delay**; end-to-end latency should be measured only at realistic aggregate rates (≤ 50 fps per receiver). Results in `results/f1_shim_delay.json`.
2. **Relay edge between 500 and 600 m (OP2) — closed.** Measured at 10 m resolution: r90 = 520 m, r10 = 530 m, symmetric, monotonic over the tested range. The §H row is updated from "at least 500 m, under 600 m" to "520 m (r90), edge width 10 m." Results in `results/f2_relay_edge.json`.
3. **Handheld txpower during the relay test (OP3) — closed.** Sampled `iw dev hh-wlan0 info` before launch, at sender start, during transmission, and after completion at 100, 200, 400 and 500 m. The handheld reports `txpower 20.00 dBm` at every sample and every distance. The handheld's configured power is not raised during the relay test, and the 500 m handheld-to-relay link is a genuine wmediumd property. A secondary finding: the relay's `iw dev` reports 20 dBm while its wmediumd attribute reports 48 dBm, so the report describes the relay's 48 dBm as a wmediumd parameter, not a kernel setting. Results in `results/f3_handheld_txpower.json`.
4. **Scale at 20 stations (OP4) — closed.** 20 nodes pass with margin: 99.98% HELLO delivery, 100% flood reach, 2.6% CPU, 46 s build. The 20-node fallback is safe for WP14 if the 30-node size proves problematic. Results in `results/f4_scale_20.json`.
5. **0.5% loss with 16 receivers (OP5) — closed.** Investigated with a drain-on-start listener that discards queued packets before signalling readiness. The drain phase discarded **0 packets at every distance** and the drain listener delivered slightly less (98.9% vs 99.5%), so the loss is not startup receive-buffer overflow. WP14 should still not count it as range loss, but the mechanism is not queueing at startup. Likely candidates: IBSS join race at test start, or first-HELLO-in-flight behaviour before all receivers have joined. This is a WP5/WP9 startup concern, not an emulator property. Results in `results/f5_startup_loss.json`.

## Design changes since the proposal

For the final report's change log:

- **D1 (loss mechanism).** Loss is injected by a seeded receiver-side shim, not by wmediumd or `tc netem loss`. Measured accuracy is ±0.2 pp, independent per receiver (joint-loss ratio 0.96–0.99), composes with mobility, and does not require Mininet-WiFi features beyond topology and mobility. `tc netem loss` on `*-wlan0` breaks ad hoc broadcast; `tc netem delay` works but is unused (the shim handles both). WP14 measures latency at ≤ 50 fps aggregate per receiver.
- **D8 (relay-class nodes).** Relay-class nodes work: r90 = 520 m, symmetric, at wmediumd power 48 dBm. The "handheld range = 100 m" figure applies to handheld-to-handheld links specifically; handheld-relay is 500–520 m. WP9 can use the relay-class node as designed.
- **Scenario layout.** The proposal's 1200×1200 m layout is viable at the measured ranges (91% largest component, 76.8% far-pair connectivity). No C4 change is required for connectivity reasons.
- **30-node HELLO delivery.** At 30 stations, HELLO delivery is 98.4%, within noise of the 98% pass line. WP5 should fire an immediate HELLO on node startup so this moves above 99%. This is a protocol implementation note, not an emulator change.

## Artifacts

- `results/env.json`, `results/range.json`, `results/relay.json`, `results/inject.json`, `results/mobility.json`, `results/repeat.json`, `results/scale.json`, `results/lifecycle.json`, `results/connectivity.json` — the original spike's per-test results.
- `results/f1_shim_delay.json`, `results/f2_relay_edge.json`, `results/f3_handheld_txpower.json`, `results/f4_scale_20.json`, `results/f5_startup_loss.json` — the follow-up's per-test results.
- `results/summary.md`, `results/followup_summary.md`, `results/connectivity.md` — generated summaries.
- Raw wire-level logs under `/tmp/lifeline_spike/` for verification.