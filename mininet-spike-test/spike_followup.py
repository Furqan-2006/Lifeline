#!/usr/bin/env python3
"""
WP0 follow-up tests: closes the five open points in the WP0 spike report.

Requires the additive changes to spike.py documented in this project
(run_bg_marked, isolated timeout). Everything else imports from spike.py
unchanged.

Run (Linux, as root):
    sudo python3 spike_followup.py                     # all five tests
    sudo python3 spike_followup.py --only f1,f3        # just those
    sudo python3 spike_followup.py --quick --only f5   # fast dry run

Results: results/f1_shim_delay.json ... results/f5_startup_loss.json,
         results/followup_summary.md

Repairs in this version (after fourth review):
  * F5: listener wrapper now backgrounds the whole `(python ...; echo $? > .exit)`
        subshell. Before, `&` bound only to the echo, so each listener ran in
        the foreground and blocked for its full duration.
  * F5: listeners are stopped by SIGTERM (exit 143), and lb_drain.py was never
        matched by Lab.kill_tools(). Both helpers are now killed explicitly, we
        wait for the .exit files, and 0 or 143 count as clean.
  * main: errors are saved under the real result stem (f1 -> f1_shim_delay), so
        a crashed test shows as ERROR in the summary instead of "not run".
  * F3: the "during" iw sample is now taken while the sender is still running
        (and flagged if it was not); a separate "after" sample is kept.
  * F1: verdict depends on the shim being usable at realistic rates. Whether
        the 333 fps control reproduces the old anomaly is reported separately.
  * main: calls setLogLevel('info') and checks HAVE_MN, like spike.py, so
        progress lines actually print to the terminal.
  * Import guard only reports a missing spike.py, not any ImportError.
  * F2: removed unused variables.

Repairs in the previous version (after third review):
  * F1/F3: remove stale send_*.done markers before launching senders.
  * F3: check sender .done exit code; fail if nonzero.
  * F5: fix operator-precedence bug; separate `recovered` and `far_behavior_ok`
        as independent conditions, not one OR expression.
  * F5: rename diagnosis to reflect what the experiment actually tests
        (stabilization, not proven buffer loss). The label is
        `startup_stabilization_effect`, not `startup_receive_buffer_effect`.
  * F5: helper exit codes are recorded to .exit files (via shell wrapper) and
        checked; missing .exit is a failure.
  * F5: remove stale .drained files before each drain-mode run; missing
        .drained is a helper failure.
  * F3: qualifications for the iw claim. The report will say "configured
        transmit power is 20 dBm", not "actual RF power is 20 dBm". F3 also
        samples iw at sender-start and again near sender-end, not just at a
        fixed sleep.
  * F2: adds `monotonicity_ok` and reports the threshold as a bracket, not as
        an unqualified propagation boundary.
"""

import argparse
import json
import os
import statistics
import sys
import time
import traceback
import zlib

try:
    from spike import (
        Lab, HAND_RANGE, RELAY_RANGE, TOOLS_DIR, LB,
        mono, write_tools, read_rx, seqs_from, percentile,
        isolated, save, load, force_cleanup, info, setLogLevel, HAVE_MN,
    )
except ImportError as _e:
    if getattr(_e, 'name', None) == 'spike':
        sys.exit('spike_followup.py must live in the same directory as '
                 'spike.py (it reuses its helpers).')
    raise


# ================================================================= shared bits

def _read_helper_output(path):
    try:
        with open(path) as f:
            return f.read().splitlines()[-10:]
    except OSError:
        return []


def _read_int_file(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _wait_done(paths, timeout):
    """Wait for all .done files to appear with integer exit codes.
    Returns the list of codes (None where missing)."""
    deadline = mono() + timeout
    while mono() < deadline:
        codes = [_read_int_file(p) for p in paths]
        if all(c is not None for c in codes):
            return codes
        time.sleep(0.1)
    return [_read_int_file(p) for p in paths]


def _clear_files(paths):
    for p in paths:
        if p and os.path.exists(p):
            os.remove(p)


def _assert_ready(readies, timeout=20.0):
    t = mono() + timeout
    while mono() < t:
        if all(os.path.exists(r) for r in readies):
            return
        time.sleep(0.05)
    missing = [r for r in readies if not os.path.exists(r)]
    raise RuntimeError('listeners did not start: %s' % missing[:3])


# ================================================================= f1: shim delay

def _f1_delay_one(a, rate_fps, n_senders, frames, delay_ms):
    """One delay test at a given send rate per sender.

    Senders run concurrently. Stale .done markers are removed before launch,
    and every sender's exit code is checked before listeners are stopped.
    """
    interval = 1.0 / rate_fps
    names = ['s%d' % (i + 1) for i in range(n_senders)]

    with Lab(a) as lab:
        for i, n in enumerate(names):
            lab.add(n, 5 * i, 0)
        lab.start()

        outs, readies = {}, []
        for n in names:
            tag = 'f1_%s_L%g' % (n, rate_fps)
            out, rdy = lab.listen(n, 5400, tag,
                                  loss=0.0, delay_ms=delay_ms,
                                  seed=zlib.crc32(('%s:f1:%g' % (n, rate_fps)).encode()))
            outs[n] = out
            readies.append(rdy)
        _assert_ready(readies)

        # Remove stale sender .done markers before launching.
        done_paths = ['%s/send_%s.done' % (TOOLS_DIR, n) for n in names]
        _clear_files(done_paths)

        for n in names:
            lab.send(n, 5400, frames, interval, background=True)

        # Join all senders via their .done markers, and check exit code.
        codes = _wait_done(done_paths, timeout=frames * interval + 30.0)
        if any(c is None or c != 0 for c in codes):
            raise RuntimeError('sender did not complete cleanly: %s'
                               % list(zip(done_paths, codes)))

        time.sleep(delay_ms / 1000.0 + 1.0)
        lab.kill_tools()
        time.sleep(0.5)

    shim_delays, e2e_delays = [], []
    received_pairs = 0
    for n in names:
        for src, seq, sts, rts, dts, ln in read_rx(outs[n]):
            if src != n:
                shim_delays.append((dts - rts) * 1000.0)
                e2e_delays.append((dts - sts) * 1000.0)
                received_pairs += 1
    expected = n_senders * (n_senders - 1) * frames
    return {
        'rate_fps': rate_fps,
        'n_senders': n_senders,
        'frames_per_sender': frames,
        'configured_ms': delay_ms,
        'shim_median_ms': percentile(shim_delays, 0.5),
        'shim_p95_ms': percentile(shim_delays, 0.95),
        'e2e_median_ms': percentile(e2e_delays, 0.5),
        'e2e_p95_ms': percentile(e2e_delays, 0.95),
        'received_fraction': received_pairs / float(expected) if expected else 0.0,
    }


def test_f1_shim_delay(a):
    """OP1: is the 414 ms delay a rate artifact, or is the shim broken at any rate?"""
    info('\n===== f1: shim delay at realistic and spike rates (OP1) =====\n')
    delay_ms = 20.0
    n_senders = 5
    frames = 200 if a.quick else 1000
    rates = [50.0, 20.0]
    if not a.quick:
        rates.append(333.0)

    rows = []
    for r in rates:
        info('  rate=%g fps/sender, %d senders, %d frames, delay=%g ms\n'
             % (r, n_senders, frames, delay_ms))
        row = isolated(_f1_delay_one, a, r, n_senders, frames, delay_ms, timeout=300.0)
        row['shim_ok'] = (
            row['shim_median_ms'] is not None
            and row['received_fraction'] >= 0.98
            and row['shim_median_ms'] <= delay_ms + 5.0
        )
        rows.append(row)
        info('    shim median %.1f ms, e2e median %.1f ms, received %.3f -> %s\n'
             % (row['shim_median_ms'] or 0, row['e2e_median_ms'] or 0,
                row['received_fraction'],
                'PASS' if row['shim_ok'] else 'FAIL'))

    realistic = [r for r in rows if r['rate_fps'] != 333.0]
    control = [r for r in rows if r['rate_fps'] == 333.0]
    realistic_ok = all(r['shim_ok'] for r in realistic)
    control_reproduces = (
        bool(control)
        and control[0]['shim_median_ms'] is not None
        and control[0]['shim_median_ms'] > delay_ms + 50.0
    )
    # Verdict = is the shim usable at realistic rates. Whether the 333 fps
    # control reproduces the old anomaly is informational, reported below.
    ok = realistic_ok
    headline = {
        'shim_median_by_rate': {str(r['rate_fps']): r['shim_median_ms'] for r in rows},
        'e2e_median_by_rate': {str(r['rate_fps']): r['e2e_median_ms'] for r in rows},
        'received_by_rate': {str(r['rate_fps']): r['received_fraction'] for r in rows},
        'realistic_ok': realistic_ok,
        'control_reproduces_414ms': control_reproduces if control else None,
        'control_run': bool(control),
        'decision': 'shim_delay_usable_at_realistic_rates'
                    if realistic_ok else 'use_tc_netem_delay',
    }
    save(a, 'f1_shim_delay', {
        'verdict': 'PASS' if ok else 'FAIL',
        'configured_delay_ms': delay_ms,
        'rows': rows,
        'headline': headline,
    })
    info('  f1 %s: shim %s at realistic rates; control rate %s\n'
         % ('PASS' if ok else 'FAIL',
            'usable' if realistic_ok else 'NOT usable',
            'reproduced 414 ms' if control_reproduces
            else 'did not reproduce 414 ms' if control else 'not run'))
    return ok


# ================================================================= f2: relay edge

def _f2_relay_edge_one(a, d, frames):
    with Lab(a) as lab:
        lab.add('hh', 0, 0, HAND_RANGE)
        lab.add('rr', d, 0, RELAY_RANGE)
        lab.start()
        res = {'distance_m': d}
        for tx, rx in (('hh', 'rr'), ('rr', 'hh')):
            out, rdy = lab.listen(rx, 5300, 'f2_relay_%d_%s' % (d, rx))
            _assert_ready([rdy])
            lab.send(tx, 5300, frames, 0.005)
            time.sleep(0.7)
            lab.kill_tools()
            time.sleep(0.3)
            key = '%s_to_%s' % (
                'handheld' if tx == 'hh' else 'relay',
                'relay' if tx == 'hh' else 'handheld',
            )
            res[key] = len(seqs_from(read_rx(out), tx)) / float(frames)
    return res


def test_f2_relay_edge(a):
    """OP2: find the relay edge between 500 and 600 m at 10 m resolution."""
    info('\n===== f2: relay edge at 10 m resolution (OP2) =====\n')
    frames = 100 if a.quick else 500
    dists = list(range(500, 601, 10))
    rows = []
    for d in dists:
        row = isolated(_f2_relay_edge_one, a, d, frames, timeout=180.0)
        rows.append(row)
        info('  %4d m: handheld->relay %.2f   relay->handheld %.2f\n'
             % (d, row['handheld_to_relay'], row['relay_to_handheld']))

    def edge_stats(key):
        series = [(r['distance_m'], r[key]) for r in rows]
        r90 = None
        for d, frac in series:
            if frac >= 0.9:
                r90 = d
            else:
                break
        r10 = None
        started = False
        for d, frac in series:
            if r90 is not None and d > r90:
                started = True
            if started and frac <= 0.1:
                r10 = d
                break
        non_mono = []
        for i in range(1, len(series)):
            prev_d, prev_f = series[i - 1]
            cur_d, cur_f = series[i]
            if cur_f > prev_f + 0.05:
                non_mono.append({
                    'at_m': cur_d,
                    'prev_distance_m': prev_d,
                    'prev_fraction': round(prev_f, 4),
                    'fraction': round(cur_f, 4),
                })
        return r90, r10, non_mono

    h2r90, h2r10, h2r_nonmono = edge_stats('handheld_to_relay')
    r2h90, r2h10, r2h_nonmono = edge_stats('relay_to_handheld')
    h2r_edge_beyond_max = all(r['handheld_to_relay'] >= 0.9 for r in rows)
    r2h_edge_beyond_max = all(r['relay_to_handheld'] >= 0.9 for r in rows)
    # Separation between r90 and r10 must exist for the bracket to be meaningful.
    threshold_identified = (
        h2r90 is not None and h2r10 is not None
        and r2h90 is not None and r2h10 is not None
        and not h2r_edge_beyond_max and not r2h_edge_beyond_max
    )
    monotonicity_ok = (not h2r_nonmono) and (not r2h_nonmono)
    # The test passes as soon as a threshold is identified; monotonicity is a
    # separate quality signal reported in the headline, not part of the verdict.
    ok = threshold_identified
    headline = {
        'handheld_to_relay_r90_m': h2r90,
        'handheld_to_relay_r10_m': h2r10,
        'relay_to_handheld_r90_m': r2h90,
        'relay_to_handheld_r10_m': r2h10,
        'symmetric': (h2r90, h2r10) == (r2h90, r2h10),
        'non_monotonic': h2r_nonmono + r2h_nonmono,
        'monotonicity_ok': monotonicity_ok,
        'edge_beyond_max_h2r': h2r_edge_beyond_max,
        'edge_beyond_max_r2h': r2h_edge_beyond_max,
        'interpretation': (
            'measured_threshold_bracket_monotonic' if monotonicity_ok
            else 'measured_threshold_bracket_non_monotonic'
        ),
    }
    save(a, 'f2_relay_edge', {
        'verdict': 'PASS' if ok else 'FAIL',
        'frames': frames,
        'distances_m': dists,
        'rows': rows,
        'headline': headline,
    })
    info('  bracket: h->r %s..%s m, r->h %s..%s m; monotonic=%s\n'
         % (h2r90, h2r10, r2h90, r2h10, monotonicity_ok))
    return ok


# ================================================================= f3: handheld txpower

def _parse_txpower(iw_output):
    for line in iw_output.splitlines():
        line = line.strip()
        if line.startswith('txpower'):
            parts = line.split()
            try:
                return float(parts[1])
            except (IndexError, ValueError):
                return None
    return None


def _f3_txpower_one(a, d, frames):
    """Capture iw before, at sender-start, and near sender-end.
    Sender completion is confirmed by exit code, not by a sleep guess."""
    with Lab(a) as lab:
        h = lab.add('hh', 0, 0, HAND_RANGE)
        r = lab.add('rr', d, 0, RELAY_RANGE)
        lab.start()
        iw_h_before = _parse_txpower(h.cmd('iw dev hh-wlan0 info 2>/dev/null'))
        iw_r_before = _parse_txpower(r.cmd('iw dev rr-wlan0 info 2>/dev/null'))
        attr_h = getattr(h.wintfs[0], 'txpower', None)
        attr_r = getattr(r.wintfs[0], 'txpower', None)
        out, rdy = lab.listen('rr', 5300, 'f3_relay_%d_rr' % d)
        _assert_ready([rdy])
        # Remove stale .done before launching.
        done = '%s/send_hh.done' % TOOLS_DIR
        _clear_files([done])
        send_s = frames * 0.005
        t_launch = mono()
        lab.send('hh', 5300, frames, 0.005, background=True)
        # Sample iw immediately after launch.
        time.sleep(0.1)
        iw_h_at_start = _parse_txpower(h.cmd('iw dev hh-wlan0 info 2>/dev/null'))
        iw_r_at_start = _parse_txpower(r.cmd('iw dev rr-wlan0 info 2>/dev/null'))
        # Sample again while the sender is still transmitting (~60% in).
        left = t_launch + 0.6 * send_s - mono()
        if left > 0:
            time.sleep(left)
        iw_h_during = _parse_txpower(h.cmd('iw dev hh-wlan0 info 2>/dev/null'))
        iw_r_during = _parse_txpower(r.cmd('iw dev rr-wlan0 info 2>/dev/null'))
        during_while_sending = _read_int_file(done) is None
        # Wait for sender to finish; check exit code.
        codes = _wait_done([done], timeout=send_s + 30.0)
        if codes != [0]:
            raise RuntimeError('F3 sender did not complete cleanly: %s' % codes)
        time.sleep(0.5)
        iw_h_after = _parse_txpower(h.cmd('iw dev hh-wlan0 info 2>/dev/null'))
        iw_r_after = _parse_txpower(r.cmd('iw dev rr-wlan0 info 2>/dev/null'))
        lab.kill_tools()
        time.sleep(0.3)
        frac = len(seqs_from(read_rx(out), 'hh')) / float(frames)
    return {
        'distance_m': d,
        'iw_handheld_before_dbm': iw_h_before,
        'iw_handheld_at_start_dbm': iw_h_at_start,
        'iw_handheld_during_dbm': iw_h_during,
        'iw_relay_before_dbm': iw_r_before,
        'iw_relay_at_start_dbm': iw_r_at_start,
        'iw_relay_during_dbm': iw_r_during,
        'iw_handheld_after_dbm': iw_h_after,
        'iw_relay_after_dbm': iw_r_after,
        'during_sampled_while_sending': during_while_sending,
        'attr_handheld_dbm': attr_h,
        'attr_relay_dbm': attr_r,
        'handheld_to_relay_fraction': frac,
    }


def test_f3_handheld_txpower(a):
    """OP3: does the handheld report a configured 20 dBm transmit power during
    the relay test? The "during" sample is taken while the sender is running.

    Note: `iw dev ... info` reports the configured interface setting, not an
    independently measured RF power. The claim in the report will be phrased
    as "configured transmit power is 20 dBm".
    """
    info('\n===== f3: handheld configured txpower during relay test (OP3) =====\n')
    dists = [100, 200, 400, 500]
    frames = 100 if a.quick else 500
    rows = []
    for d in dists:
        row = isolated(_f3_txpower_one, a, d, frames, timeout=180.0)
        rows.append(row)
        info('  %3d m: iw h=%.1f/%.1f/%.1f (before/start/during) dBm, '
             'iw r=%.1f/%.1f/%.1f dBm, attr h=%s r=%s, h->r %.2f\n'
             % (d,
                row['iw_handheld_before_dbm'] or 0,
                row['iw_handheld_at_start_dbm'] or 0,
                row['iw_handheld_during_dbm'] or 0,
                row['iw_relay_before_dbm'] or 0,
                row['iw_relay_at_start_dbm'] or 0,
                row['iw_relay_during_dbm'] or 0,
                row['attr_handheld_dbm'],
                row['attr_relay_dbm'],
                row['handheld_to_relay_fraction']))

    iw_fields = (
        [r['iw_handheld_at_start_dbm'] for r in rows]
        + [r['iw_handheld_during_dbm'] for r in rows]
        + [r['iw_relay_at_start_dbm'] for r in rows]
        + [r['iw_relay_during_dbm'] for r in rows]
    )
    attr_fields = (
        [r['attr_handheld_dbm'] for r in rows]
        + [r['attr_relay_dbm'] for r in rows]
    )
    expected = 20.0
    tol = 1.0
    if any(v is None for v in iw_fields):
        diagnosis = 'inconclusive_iw_missing'
        ok = False
    elif not all(r['during_sampled_while_sending'] for r in rows):
        diagnosis = 'inconclusive_sender_finished_before_during_sample'
        ok = False
    elif any(v is None for v in attr_fields):
        diagnosis = 'inconclusive_attribute_missing'
        ok = False
    else:
        handheld_ok = all(
            abs(v - expected) <= tol for v in [r['iw_handheld_during_dbm'] for r in rows]
        )
        attr_matches_iw = all(
            abs(r['attr_handheld_dbm'] - r['iw_handheld_during_dbm']) <= tol
            and abs(r['attr_relay_dbm'] - r['iw_relay_during_dbm']) <= tol
            for r in rows
        )
        handheld_raised = any(
            r['iw_handheld_during_dbm'] > expected + tol for r in rows
        )
        frac_at_500 = next(
            (r['handheld_to_relay_fraction'] for r in rows if r['distance_m'] == 500),
            None,
        )
        if handheld_raised:
            diagnosis = 'handheld_configured_txpower_raised_during_test'
            ok = False
        elif not handheld_ok:
            diagnosis = 'handheld_configured_txpower_not_20_dbm'
            ok = False
        elif not attr_matches_iw:
            diagnosis = 'iw_and_attribute_disagree'
            ok = False
        elif frac_at_500 is not None and frac_at_500 >= 0.9:
            diagnosis = 'wmediumd_allows_handheld_to_relay_at_500m'
            ok = True
        else:
            diagnosis = 'observation_not_reproduced'
            ok = False
    headline = {
        'expected_configured_handheld_dbm': expected,
        'measured_configured_handheld_dbm': [
            {'at_start': r['iw_handheld_at_start_dbm'],
             'during': r['iw_handheld_during_dbm']}
            for r in rows
        ],
        'handheld_to_relay_at_500m': next(
            (r['handheld_to_relay_fraction'] for r in rows if r['distance_m'] == 500),
            None,
        ),
        'qualifier': (
            'iw reports the configured interface transmit power, '
            'not independently measured RF power'
        ),
        'diagnosis': diagnosis,
    }
    save(a, 'f3_handheld_txpower', {
        'verdict': 'PASS' if ok else 'FAIL',
        'rows': rows,
        'headline': headline,
    })
    info('  f3 %s: %s\n' % ('PASS' if ok else 'FAIL', diagnosis))
    return ok


# ================================================================= f4: scale at 20

def _f4_scale_20_one(a, n, duration):
    from spike import _scale_one
    return _scale_one(a, n, duration)


def test_f4_scale_20(a):
    """OP4: does 20 nodes pass with margin, as a documented fallback?"""
    info('\n===== f4: scale at 20 nodes (OP4) =====\n')
    duration = 20.0 if a.quick else 30.0
    n = 20
    row = isolated(_f4_scale_20_one, a, n, duration, timeout=300.0)
    passed = bool(row['pass'])
    hello = row.get('hello_delivery_ratio')
    reach = row.get('flood_reach_mean')
    cpu = row.get('cpu_busy_pct')
    margin_ok = (
        hello is not None and hello >= 0.99
        and reach is not None and reach >= 0.99
        and cpu is not None and cpu <= 20.0
    )
    final_pass = passed and margin_ok
    save(a, 'f4_scale_20', {
        'verdict': 'PASS' if final_pass else 'FAIL',
        'nodes': n,
        'row': row,
        'margin_ok': margin_ok,
        'headline': {
            'nodes': n,
            'hello_delivery_ratio': hello,
            'flood_reach_mean': reach,
            'cpu_busy_pct': cpu,
            'margin_ok': margin_ok,
            'final_pass': final_pass,
        },
    })
    info('  f4 %s: hello %s, reach %s, cpu %s%%, margin_ok=%s\n'
         % ('PASS' if final_pass else 'FAIL',
            hello, reach, cpu, margin_ok))
    return final_pass


# ================================================================= f5: startup stabilization

# Drain listener: waits until the receive buffer is quiet before signalling
# ready. The parent waits for .ready, THEN starts the sender. So the drain
# phase cannot consume test packets. Records the drained count to a file.
LB_DRAIN_SOURCE = r'''
import argparse, heapq, json, random, select, socket, sys, time


def mono():
    return time.monotonic()


def sock(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4194304)
    except OSError:
        pass
    s.bind(('', port))
    return s


def cmd_listen_drain(a):
    s = sock(a.port)
    s.setblocking(False)
    drain_start = mono()
    drain_deadline = drain_start + a.drain_max_s
    drained = 0
    quiet_since = mono()
    while mono() < drain_deadline:
        try:
            s.recvfrom(4096)
            drained += 1
            quiet_since = mono()
        except BlockingIOError:
            if mono() - quiet_since >= a.drain_s:
                break
            time.sleep(0.02)
    if a.count_file:
        with open(a.count_file, 'w') as f:
            f.write(str(drained))
    sys.stderr.write('drained %d packets in %.3fs\n'
                     % (drained, mono() - drain_start))
    sys.stderr.flush()
    open(a.ready, 'w').close()

    out = open(a.out, 'w', buffering=1)
    rng = random.Random(a.seed)
    p = a.loss / 100.0
    delay = a.delay_ms / 1000.0
    pending, n, dropped, received = [], 0, 0, 0
    end = mono() + a.duration if a.duration > 0 else None
    while True:
        now = mono()
        if end is not None and now >= end:
            break
        timeout = 0.25
        if pending:
            timeout = max(0.0, min(timeout, pending[0][0] - now))
        select.select([s], [], [], timeout)
        while True:
            try:
                d, _ = s.recvfrom(4096)
            except BlockingIOError:
                break
            rts = mono()
            parts = d.split(None, 3)
            if len(parts) < 3:
                continue
            try:
                item = (rts + delay, n, parts[0].decode(), int(parts[1]),
                        float(parts[2]), rts, len(d))
            except ValueError:
                continue
            received += 1
            if p > 0 and rng.random() < p:
                dropped += 1
                continue
            heapq.heappush(pending, item)
            n += 1
        now = mono()
        while pending and pending[0][0] <= now:
            _, _, src, seq, sts, rts, ln = heapq.heappop(pending)
            out.write('%s %d %.6f %.6f %.6f %d\n'
                      % (src, seq, sts, rts, mono(), ln))
    for _, _, src, seq, sts, rts, ln in sorted(pending):
        out.write('%s %d %.6f %.6f %.6f %d\n'
                  % (src, seq, sts, rts, mono(), ln))
    out.write('# received %d dropped %d\n' % (received, dropped))
    out.close()


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    l = sub.add_parser('listen-drain')
    l.add_argument('--port', type=int, required=True)
    l.add_argument('--out', required=True)
    l.add_argument('--ready', default='')
    l.add_argument('--count-file', dest='count_file', default='')
    l.add_argument('--loss', type=float, default=0.0)
    l.add_argument('--delay-ms', dest='delay_ms', type=float, default=0.0)
    l.add_argument('--seed', type=int, default=0)
    l.add_argument('--duration', type=float, default=0.0)
    l.add_argument('--drain-s', dest='drain_s', type=float, default=0.3)
    l.add_argument('--drain-max-s', dest='drain_max_s', type=float, default=2.0)
    a = ap.parse_args()
    {'listen-drain': cmd_listen_drain}[a.cmd](a)


main()
'''

LB_DRAIN_PATH = os.path.join(TOOLS_DIR, 'lb_drain.py')
EXPECTED_EXIT_CODES = (0, 143)


def _write_drain_helper():
    os.makedirs(TOOLS_DIR, exist_ok=True)
    with open(LB_DRAIN_PATH, 'w') as f:
        f.write(LB_DRAIN_SOURCE)
    os.chmod(LB_DRAIN_PATH, 0o755)
    return LB_DRAIN_PATH


def _f5_range_one(a, frames, use_drain):
    """Reproduce the spike's 16-receiver topology, with helper exit-code checks.
    Each helper's exit code is recorded to <tag>.exit by a shell wrapper."""
    dists = list(range(50, 201, 10))
    result = {
        'fractions': {}, 'drained_counts': {}, 'helper_exits': {},
        'helper_errors': {}, 'missing_exit': [], 'missing_drained': [],
    }
    with Lab(a) as lab:
        lab.add('tx', 0, 0)
        for d in dists:
            lab.add('r%03d' % d, d, 0)
        lab.start()
        outs, readies = {}, []
        helper = LB_DRAIN_PATH if use_drain else LB
        mode = 'drain' if use_drain else 'plain'
        for d in dists:
            name = 'r%03d' % d
            tag = 'f5_%s_%s' % (mode, name)
            out = '%s/%s.rx' % (TOOLS_DIR, tag)
            rdy = '%s/%s.ready' % (TOOLS_DIR, tag)
            out_helper = '%s/%s.out' % (TOOLS_DIR, tag)
            exit_file = '%s/%s.exit' % (TOOLS_DIR, tag)
            count_file = '%s/%s.drained' % (TOOLS_DIR, tag) if use_drain else None
            # Remove ALL stale files, including .drained and .exit.
            _clear_files([out, rdy, out_helper, exit_file, count_file])
            if use_drain:
                cmd = ('listen-drain --port 5700 --out %s --ready %s '
                       '--count-file %s --duration 20 '
                       '--drain-s 0.3 --drain-max-s 2.0'
                       % (out, rdy, count_file))
            else:
                cmd = ('listen --port 5700 --out %s --ready %s --duration 20'
                       % (out, rdy))
            # Shell wrapper records exit code to .exit.
            # Parentheses matter: the whole subshell is backgrounded, so the
            # station shell returns immediately and the exit code is captured.
            lab.stas[name].cmd(
                '(python3 -u %s %s > %s 2>&1; echo $? > %s) &'
                % (helper, cmd, out_helper, exit_file)
            )
            outs[d] = out
            readies.append(rdy)
            result['helper_exits'][str(d)] = exit_file
            if count_file:
                result['drained_counts'][str(d)] = count_file
        _assert_ready(readies)
        lab.send('tx', 5700, frames, 0.005)
        time.sleep(1.0)
        lab.kill_tools()
        # kill_tools() only matches lb.py; stop the drain helper explicitly.
        lab.stas['tx'].cmd('pkill -f %s 2>/dev/null' % LB_DRAIN_PATH)
        # Listeners are stopped by SIGTERM, so wait for every .exit file.
        _wait_done([result['helper_exits'][str(d)] for d in dists], timeout=10.0)
        time.sleep(0.3)
        # Helper exit-code checks. .exit must exist; 0 (ran to completion) and
        # 143 (our SIGTERM, 128+15) are the expected clean outcomes.
        for d in dists:
            exit_file = result['helper_exits'][str(d)]
            code = _read_int_file(exit_file)
            if code is None:
                result['missing_exit'].append(str(d))
            elif code not in EXPECTED_EXIT_CODES:
                result['helper_errors'][str(d)] = {
                    'exit_code': code,
                    'tail': _read_helper_output(
                        '%s/f5_%s_r%03d.out' % (TOOLS_DIR, mode, d)
                    ),
                }
    for d in dists:
        got = len(seqs_from(read_rx(outs[d]), 'tx'))
        result['fractions'][d] = got / float(frames)
    # In drain mode, every count file must exist and be readable.
    if use_drain:
        for d in dists:
            cf = result['drained_counts'][str(d)]
            if _read_int_file(cf) is None:
                result['missing_drained'].append(str(d))
    return result


def test_f5_startup_loss(a):
    """OP5: does waiting for the receive buffer to settle before transmission
    change delivery at 16 receivers?

    This tests stabilization, not buffer loss directly. Plain mode signals
    ready on bind; drain mode waits for the receive buffer to be quiet, then
    signals ready; both variants wait for all readiness signals before the
    sender starts. The only difference is whether the receiver was allowed to
    settle before the first test packet arrived.
    """
    info('\n===== f5: startup stabilization at 16 receivers (OP5) =====\n')
    _write_drain_helper()
    frames = 200 if a.quick else 1000
    plain = isolated(_f5_range_one, a, frames, False, timeout=300.0)
    drain = isolated(_f5_range_one, a, frames, True, timeout=300.0)
    dists = sorted(plain['fractions'])
    info('  dist   plain       drain        diff     drained\n')
    drained_total = 0
    for d in dists:
        p = plain['fractions'][d]
        dv = drain['fractions'][d]
        drained = _read_int_file(drain['drained_counts'].get(str(d))) if \
            'drained_counts' in drain else None
        drained_total += drained or 0
        info('  %4d   %.4f      %.4f      %+.4f    %s\n'
             % (d, p, dv, dv - p, drained if drained is not None else '-'))

    near_dists = [d for d in dists if d <= 100]
    far_dists = [d for d in dists if d > 100]
    plain_near = statistics.mean(plain['fractions'][d] for d in near_dists)
    drain_near = statistics.mean(drain['fractions'][d] for d in near_dists)
    drain_far_max = max(drain['fractions'][d] for d in far_dists) if far_dists else None

    # Two conditions, combined with and, not or.
    recovered = (drain_near >= 0.999) and (drain_near >= plain_near + 0.005)
    no_difference = abs(drain_near - plain_near) < 0.002
    far_ok = (drain_far_max is not None) and (drain_far_max <= 0.01)

    # Helper failures (missing .exit, nonzero exit, missing .drained)
    any_helper_failure = bool(
        plain.get('helper_errors') or drain.get('helper_errors')
        or plain.get('missing_exit') or drain.get('missing_exit')
        or drain.get('missing_drained')
    )

    if any_helper_failure:
        diagnosis = 'invalid_helper_errors'
        ok = False
    elif recovered and far_ok:
        diagnosis = 'startup_stabilization_effect'
        ok = True
    elif no_difference and plain_near < 0.999:
        diagnosis = 'no_stabilization_effect'
        ok = False
    else:
        diagnosis = 'inconclusive'
        ok = False

    save(a, 'f5_startup_loss', {
        'verdict': 'PASS' if ok else 'FAIL',
        'frames': frames,
        'dists': dists,
        'plain': {str(d): plain['fractions'][d] for d in dists},
        'drain': {str(d): drain['fractions'][d] for d in dists},
        'drained_counts': {
            str(d): _read_int_file(drain['drained_counts'].get(str(d)))
            if 'drained_counts' in drain else None
            for d in dists
        },
        'drained_total': drained_total,
        'plain_helper_errors': plain.get('helper_errors', {}),
        'plain_missing_exit': plain.get('missing_exit', []),
        'drain_helper_errors': drain.get('helper_errors', {}),
        'drain_missing_exit': drain.get('missing_exit', []),
        'drain_missing_drained': drain.get('missing_drained', []),
        'plain_near_mean': plain_near,
        'drain_near_mean': drain_near,
        'drain_far_max': drain_far_max,
        'headline': {
            'plain_near_mean': plain_near,
            'drain_near_mean': drain_near,
            'recovered': recovered,
            'no_difference': no_difference,
            'far_ok': far_ok,
            'drained_total': drained_total,
            'diagnosis': diagnosis,
            'qualifier': (
                'drain phase cannot consume test packets; this measures '
                'stabilization, not buffer loss'
            ),
        },
    })
    info('  f5 %s: %s (plain %.4f, drain %.4f, drained %d, far_ok %s)\n'
         % ('PASS' if ok else 'FAIL',
            diagnosis, plain_near, drain_near, drained_total, far_ok))
    return ok


# ================================================================= summary

def write_followup_summary(a):
    lines = ['# WP0 follow-up summary', '',
             'Path-loss exponent used: %s' % a.exp, '',
             '| Test | Open point | Verdict | Headline |',
             '|---|---|---|---|']
    rows = [
        ('f1_shim_delay',       'OP1', 'f1_shim_delay.json'),
        ('f2_relay_edge',       'OP2', 'f2_relay_edge.json'),
        ('f3_handheld_txpower', 'OP3', 'f3_handheld_txpower.json'),
        ('f4_scale_20',         'OP4', 'f4_scale_20.json'),
        ('f5_startup_loss',     'OP5', 'f5_startup_loss.json'),
    ]
    for name, op, _fname in rows:
        d = load(a, name)
        if d is None:
            lines.append('| %s | %s | not run | |' % (name, op))
        else:
            lines.append('| %s | %s | %s | %s |'
                         % (name, op, d.get('verdict', '?'),
                            json.dumps(d.get('headline', {}), default=str)[:300]))
    path = os.path.join(a.results, 'followup_summary.md')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    info('\n  saved %s\n' % path)


# ================================================================= main

RESULT_STEM = {
    'f1': 'f1_shim_delay', 'f2': 'f2_relay_edge', 'f3': 'f3_handheld_txpower',
    'f4': 'f4_scale_20', 'f5': 'f5_startup_loss',
}

TESTS = {
    'f1': test_f1_shim_delay,
    'f2': test_f2_relay_edge,
    'f3': test_f3_handheld_txpower,
    'f4': test_f4_scale_20,
    'f5': test_f5_startup_loss,
}
DEFAULT_ORDER = ['f1', 'f2', 'f3', 'f4', 'f5']


def parse_args():
    p = argparse.ArgumentParser(description='WP0 follow-up tests (see the module docstring).')
    p.add_argument('--only', default='', help='comma-separated subset of: ' + ','.join(DEFAULT_ORDER))
    p.add_argument('--exp', type=float, default=4.0, help='logDistance path-loss exponent')
    p.add_argument('--quick', action='store_true', help='fewer frames for a dry run')
    p.add_argument('--results', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'results'))
    a = p.parse_args()
    a.relay_tx = None
    a.hand_tx = None
    return a


def main():
    a = parse_args()
    if os.geteuid() != 0:
        sys.exit('Run with sudo: Mininet-WiFi needs root.')
    if not HAVE_MN:
        sys.exit('Could not import Mininet-WiFi. Run this on the emulation host.')
    setLogLevel('info')          # same as spike.py; without it info() output is hidden
    os.makedirs(a.results, exist_ok=True)
    write_tools()
    wanted = [t for t in (a.only.split(',') if a.only else DEFAULT_ORDER) if t]
    unknown = [t for t in wanted if t not in TESTS]
    if unknown:
        sys.exit('unknown test(s): %s' % ', '.join(unknown))

    failed = []
    for name in wanted:
        try:
            ok = TESTS[name](a)
            if not ok:
                failed.append(name)
        except Exception:
            tb = traceback.format_exc()
            info('\n  !!! %s failed:\n%s\n' % (name, tb))
            save(a, RESULT_STEM[name], {'verdict': 'ERROR', 'error': tb[-1500:],
                                        'headline': {'error': tb[-300:]}})
            failed.append(name)
            force_cleanup()
    write_followup_summary(a)

    if failed:
        info('\n  FAILED: %s\n' % ', '.join(failed))
        sys.exit(1)


if __name__ == '__main__':
    main()