# WP0 Spike Report: Mininet-WiFi Emulator Characterization

Lifeline project. One full run of spike.py on 9 November 2026 (16:31 to 16:47). Covers checklist sections B to H; section A (environment) is left out as agreed.

## Setup

Mininet-WiFi 2.7 with wmediumd in interference mode, logDistance propagation with path-loss exponent 4.0, ad hoc (IBSS) mode g on channel 5. All traffic is broadcast (contract C2) with 333-byte frames. Handhelds are configured for 100 m range (the emulator reports 20 dBm), relays for 500 m (48 dBm). Host: 12 CPUs. Each emulated network ran in its own process, so no state carried over between networks. Loss and delay are injected by a seeded receiver-side shim in our own code, not by wmediumd. Sampling resolution: 10 m for handheld range, 100 m for relay range.

## Summary

| Area | Result | Verdict |
| --- | --- | --- |
| B1 Basic communication | 5 stations all hear each other, zero loss on 20 sender-receiver pairs | PASS |
| B3 Frame size | 333-byte frames delivered, no fragmentation | PASS |
| B4 Range | 99.2% received up to 100 m, 0% from 110 m | PASS |
| C1 Loss accuracy | Measured 0.00 / 4.92 / 15.20% for 0 / 5 / 15% configured | PASS |
| C2 Loss independence | Joint-loss ratio 0.96 and 0.99 (1.0 means independent) | PASS |
| C3 Delay with loss | netem delay works on broadcast; shim delay measured 414 ms against 20 ms, pending follow-up | PARTIAL |
| D Mobility | Reception follows position, r90 while moving 100 m | PASS |
| E Relay nodes | Symmetric, 100% up to 500 m, 0% at 600 m | PASS |
| F Scale | 5, 15 and 30 stations pass, 6% CPU at 30 | PASS |
| G Reproducibility | Three identical runs differ by 0.000; five start/stop cycles, no leaks | PASS |

## B. Basic communication

**B1.** In the inject run, five stations each broadcast 2000 numbered frames at 0% configured loss. All 20 sender-receiver pairs received every frame.

**B3.** Every test uses 333-byte frames. In the earlier test_3 run, 1000 of 1000 frames arrived at both 64 and 333 bytes to listeners at 10, 90 and 110 m, and no fragmentation counters moved.

**B4.** Range sweep, 1000 frames per distance, receivers every 10 m from 50 to 200 m:

| Distance | Frames received |
| --- | --- |
| 50 to 100 m | 992 of 1000 (99.2%), identical at every distance |
| 110 to 200 m | 0 of 1000 |

The cutoff is a hard step between 100 m and 110 m (r90 = 100 m, r10 = 110 m, edge width 10 m). 90 m is heard and 110 m is not, as required. There is no gradual fade in this model.

Two notes. The same 8 frames are missing at every distance up to 100 m, so this is not a range effect. The 0%-loss inject test with 5 receivers and the repeat test both received 100%, and the only structural difference here is 16 simultaneous receivers. The likely cause is a startup receive-buffer effect: with 16 listeners draining their sockets on the same shared host CPU, the first few frames overflow the kernel receive buffer before the listener loop drains it. The mechanism is consistent with the listener code (`select` with a 0.25 s timeout, `SO_RCVBUF` = 4 MB) and would be confirmed by a drain-on-start loop in the listener, but that was not done. **WP14 should not count this 0.8% as range loss**; it is startup overhead at high receiver fan-out. The earlier test_3 run heard 110 m at 100% because that run reported a minimum TX power of 1 dBm, so its settings differed from this run, where range 100 m maps to 20 dBm and the cutoff matches.

## C. Loss and delay

**C1.** 2000 frames per pair, 20 pairs per level:

| Configured loss | Measured mean | Error | Per-pair range |
| --- | --- | --- | --- |
| 0% | 0.00% | 0.00 pp | 0.00 to 0.00% |
| 5% | 4.92% | -0.08 pp | 3.95 to 5.90% |
| 15% | 15.20% | +0.20 pp | 13.95 to 17.30% |

Per-pair spread is what sampling noise gives with 2000 frames (about 0.5 pp standard deviation at 5%, 0.8 pp at 15%).

**C2.** The joint-loss ratio is 0.96 at 5% and 0.99 at 15%. Independent loss gives about 1; identical loss patterns across receivers would give about 20 at 5% and 6.7 at 15%. Receiver-side loss is therefore independent per receiver. The first attempt, sender-side netem loss, produced identical loss at all listeners, which is why the receiver-side shim is used.

**C3.** A tc netem delay probe (one sender, 200 frames at 5 ms spacing, 20 ms configured) was received 100% by all four receivers with a median delay of 23.9 ms, so netem delay does not break broadcast. The shim delay test did not pass: median delay was 414 ms (p95 545 ms) at 0% loss, 421 ms at 5% and 414 ms at 15%, against 20 ms configured and a 5 ms tolerance. Delay is the same at every loss level, so loss and delay do not interfere. The likely cause is queueing from load: the shim test had five senders at about 333 frames per second each (about 1,670 frames per second on one broadcast channel) against 200 frames per second in the probe that gave 23.9 ms. The ratio of 1.67 in rate does not by itself explain a 20× increase in delay, so a second mechanism — the shim's delivery loop falling behind under sustained load — is likely. This is the subject of the first open point below.

## D. Mobility

A station moved from 50 m at 2 m/s for 100 s (to 250 m) while position was updated every 0.5 s by setPosition, with a sender broadcasting every 0.1 s.

| Position bin | Frames received |
| --- | --- |
| 50 to 100 m | 100% |
| 110 to 250 m | 0% |

Movement changes reception, r90 while moving is 100 m, and the moving curve differs from the static range curve by at most 0.008 (the 0.8% startup loss above). setPosition took 0.45 ms on average and 0.56 ms at most.

## E. Relay nodes

500 frames per direction, relay placed at each distance from a handheld:

| Distance | Handheld to relay | Relay to handheld |
| --- | --- | --- |
| 100 to 500 m | 100% | 100% |
| 600 m | 0% | 0% |

Links are symmetric, with r90 = 500 m in both directions (the true edge lies between 500 and 600 m; resolution here is 100 m).

One consequence matters for the scenario design: a handheld at 20 dBm was heard by the relay at 500 m, although another handheld at 20 dBm stops hearing it beyond 100 m. For handheld-relay links the emulator does not reflect the handheld's lower transmit power in the same way it does for handheld-handheld links. The reason was not investigated; the two possibilities are that wmediumd combines both ends' transmit power for broadcast links (making the reported 100 m range specifically a handheld-to-handheld figure), or that the handheld's txpower was raised during the relay test (not checked). The first is more likely given the symmetric 100% delivery at 500 m in both directions. **WP9 should treat the "handheld broadcast range = 100 m" figure as applying to handheld-to-handheld links specifically**; the handheld-to-relay link is 500 m as measured. The relevant follow-up check is a one-line `iw dev hh-wlan0 info` inside the relay test, before the sender starts, to confirm the handheld's txpower is still 20 dBm at that point.

## F. Scale

Close-range stations, periodic HELLO plus a flood burst of 10 messages, 30 s runs:

| Stations | HELLO delivery | Flood reach | Flood latency median / p95 | CPU busy | Build / stop |
| --- | --- | --- | --- | --- | --- |
| 5 | 100% | 100% | 2.1 / 2.3 ms | 2.9% | 19.7 / 1.8 s |
| 15 | 100% | 100% | 2.0 / 2.4 ms | 2.2% | 36.9 / 2.2 s |
| 30 | 98.4% | 100% | 2.2 / 3.3 ms | 6.2% | 63.7 / 2.8 s |

Thirty stations pass on this 12-CPU host with large CPU headroom, so a 20-versus-30 node choice is not limited by the host. Two cautions: HELLO delivery at 30 stations is 98.4%, only 0.4 pp above the 98% pass line, which is within run-to-run noise — a rerun could legitimately fall either side. **WP5 should fire an immediate HELLO on node startup** (before entering the periodic schedule) so that HELLO delivery at 30 nodes moves above 99% and the pass margin is not fragile. This is a node-runtime concern, not an emulator concern. Also, all stations were within about 25 m of each other, so this test measures load, not range. Build time is roughly 10 s plus 1.8 s per station. Each flood message cost one transmission per node (5, 15, 30). The memory figure was inconsistent (−96 MB at 15 stations) and is not reported.

## G. Reproducibility and run hygiene

Three identical runs at 20, 90, 100, 110 and 120 m (1000 frames each) gave exactly the same reception at every distance, a spread of 0.000. The emulator's range behaviour is deterministic, so any run-to-run variation in Lifeline results will come from the protocol and the seeded shim, not the emulator. This was measured only on the static range edge.

Five start/stop cycles of 10 stations took 28.0 to 28.5 s to build and about 2.0 s to stop. Interface counts were identical before and after (4 network interfaces, 1 wifi interface, 0 wmediumd processes), so nothing leaks between runs.

## H. Conclusions and parameters for the scenario generator

| Parameter | Value |
| --- | --- |
| Propagation | logDistance, exponent 4.0 |
| Handheld-to-handheld broadcast range | 100 m, hard cutoff (heard at 100 m, not at 110 m) |
| Relay range | at least 500 m, under 600 m |
| Handheld-relay link | 500 m in both directions |
| Loss | Seeded receiver-side shim, independent per receiver, accurate to 0.2 pp |
| Delay | netem delay works on broadcast; shim delay to be re-validated at realistic load (see open point 1) |
| Mobility | setPosition on a schedule works (2 m/s, 0.5 s steps, under 1 ms per call) |
| Scale | 30 stations pass; build about 64 s |
| Run overhead | about 28 s build and 2 s stop for 10 stations |
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

## Open points

1. **Shim delay.** Rerun the shim delay test with one sender, 200 frames, 20 ms delay, 20 ms interval (50 fps). If median delay is approximately 20 ms, the 414 ms observed in the spike was rate-dependent queueing and the shim's delay is usable at realistic load. If median delay is still approximately 400 ms, the shim's delivery loop cannot keep up at any rate and WP7 must not use the shim's delay; use `tc netem delay` instead (which the probe confirmed works on broadcast). Owner: M1, before WP7 needs the delay mechanism. Small, one run.
2. **Relay edge between 500 and 600 m.** Measure at 10 m steps to find the transition. The current resolution is 100 m, which is sufficient for the scenario generator but not for a precise range claim in the final report. Owner: M1, when convenient.
3. **Handheld txpower during the relay test.** Run `iw dev hh-wlan0 info` inside the relay test before the sender starts, to confirm the handheld's txpower is still 20 dBm. This distinguishes "wmediumd combines both ends' power for broadcast" from "the handheld's txpower was raised during the test." Owner: M1, one-line check that can be folded into the next spike re-run.
4. **Scale at 20 stations.** Scale was measured at 5, 15 and 30 stations; 20 was not run separately. If WP14 elects to use 20 nodes as a fallback, this is the size to re-measure. Not urgent. Owner: M1, before WP14 if the 30-node option is dropped.
5. **0.8% startup loss with 16 receivers.** Confirm by adding a drain-on-start loop to the listener and re-running the range sweep. If the 992/1000 becomes 1000/1000, the loss is a startup receive-buffer effect and should not be counted as range loss. Not urgent — WP14 should not count it as range loss either way, per §B4.