#!/usr/bin/env python3
"""
WP0 spike for Lifeline: characterize the Mininet-WiFi emulator.

This is NOT a test of Lifeline code. It measures what the emulator can do, and
produces the numbers the scenario file (C4) and the node runtime need.

Tests (each builds a fresh topology; names are for --only):
  env        8  Python, cryptography, ports 5000/5001, shared clock, inside a station
  range      1  Handheld BROADCAST range curve (reception vs distance), r90 and edge width
  relay      2  Relay-class (range=500) vs handheld (range=100): range and symmetry
  inject     4  Receiver-side loss + delay shim: accuracy and per-receiver independence,
                plus a probe of whether tc netem delay breaks broadcast
  mobility   3  A moving station: does reception follow position (compared with `range`)
  repeat     5  Same setup 3 times: emulator noise floor at the range edge
  scale      6  5/15/30 stations: CPU, close-range loss under load, flood reach
  lifecycle  7  Start/stop cycles: leaks and per-run overhead

After the tests, if range results exist, a connectivity re-check is written
(connectivity.md) using the MEASURED ranges.

Run (Linux, as root):
    sudo python3 spike.py                      # everything, in a sensible order
    sudo python3 spike.py --only env,range     # just those
    sudo python3 spike.py --quick --only range # fast dry run (fewer frames)
    python3 spike.py --selftest                # no Mininet needed: checks the helper code

Results: results/<test>.json, results/summary.md, results/connectivity.md
Design notes:
  * Lifeline only ever broadcasts (contract C2), so every range test uses BROADCAST.
  * Loss and delay are injected by a seeded receiver-side shim in our own code,
    not by tc netem or wmediumd, so loss is independent per receiver.
  * Mobility is driven by us (setPosition on a schedule), so it is deterministic.
  * All times use time.monotonic(), which is shared by every process on the host.
"""

import argparse
import json
import math
import multiprocessing as mp
import os
import platform
import random
import statistics
import subprocess
import sys
import time
import traceback
import zlib
from collections import defaultdict

try:
    from mininet.log import setLogLevel, info
    from mn_wifi.net import Mininet_wifi
    from mn_wifi.link import adhoc, wmediumd
    from mn_wifi.wmediumdConnector import interference
    HAVE_MN = True
except ImportError:                      # lets --selftest and --help run anywhere
    HAVE_MN = False

    def info(msg):
        sys.stdout.write(msg)
        sys.stdout.flush()

    def setLogLevel(_level):
        pass


# ---------------------------------------------------------------- constants

TOOLS_DIR = '/tmp/lifeline_spike'
LB = os.path.join(TOOLS_DIR, 'lb.py')
BCAST = '10.255.255.255'                # broadcast address of 10.0.0.0/8
HAND_RANGE = 100
RELAY_RANGE = 500
DEFAULT_ORDER = ['env', 'range', 'relay', 'inject', 'mobility', 'repeat', 'scale', 'lifecycle']


# ---------------------------------------------------------------- helper program run inside stations
# One small program with sub-commands, written to /tmp and run by every station.

LB_SOURCE = r'''
import argparse, heapq, json, random, select, socket, sys, time, zlib


def mono():
    return time.monotonic()


def sock(port, bind=True):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4194304)
    except OSError:
        pass
    if bind:
        s.bind(('', port))
    return s


def touch(path):
    if path:
        open(path, 'w').close()


def cmd_listen(a):
    """Receive frames; apply seeded independent loss and fixed delay; log each delivery.
    Line format: src seq send_ts recv_ts deliver_ts length"""
    s = sock(a.port)
    s.setblocking(False)
    rng = random.Random(a.seed)
    p = a.loss / 100.0
    delay = a.delay_ms / 1000.0
    out = open(a.out, 'w', buffering=1)
    pending, n, dropped, received = [], 0, 0, 0
    end = mono() + a.duration if a.duration > 0 else None
    touch(a.ready)
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
                item = (rts + delay, n, parts[0].decode(), int(parts[1]), float(parts[2]), rts, len(d))
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
            out.write('%s %d %.6f %.6f %.6f %d\n' % (src, seq, sts, rts, mono(), ln))
    for _, _, src, seq, sts, rts, ln in sorted(pending):
        out.write('%s %d %.6f %.6f %.6f %d\n' % (src, seq, sts, rts, mono(), ln))
    out.write('# received %d dropped %d\n' % (received, dropped))
    out.close()


def cmd_send(a):
    """Send numbered frames at a fixed rate (absolute schedule); optionally log send times."""
    s = sock(0, bind=False)
    try:
        s.setsockopt(socket.IPPROTO_IP, 10, 2)     # IP_MTU_DISCOVER = do not fragment
    except OSError:
        pass
    log = open(a.log, 'w', buffering=1) if a.log else None
    t0 = mono()
    for seq in range(a.count):
        ts = mono()
        header = ('%s %d %.6f ' % (a.name, seq, ts)).encode()
        s.sendto(header.ljust(a.size, b'x'), (a.dest, a.port))
        if log:
            log.write('%d %.6f\n' % (seq, ts))
        left = t0 + (seq + 1) * a.interval - mono()
        if left > 0:
            time.sleep(left)
    if log:
        log.close()


def cmd_flood(a):
    """Mini protocol for the load test: periodic HELLO, plus first-copy rebroadcast of data
    messages with random jitter (like the baseline). Writes a JSON summary at the end."""
    rng = random.Random(zlib.crc32(a.name.encode()))
    s = sock(a.port)
    s.setblocking(False)
    start = mono()
    end = start + a.duration
    ev, ctr = [], [0]

    def push(t, kind, data):
        ctr[0] += 1
        heapq.heappush(ev, (t, ctr[0], kind, data))

    def frame(kind, who, idx):
        return ('%s %s %d %.6f ' % (kind, who, idx, mono())).encode().ljust(a.size, b'x')

    push(start + rng.uniform(0, a.hello), 'H', 0)
    if a.origin:
        for i in range(a.burst):
            push(start + a.burst_at + i * a.burst_gap, 'O', i)
    seen, origin_tx = {}, {}
    hello_tx = hello_rx = data_tx = data_rx = 0
    touch(a.ready)
    while True:
        now = mono()
        if now >= end:
            break
        timeout = 0.2
        if ev:
            timeout = max(0.0, min(timeout, ev[0][0] - now))
        select.select([s], [], [], timeout)
        while True:
            try:
                d, _ = s.recvfrom(4096)
            except BlockingIOError:
                break
            rts = mono()
            p = d.split(None, 4)
            if len(p) < 4:
                continue
            kind, who = p[0].decode(), p[1].decode()
            if kind == 'H':
                if who != a.name:
                    hello_rx += 1
            elif kind == 'D':
                data_rx += 1
                key = who + ':' + p[2].decode()
                if key not in seen:
                    seen[key] = rts
                    push(rts + rng.uniform(0, a.jitter), 'R', (who, int(p[2])))
        now = mono()
        while ev and ev[0][0] <= now:
            _, _, kind, data = heapq.heappop(ev)
            if kind == 'H':
                s.sendto(frame('H', a.name, hello_tx), (a.dest, a.port))
                hello_tx += 1
                push(mono() + a.hello * rng.uniform(0.75, 1.25), 'H', 0)
            elif kind == 'O':
                key = a.name + ':' + str(data)
                origin_tx[key] = mono()
                seen[key] = mono()
                s.sendto(frame('D', a.name, data), (a.dest, a.port))
                data_tx += 1
            elif kind == 'R':
                s.sendto(frame('D', data[0], data[1]), (a.dest, a.port))
                data_tx += 1
    first_rx = {k: v for k, v in seen.items() if k not in origin_tx}
    with open(a.out, 'w') as f:
        json.dump({'name': a.name, 'hello_tx': hello_tx, 'hello_rx': hello_rx,
                   'data_tx': data_tx, 'data_rx': data_rx,
                   'first_rx': first_rx, 'origin_tx': origin_tx}, f)


def cmd_env(a):
    out = {'python': sys.version.split()[0], 'exec': sys.executable,
           'monotonic': mono(), 'time': time.time()}
    try:
        import cryptography
        out['cryptography'] = cryptography.__version__
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        k = Ed25519PrivateKey.generate()
        sig = k.sign(b'x')
        k.public_key().verify(sig, b'x')
        out['ed25519_ok'] = True
    except Exception as e:
        out['cryptography_error'] = repr(e)
    try:
        import tomllib                                   # noqa: F401
        out['tomllib'] = True
    except Exception:
        out['tomllib'] = False
    binds = {}
    for port in (5000, 5001):
        try:
            sock(port).close()
            binds[str(port)] = True
        except Exception as e:
            binds[str(port)] = repr(e)
    out['bind'] = binds
    print(json.dumps(out))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    l = sub.add_parser('listen')
    l.add_argument('--port', type=int, required=True)
    l.add_argument('--out', required=True)
    l.add_argument('--ready', default='')
    l.add_argument('--loss', type=float, default=0.0)
    l.add_argument('--delay-ms', dest='delay_ms', type=float, default=0.0)
    l.add_argument('--seed', type=int, default=0)
    l.add_argument('--duration', type=float, default=0.0)
    se = sub.add_parser('send')
    se.add_argument('--name', required=True)
    se.add_argument('--count', type=int, required=True)
    se.add_argument('--interval', type=float, required=True)
    se.add_argument('--size', type=int, default=333)
    se.add_argument('--dest', required=True)
    se.add_argument('--port', type=int, required=True)
    se.add_argument('--log', default='')
    f = sub.add_parser('flood')
    f.add_argument('--name', required=True)
    f.add_argument('--port', type=int, required=True)
    f.add_argument('--dest', required=True)
    f.add_argument('--out', required=True)
    f.add_argument('--ready', default='')
    f.add_argument('--duration', type=float, default=30.0)
    f.add_argument('--hello', type=float, default=2.0)
    f.add_argument('--size', type=int, default=120)
    f.add_argument('--jitter', type=float, default=0.05)
    f.add_argument('--origin', action='store_true')
    f.add_argument('--burst', type=int, default=10)
    f.add_argument('--burst-at', dest='burst_at', type=float, default=10.0)
    f.add_argument('--burst-gap', dest='burst_gap', type=float, default=0.1)
    sub.add_parser('env')
    a = ap.parse_args()
    {'listen': cmd_listen, 'send': cmd_send, 'flood': cmd_flood, 'env': cmd_env}[a.cmd](a)


main()
'''


# ---------------------------------------------------------------- small utilities

def mono():
    return time.monotonic()


def write_tools():
    os.makedirs(TOOLS_DIR, exist_ok=True)
    with open(LB, 'w') as f:
        f.write(LB_SOURCE)
    os.chmod(LB, 0o755)


def percentile(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def read_rx(path):
    """Rows of (src, seq, send_ts, recv_ts, deliver_ts, length) from a listener log."""
    rows = []
    try:
        with open(path) as f:
            for line in f:
                if line.startswith('#'):
                    continue
                p = line.split()
                if len(p) == 6:
                    rows.append((p[0], int(p[1]), float(p[2]), float(p[3]), float(p[4]), int(p[5])))
    except FileNotFoundError:
        pass
    return rows


def read_sent(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                p = line.split()
                if len(p) == 2:
                    rows.append((int(p[0]), float(p[1])))
    except FileNotFoundError:
        pass
    return rows


def seqs_from(rows, src):
    return {r[1] for r in rows if r[0] == src}


def cpu_times():
    with open('/proc/stat') as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return idle, sum(v)


def mem_available_mb():
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable'):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return None


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ''


def host_snapshot():
    """Counts used to detect leaks between runs."""
    ifaces = len(os.listdir('/sys/class/net')) if os.path.isdir('/sys/class/net') else -1
    wlan = sh('iw dev 2>/dev/null | grep -c Interface').strip()
    wm = sh('pgrep -x wmediumd | wc -l').strip()
    return {'net_interfaces': ifaces,
            'wifi_interfaces': int(wlan) if wlan.isdigit() else None,
            'wmediumd_processes': int(wm) if wm.isdigit() else None}


def force_cleanup():
    try:
        from mn_wifi.clean import Cleanup
        Cleanup.cleanup_wifi()
    except Exception:
        pass
    sh('mn -c >/dev/null 2>&1')


def isolated(fn, *args):
    """Run fn(*args) in a forked child process and return its result.

    Mininet-WiFi keeps global state that survives net.stop(): the first network built in a process
    works, later ones can crash (AttributeError: 'managed' object has no attribute 'wmIface').
    The parent never builds a network itself, so every fork starts from clean library state.
    fn must return something picklable (dicts, lists, numbers, strings)."""
    ctx = mp.get_context('fork')
    q = ctx.Queue()

    def child():
        try:
            q.put(('ok', fn(*args)))
        except BaseException:
            q.put(('err', traceback.format_exc()))

    p = ctx.Process(target=child)
    p.start()
    while True:
        try:
            kind, val = q.get(timeout=1.0)
            break
        except Exception:
            if not p.is_alive():
                try:
                    kind, val = q.get(timeout=1.0)
                except Exception:
                    kind, val = 'err', 'child process exited with code %s' % p.exitcode
                break
    p.join()
    if kind == 'err':
        raise RuntimeError(val)
    return val


def save(a, name, data):
    os.makedirs(a.results, exist_ok=True)
    data = dict(data)
    data.setdefault('meta', {})
    data['meta'].update({'exponent': a.exp, 'quick': a.quick, 'host_python': platform.python_version(),
                         'when': time.strftime('%Y-%m-%d %H:%M:%S')})
    path = os.path.join(a.results, name + '.json')
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, default=str)
    info('  saved %s\n' % path)


def load(a, name):
    try:
        with open(os.path.join(a.results, name + '.json')) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- emulator wrapper

class Lab(object):
    """One fresh Mininet-WiFi topology. Use as a context manager."""

    def __init__(self, a, wm=True):
        self.a, self.wm = a, wm
        self.net, self.stas, self.ips = None, {}, {}
        self.build_s = self.stop_s = None

    def __enter__(self):
        if not HAVE_MN:
            raise RuntimeError('Mininet-WiFi is not importable here; run on the emulation host with sudo.')
        self.net = Mininet_wifi(link=wmediumd, wmediumd_mode=interference) if self.wm else Mininet_wifi()
        return self

    def add(self, name, x, y, rng_m=HAND_RANGE):
        idx = len(self.stas) + 1
        if idx > 250:
            raise RuntimeError('too many stations for the 10.0.0.0/8 plan used here')
        ip = '10.0.0.%d' % idx
        kw = {}
        tx = self.a.relay_tx if rng_m == RELAY_RANGE else self.a.hand_tx
        if tx is None:
            kw['range'] = rng_m
        else:
            kw['txpower'] = tx                  # fallback: skip range= and set power directly
        self.stas[name] = self.net.addStation(name, ip=ip + '/8', position='%s,%s,0' % (x, y), **kw)
        self.ips[name] = ip
        return self.stas[name]

    def start(self):
        t = mono()
        self.net.setPropagationModel(model='logDistance', exp=self.a.exp)
        self.net.configureNodes()
        for s in self.stas.values():
            self.net.addLink(s, cls=adhoc, intf='%s-wlan0' % s.name, ssid='adhocNet', mode='g', channel=5)
        self.net.build()
        time.sleep(5.0 + 0.15 * len(self.stas))              # IBSS join
        self.build_s = mono() - t

    def run_bg(self, sta, args_str, tag):
        sta.cmd('python3 -u %s %s > %s/%s.out 2>&1 &' % (LB, args_str, TOOLS_DIR, tag))

    def listen(self, name, port, tag, loss=0.0, delay_ms=0.0, seed=0, duration=0):
        out = '%s/%s.rx' % (TOOLS_DIR, tag)
        ready = '%s/%s.ready' % (TOOLS_DIR, tag)
        for p in (out, ready):
            if os.path.exists(p):
                os.remove(p)
        self.run_bg(self.stas[name], 'listen --port %d --out %s --ready %s --loss %s --delay-ms %s --seed %d '
                    '--duration %s' % (port, out, ready, loss, delay_ms, seed, duration), tag)
        return out, ready

    def wait_ready(self, readies, timeout=15.0):
        t = mono() + timeout
        while mono() < t:
            if all(os.path.exists(r) for r in readies):
                return True
            time.sleep(0.1)
        return False

    def send(self, name, port, count, interval, size=333, dest=BCAST, log=None, background=False):
        args = 'send --name %s --count %d --interval %s --size %d --dest %s --port %d' % (
            name, count, interval, size, dest, port)
        if log:
            args += ' --log %s' % log
            if os.path.exists(log):
                os.remove(log)
        if background:
            self.run_bg(self.stas[name], args, 'send_' + name)
        else:
            self.stas[name].cmd('python3 -u %s %s' % (LB, args))

    def kill_tools(self):
        for s in self.stas.values():
            s.cmd('pkill -f %s 2>/dev/null' % LB)
            break                                           # one pkill reaches every process (shared PID space)

    def stop(self):
        if self.net is None:
            return
        t = mono()
        try:
            self.kill_tools()
            time.sleep(0.3)
            self.net.stop()
        except Exception:
            force_cleanup()
        self.stop_s = mono() - t
        self.net = None

    def __exit__(self, *exc):
        self.stop()
        return False


def effective_range(curve):
    """Largest distance with fraction >= 0.90 such that all nearer points are >= 0.90 too (r90),
    smallest distance with fraction <= 0.10 (r10). curve: list of dicts with distance_m, fraction."""
    r90 = None
    for row in curve:
        if row['fraction'] >= 0.90:
            r90 = row['distance_m']
        else:
            break
    r10 = None
    for row in curve:
        if row['fraction'] <= 0.10:
            r10 = row['distance_m']
            break
    return r90, r10


# ---------------------------------------------------------------- tests

def test_env(a):
    info('\n===== env: environment inside a station =====\n')
    host = {'python': platform.python_version(), 'platform': platform.platform(), 'cpus': os.cpu_count()}
    results = {}
    with Lab(a) as lab:
        lab.add('e1', 0, 0)
        lab.add('e2', 10, 0)
        lab.start()
        for name in ('e1', 'e2'):
            before = mono()
            raw = lab.stas[name].cmd('python3 %s env' % LB)
            after = mono()
            try:
                line = [l for l in raw.splitlines() if l.startswith('{')][-1]
                d = json.loads(line)
            except (IndexError, ValueError):
                results[name] = {'error': raw[-300:]}
                continue
            d['clock_within_host_bracket'] = before <= d.get('monotonic', -1) <= after
            results[name] = d
    ok = True
    for name, d in results.items():
        if 'error' in d:
            ok = False
            continue
        major, minor = [int(x) for x in d['python'].split('.')[:2]]
        d['python_ok'] = (major, minor) >= (3, 11)
        d['bind_ok'] = all(v is True for v in d['bind'].values())
        d['crypto_ok'] = bool(d.get('ed25519_ok'))
        ok = ok and d['python_ok'] and d['bind_ok'] and d['crypto_ok'] and d['tomllib'] \
            and d['clock_within_host_bracket']
    save(a, 'env', {'verdict': 'PASS' if ok else 'FAIL', 'host': host, 'stations': results,
                    'headline': {'python_in_station': next(iter(results.values())).get('python'),
                                 'all_checks': ok}})
    return ok


def test_range(a):
    info('\n===== range: handheld BROADCAST range curve (range=%d, exp=%s) =====\n' % (HAND_RANGE, a.exp))
    frames = 200 if a.quick else 1000
    dists = list(range(50, a.r1_max + 1, 10))
    with Lab(a) as lab:
        lab.add('tx', 0, 0)
        for d in dists:
            lab.add('r%03d' % d, d, 0)
        lab.start()
        txi = lab.stas['tx'].wintfs[0]
        radio = {'txpower_dbm': getattr(txi, 'txpower', None), 'nominal_range_m': getattr(txi, 'range', None)}
        outs, readies = {}, []
        for d in dists:
            out, rdy = lab.listen('r%03d' % d, 5100, 'range_r%03d' % d)
            outs[d] = out
            readies.append(rdy)
        if not lab.wait_ready(readies):
            raise RuntimeError('listeners did not start')
        lab.send('tx', 5100, frames, 0.005)
        time.sleep(1.0)
        lab.kill_tools()
        time.sleep(0.5)
    curve = []
    for d in dists:
        got = len(seqs_from(read_rx(outs[d]), 'tx'))
        curve.append({'distance_m': d, 'received': got, 'fraction': got / float(frames)})
    r90, r10 = effective_range(curve)
    reached_max = r90 == dists[-1]
    width = (r10 - r90) if (r10 is not None and r90 is not None) else None
    save(a, 'range', {'verdict': 'INFO', 'frames': frames, 'curve': curve, 'r90_m': r90, 'r10_m': r10,
                      'edge_width_m': width, 'edge_beyond_max': reached_max, 'radio': radio,
                      'headline': {'handheld_r90_m': r90, 'edge_width_m': width, 'radio': radio}})
    info('  r90=%s m, r10=%s m, edge width=%s m%s\n' % (r90, r10, width,
         '  (edge beyond max distance: rerun with a larger --r1-max)' if reached_max else ''))
    return True


def _relay_one(a, d, frames):
    """One relay distance, in its own process. Returns (row, radio)."""
    res, radio = {}, {}
    with Lab(a) as lab:
        h = lab.add('hh', 0, 0, HAND_RANGE)
        r = lab.add('rr', d, 0, RELAY_RANGE)
        lab.start()
        radio = {'hand_txpower_dbm': getattr(h.wintfs[0], 'txpower', None),
                 'relay_txpower_dbm': getattr(r.wintfs[0], 'txpower', None)}
        for tx, rx in (('hh', 'rr'), ('rr', 'hh')):
            out, rdy = lab.listen(rx, 5300, 'relay_%d_%s' % (d, rx))
            lab.wait_ready([rdy])
            lab.send(tx, 5300, frames, 0.005)
            time.sleep(0.7)
            lab.kill_tools()
            time.sleep(0.3)
            res['%s_to_%s' % ('handheld' if tx == 'hh' else 'relay', 'relay' if tx == 'hh' else 'handheld')] = \
                len(seqs_from(read_rx(out), tx)) / float(frames)
    res['distance_m'] = d
    return res, radio


def test_relay(a):
    info('\n===== relay: relay-class (range=%d) vs handheld (range=%d), both directions =====\n' % (
        RELAY_RANGE, HAND_RANGE))
    frames = 150 if a.quick else 500
    rows = []
    radio = {}
    for d in a.relay_dists:
        res, rd = isolated(_relay_one, a, d, frames)
        radio = radio or rd
        rows.append(res)
        info('  %4d m: handheld->relay %.2f   relay->handheld %.2f\n' % (
            d, res['handheld_to_relay'], res['relay_to_handheld']))
    asym = [r['distance_m'] for r in rows
            if (r['handheld_to_relay'] >= 0.9 and r['relay_to_handheld'] <= 0.1)
            or (r['relay_to_handheld'] >= 0.9 and r['handheld_to_relay'] <= 0.1)]

    def r90_of(key):
        best = None
        for r in rows:
            if r[key] >= 0.9:
                best = r['distance_m']
            else:
                break
        return best
    h2r, r2h = r90_of('handheld_to_relay'), r90_of('relay_to_handheld')
    save(a, 'relay', {'verdict': 'INFO', 'frames': frames, 'rows': rows, 'asymmetric_at_m': asym,
                      'symmetric': not asym, 'r90_handheld_to_relay_m': h2r, 'r90_relay_to_handheld_m': r2h,
                      'radio': radio,
                      'headline': {'relay_to_handheld_r90_m': r2h, 'handheld_to_relay_r90_m': h2r,
                                   'symmetric': not asym, 'radio': radio}})
    info('  symmetric: %s (r90 handheld->relay %s m, relay->handheld %s m)\n' % (not asym, h2r, r2h))
    return True


def joint_loss_ratio(lost_sets, frames):
    """Mean over receiver pairs of P(both lost) / (P(i lost) * P(j lost)).
    About 1 when losses are independent; about 1/p when the same frames are lost for everyone."""
    ratios = []
    names = list(lost_sets)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            li, lj = lost_sets[names[i]], lost_sets[names[j]]
            exp = (len(li) / float(frames)) * (len(lj) / float(frames))
            if exp > 0:
                ratios.append((len(li & lj) / float(frames)) / exp)
    return statistics.mean(ratios) if ratios else None


def analyze_inject(per_node_rx, senders, levels_frames, level, delay_ms):
    """per_node_rx: {receiver: rows}. Returns dict of loss/independence/delay stats for one level."""
    frames = levels_frames
    per_pair, ratios = {}, []
    for snd in senders:
        lost = {}
        for rcv in senders:
            if rcv == snd:
                continue
            got = seqs_from(per_node_rx[rcv], snd)
            lost[rcv] = set(range(frames)) - got
            per_pair['%s->%s' % (snd, rcv)] = 100.0 * len(lost[rcv]) / frames
        r = joint_loss_ratio(lost, frames) if level > 0 else None
        if r is not None:
            ratios.append(r)
    vals = list(per_pair.values())
    delays = []
    for rcv, rows in per_node_rx.items():
        for src, seq, sts, rts, dts, ln in rows:
            if src != rcv:
                delays.append((dts - sts) * 1000.0)
    return {'configured_pct': level, 'mean_pct': statistics.mean(vals), 'min_pct': min(vals), 'max_pct': max(vals),
            'mean_error_pp': statistics.mean(vals) - level, 'per_pair': per_pair,
            'joint_loss_ratio': statistics.mean(ratios) if ratios else None,
            'delay_ms_median': percentile(delays, 0.5), 'delay_ms_p95': percentile(delays, 0.95),
            'configured_delay_ms': delay_ms}


def test_inject(a):
    info('\n===== inject: receiver-side loss + delay shim =====\n')
    frames = 500 if a.quick else 2000
    names = ['s1', 's2', 's3', 's4', 's5']
    levels, probe = [], {}
    with Lab(a) as lab:
        for i, n in enumerate(names):
            lab.add(n, 5 * i, 0)
        lab.start()
        for level in (0, 5, 15):
            info('  --- configured loss %d%%, delay %s ms ---\n' % (level, a.delay_ms))
            outs, readies = {}, []
            for n in names:
                out, rdy = lab.listen(n, 5400, 'inject_%s_L%d' % (n, level), loss=level, delay_ms=a.delay_ms,
                                      seed=zlib.crc32(('%s:%d' % (n, level)).encode()))
                outs[n] = out
                readies.append(rdy)
            lab.wait_ready(readies)
            for n in names:
                lab.send(n, 5400, frames, 0.003)
            time.sleep(1.0)
            lab.kill_tools()
            time.sleep(0.5)
            levels.append(analyze_inject({n: read_rx(outs[n]) for n in names}, names, frames, level, a.delay_ms))
        # Probe: does tc netem delay break broadcast on the wlan interface?
        info('  --- netem delay probe (does tc break broadcast?) ---\n')
        for n in names:
            lab.stas[n].cmd('tc qdisc replace dev %s-wlan0 root netem delay %dms 2>&1' % (n, int(a.delay_ms)))
        outs, readies = {}, []
        for n in names[1:]:
            out, rdy = lab.listen(n, 5500, 'inject_netem_%s' % n)
            outs[n] = out
            readies.append(rdy)
        lab.wait_ready(readies)
        lab.send('s1', 5500, 200, 0.005)
        time.sleep(1.0)
        lab.kill_tools()
        fr = [len(seqs_from(read_rx(outs[n]), 's1')) / 200.0 for n in names[1:]]
        dl = [(r[4] - r[2]) * 1000.0 for n in names[1:] for r in read_rx(outs[n])]
        probe = {'received_fraction_per_receiver': fr, 'delay_ms_median': percentile(dl, 0.5),
                 'netem_delay_ok': min(fr) >= 0.9 and (percentile(dl, 0.5) or 0) >= a.delay_ms * 0.8}
        for n in names:
            lab.stas[n].cmd('tc qdisc del dev %s-wlan0 root 2>/dev/null' % n)
    loss_ok = all(abs(l['mean_error_pp']) <= 1.5 for l in levels)
    indep = [l['joint_loss_ratio'] for l in levels if l['joint_loss_ratio'] is not None]
    indep_ok = bool(indep) and all(0.6 <= r <= 1.4 for r in indep)
    d0 = levels[0]
    delay_ok = d0['delay_ms_median'] is not None and abs(d0['delay_ms_median'] - a.delay_ms) <= 5.0
    ok = loss_ok and indep_ok and delay_ok
    save(a, 'inject', {'verdict': 'PASS' if ok else 'FAIL', 'frames': frames, 'levels': levels,
                       'netem_delay_probe': probe, 'loss_ok': loss_ok, 'independent_ok': indep_ok,
                       'delay_ok': delay_ok,
                       'headline': {'loss_error_pp': [round(l['mean_error_pp'], 2) for l in levels],
                                    'joint_loss_ratio': [None if l['joint_loss_ratio'] is None
                                                         else round(l['joint_loss_ratio'], 2) for l in levels],
                                    'delay_ms_median_at_0pct': d0['delay_ms_median'],
                                    'netem_delay_ok': probe.get('netem_delay_ok')}})
    info('  loss_ok=%s independent_ok=%s delay_ok=%s netem_delay_ok=%s\n' % (
        loss_ok, indep_ok, delay_ok, probe.get('netem_delay_ok')))
    return ok


def test_mobility(a):
    info('\n===== mobility: a moving station (we call setPosition on a schedule) =====\n')
    start_m, speed, step = 50.0, 2.0, 0.5
    duration = 20.0 if a.quick else 100.0
    interval = 0.1
    count = int(duration / interval) + 20
    moves, lags = [], []
    with Lab(a) as lab:
        tx = lab.add('tx', 0, 0)
        mover = lab.add('mv', start_m, 0)
        lab.start()
        out, rdy = lab.listen('mv', 5600, 'mobility_mv')
        lab.wait_ready([rdy])
        sent_log = '%s/mobility_sent.log' % TOOLS_DIR
        t_start = mono()
        lab.send('tx', 5600, count, interval, log=sent_log, background=True)
        k = 0
        while True:
            t = mono() - t_start
            if t > duration:
                break
            x = start_m + speed * t
            t_call = mono()
            mover.setPosition('%.2f,0,0' % x)
            lags.append(mono() - t_call)
            moves.append((t_call, x))
            k += 1
            wait = t_start + k * step - mono()
            if wait > 0:
                time.sleep(wait)
        time.sleep(1.5)
        lab.kill_tools()
        time.sleep(0.5)
    sent = read_sent(sent_log)
    got = seqs_from(read_rx(out), 'tx')

    def pos_at(ts):
        x = start_m
        for t_call, px in moves:
            if t_call <= ts:
                x = px
            else:
                break
        return x
    bins = defaultdict(lambda: [0, 0])
    for seq, ts in sent:
        if ts > t_start + duration:
            continue
        b = int(round(pos_at(ts) / 10.0) * 10)
        bins[b][0] += 1
        bins[b][1] += 1 if seq in got else 0
    curve = [{'distance_m': b, 'sent': bins[b][0], 'received': bins[b][1],
              'fraction': bins[b][1] / float(bins[b][0])} for b in sorted(bins) if bins[b][0] >= 5]
    r90, r10 = effective_range(curve)
    near = [c['fraction'] for c in curve if c['distance_m'] <= 80]
    far = [c['fraction'] for c in curve if c['distance_m'] >= 240]
    effective = bool(near) and bool(far) and min(near) >= 0.8 and max(far) <= 0.3
    ref = load(a, 'range')
    max_diff = None
    if ref:
        ref_curve = {c['distance_m']: c['fraction'] for c in ref['curve']}
        diffs = [abs(c['fraction'] - ref_curve[c['distance_m']]) for c in curve if c['distance_m'] in ref_curve]
        max_diff = max(diffs) if diffs else None
    follows = effective and (max_diff is None or max_diff <= 0.25)
    save(a, 'mobility', {'verdict': 'PASS' if follows else 'FAIL', 'speed_mps': speed, 'start_m': start_m,
                         'duration_s': duration, 'step_s': step, 'curve': curve, 'r90_m': r90,
                         'movement_changes_reception': effective, 'max_diff_vs_range_curve': max_diff,
                         'setposition_lag_s': {'mean': statistics.mean(lags), 'max': max(lags)},
                         'headline': {'r90_while_moving_m': r90, 'movement_changes_reception': effective,
                                      'max_diff_vs_range_curve': max_diff,
                                      'setposition_lag_max_s': round(max(lags), 3)}})
    info('  movement changes reception: %s; r90 while moving: %s m; max diff vs range curve: %s\n' % (
        effective, r90, max_diff))
    return follows


def _repeat_one(a, rep, dists, frames):
    """One identical run, in its own process. Returns {distance: received fraction}."""
    with Lab(a) as lab:
        lab.add('tx', 0, 0)
        for d in dists:
            lab.add('r%03d' % d, d, 0)
        lab.start()
        outs, readies = {}, []
        for d in dists:
            out, rdy = lab.listen('r%03d' % d, 5700, 'repeat%d_r%03d' % (rep, d))
            outs[d] = out
            readies.append(rdy)
        lab.wait_ready(readies)
        lab.send('tx', 5700, frames, 0.005)
        time.sleep(1.0)
        lab.kill_tools()
        time.sleep(0.3)
    return {d: len(seqs_from(read_rx(outs[d]), 'tx')) / float(frames) for d in dists}


def test_repeat(a):
    info('\n===== repeat: emulator noise floor at the range edge (3 identical runs) =====\n')
    frames = 200 if a.quick else 1000
    ref = load(a, 'range')
    r90 = ref['r90_m'] if ref and ref.get('r90_m') else 90
    dists = [20, max(30, r90 - 10), r90, r90 + 10, r90 + 20]
    runs = [isolated(_repeat_one, a, rep, dists, frames) for rep in range(3)]
    spread = {d: max(r[d] for r in runs) - min(r[d] for r in runs) for d in dists}
    noise = statistics.mean(spread.values())
    save(a, 'repeat', {'verdict': 'INFO', 'frames': frames, 'distances_m': dists, 'runs': runs,
                       'spread_by_distance': spread, 'noise_floor': noise, 'max_spread': max(spread.values()),
                       'headline': {'mean_spread': round(noise, 3), 'max_spread': round(max(spread.values()), 3)}})
    info('  mean spread %.3f, max spread %.3f (fraction of frames)\n' % (noise, max(spread.values())))
    return True


def _scale_one(a, n, duration):
    """One network size, in its own process. Returns the result row."""
    names = ['n%02d' % i for i in range(1, n + 1)]
    row = {'nodes': n}
    with Lab(a) as lab:
        for i, name in enumerate(names):
            lab.add(name, 5 * (i % 6), 5 * (i // 6))
        lab.start()
        row['build_s'] = lab.build_s
        outs, readies = {}, []
        for i, name in enumerate(names):
            out = '%s/scale_%s.json' % (TOOLS_DIR, name)
            rdy = '%s/scale_%s.ready' % (TOOLS_DIR, name)
            for p in (out, rdy):
                if os.path.exists(p):
                    os.remove(p)
            extra = ' --origin --burst 10 --burst-at %.1f' % (duration / 3.0) if i == 0 else ''
            lab.run_bg(lab.stas[name], 'flood --name %s --port 5800 --dest %s --out %s --ready %s '
                       '--duration %s%s' % (name, BCAST, out, rdy, duration, extra), 'scale_%s' % name)
            outs[name] = out
            readies.append(rdy)
        lab.wait_ready(readies, timeout=30.0)
        idle0, tot0 = cpu_times()
        mem0 = mem_available_mb()
        time.sleep(duration + 3.0)
        idle1, tot1 = cpu_times()
        mem1 = mem_available_mb()
        row['cpu_busy_pct'] = 100.0 * (1.0 - (idle1 - idle0) / float(max(1, tot1 - tot0)))
        row['loadavg'] = os.getloadavg()[0]
        row['mem_used_mb'] = (mem0 - mem1) if (mem0 is not None and mem1 is not None) else None
    row['stop_s'] = lab.stop_s
    summ = {}
    for name in names:
        try:
            with open(outs[name]) as f:
                summ[name] = json.load(f)
        except (OSError, ValueError):
            summ[name] = None
    missing = [k for k, v in summ.items() if v is None]
    good = {k: v for k, v in summ.items() if v is not None}
    total_hello_tx = sum(v['hello_tx'] for v in good.values())
    ratios = []
    for k, v in good.items():
        expected = total_hello_tx - v['hello_tx']
        if expected > 0:
            ratios.append(v['hello_rx'] / float(expected))
    origin = summ[names[0]]
    reach, lat = [], []
    if origin:
        for key, t_send in origin['origin_tx'].items():
            got = [v['first_rx'][key] for k, v in good.items() if k != names[0] and key in v['first_rx']]
            reach.append(len(got) / float(n - 1))
            lat.extend((t - t_send) * 1000.0 for t in got)
        row['data_tx_per_message'] = sum(v['data_tx'] for v in good.values()) / float(max(1, len(origin['origin_tx'])))
    row.update({'missing_summaries': missing,
                'hello_delivery_ratio': statistics.mean(ratios) if ratios else None,
                'flood_reach_mean': statistics.mean(reach) if reach else None,
                'flood_latency_ms_median': percentile(lat, 0.5), 'flood_latency_ms_p95': percentile(lat, 0.95)})
    row['pass'] = (not missing and row['hello_delivery_ratio'] is not None and row['hello_delivery_ratio'] >= 0.98
                   and row['flood_reach_mean'] is not None and row['flood_reach_mean'] >= 0.98
                   and row['cpu_busy_pct'] <= 80.0)
    return row


def test_scale(a):
    info('\n===== scale: %s stations, close range, HELLO traffic plus a flood burst =====\n' % a.scale_nodes)
    duration = 20.0 if a.quick else 30.0
    results = []
    for n in a.scale_nodes:
        info('  --- %d stations ---\n' % n)
        row = isolated(_scale_one, a, n, duration)
        results.append(row)
        info('  %d nodes: hello delivery %s, flood reach %s, cpu %.0f%%, build %.0fs -> %s\n' % (
            n, row['hello_delivery_ratio'], row['flood_reach_mean'], row['cpu_busy_pct'],
            row['build_s'] or 0, 'PASS' if row['pass'] else 'FAIL'))
        if not row['pass'] and n != a.scale_nodes[-1]:
            info('  (stopping the scale-up: this size already fails)\n')
            break
    ok_sizes = [r['nodes'] for r in results if r['pass']]
    save(a, 'scale', {'verdict': 'PASS' if (results and results[-1]['pass']) else 'FAIL', 'rows': results,
                      'max_nodes_ok': max(ok_sizes) if ok_sizes else 0,
                      'headline': {'max_nodes_ok': max(ok_sizes) if ok_sizes else 0,
                                   'cpus': os.cpu_count(),
                                   'by_size': {r['nodes']: {'cpu_pct': round(r['cpu_busy_pct'], 0),
                                                            'hello': r['hello_delivery_ratio'],
                                                            'reach': r['flood_reach_mean'],
                                                            'build_s': r['build_s'], 'stop_s': r['stop_s']}
                                               for r in results}}})
    return bool(results and results[-1]['pass'])


def _lifecycle_one(a, c):
    """One start/stop cycle, in its own process. Returns the cycle row."""
    with Lab(a) as lab:
        for i in range(10):
            lab.add('c%02d' % i, 5 * i, 0)
        lab.start()
    return {'cycle': c + 1, 'build_s': lab.build_s, 'stop_s': lab.stop_s, 'after': host_snapshot()}


def test_lifecycle(a):
    info('\n===== lifecycle: start/stop cycles =====\n')
    cycles = 3 if a.quick else 5
    base = host_snapshot()
    rows = []
    for c in range(cycles):
        row = isolated(_lifecycle_one, a, c)
        rows.append(row)
        info('  cycle %d: build %.1fs, stop %.1fs, %s\n' % (c + 1, row['build_s'], row['stop_s'], row['after']))
    last = rows[-1]['after']
    leaks = {k: (last[k] - base[k]) if (last[k] is not None and base[k] is not None) else None for k in base}
    leaked = any(v not in (0, None) for v in leaks.values())
    save(a, 'lifecycle', {'verdict': 'FAIL' if leaked else 'PASS', 'baseline': base, 'cycles': rows,
                          'leaks_after_last_cycle': leaks,
                          'headline': {'leaks': leaks,
                                       'build_s_mean': statistics.mean(r['build_s'] for r in rows),
                                       'stop_s_mean': statistics.mean(r['stop_s'] for r in rows)}})
    return not leaked


# ---------------------------------------------------------------- connectivity re-check from MEASURED ranges

def connectivity(a):
    ref = load(a, 'range')
    if not ref or not ref.get('r90_m'):
        info('\n(connectivity re-check skipped: run the `range` test first)\n')
        return
    rh = float(ref['r90_m'])
    rel = load(a, 'relay')
    rr = float(rel['r90_relay_to_handheld_m']) if rel and rel.get('r90_relay_to_handheld_m') else float(RELAY_RANGE)
    sym = bool(rel and rel.get('symmetric', True))
    # A link needs both ends to hear each other: handheld-handheld uses rh, relay-relay uses the relay range,
    # handheld-relay is limited by the handheld's short range.
    limit = {('h', 'h'): rh, ('r', 'r'): rr, ('h', 'r'): rh, ('r', 'h'): rh}

    def place_uniform(w, h, nh, nr, rng):
        pts = [(rng.uniform(0, w), rng.uniform(0, h), 'h') for _ in range(nh)]
        pts += [(rng.uniform(0, w), rng.uniform(0, h), 'r') for _ in range(nr)]
        return pts

    def place_clusters(rng):
        pts = []
        for cx in (150, 600, 1050):
            for _ in range(8):
                pts.append((rng.uniform(cx - 100, cx + 100), rng.uniform(0, 200), 'h'))
        pts += [(rng.uniform(0, 1200), rng.uniform(0, 200), 'r') for _ in range(6)]
        return pts

    scenarios = {
        'proposal 1200x1200 m, 24 handhelds + 6 relays': lambda r: place_uniform(1200, 1200, 24, 6, r),
        'corridor 1200x150 m, 26 handhelds + 4 relays': lambda r: place_uniform(1200, 150, 26, 4, r),
        'clustered 3x8 handhelds (200x200 m) + 6 relays': place_clusters,
    }
    trials = 300 if a.quick else 2000
    out = {}
    for label, make in scenarios.items():
        rng = random.Random(1)
        deg_s, comp_s, far_c, far_n = [], [], 0, 0
        for _ in range(trials):
            pts = make(rng)
            n = len(pts)
            parent = list(range(n))

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x
            deg = [0] * n
            for i in range(n):
                for j in range(i + 1, n):
                    d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
                    if d <= limit[(pts[i][2], pts[j][2])]:
                        deg[i] += 1
                        deg[j] += 1
                        parent[find(i)] = find(j)
            comps = defaultdict(int)
            for i in range(n):
                comps[find(i)] += 1
            comp_s.append(max(comps.values()) / float(n))
            hs = [i for i in range(n) if pts[i][2] == 'h']
            deg_s.append(statistics.mean(deg[i] for i in hs))
            for x in range(len(hs)):
                for y in range(x + 1, len(hs)):
                    i, j = hs[x], hs[y]
                    if math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) >= 600:
                        far_n += 1
                        far_c += 1 if find(i) == find(j) else 0
        out[label] = {'handheld_mean_degree': statistics.mean(deg_s), 'largest_component': statistics.mean(comp_s),
                      'far_pairs_connected': far_c / float(max(1, far_n))}
    with open(os.path.join(a.results, 'connectivity.md'), 'w') as f:
        f.write('# Connectivity re-check from measured ranges\n\n')
        f.write('Measured handheld r90 = %.0f m, relay r90 = %.0f m, links symmetric: %s. Ideal disc model, '
                'uniform placement, static snapshots, no loss; use as an order-of-magnitude guide.\n\n' % (rh, rr, sym))
        f.write('| Scenario | Handheld mean degree | Nodes in largest component | Far pairs (600 m or more) connected |\n')
        f.write('|---|---|---|---|\n')
        for label, v in out.items():
            f.write('| %s | %.2f | %.0f%% | %.1f%% |\n' % (
                label, v['handheld_mean_degree'], 100 * v['largest_component'], 100 * v['far_pairs_connected']))
    save(a, 'connectivity', {'verdict': 'INFO', 'handheld_r90_m': rh, 'relay_r90_m': rr, 'scenarios': out,
                             'headline': out})
    info('\n  connectivity.md written\n')


# ---------------------------------------------------------------- summary

def write_summary(a):
    names = ['env', 'range', 'relay', 'inject', 'mobility', 'repeat', 'scale', 'lifecycle']
    lines = ['# WP0 spike summary', '', 'Path-loss exponent used: %s' % a.exp, '',
             '| Test | Verdict | Headline |', '|---|---|---|']
    for n in names:
        d = load(a, n)
        if d is None:
            lines.append('| %s | not run | |' % n)
        else:
            lines.append('| %s | %s | %s |' % (n, d.get('verdict', '?'),
                                               json.dumps(d.get('headline', d.get('error', '')), default=str)[:300]))
    rng, rel, rep = load(a, 'range'), load(a, 'relay'), load(a, 'repeat')
    sca, lif, inj, mob = load(a, 'scale'), load(a, 'lifecycle'), load(a, 'inject'), load(a, 'mobility')
    lines += ['', '## Emulator parameters (feed these into C4 and the scenario generator)', '',
              '| Parameter | Value |', '|---|---|',
              '| Path-loss exponent | %s |' % a.exp,
              '| Handheld effective broadcast range (r90) | %s m (edge width %s m) |' % (
                  rng and rng.get('r90_m'), rng and rng.get('edge_width_m')),
              '| Relay-class range, relay to handheld (r90) | %s m |' % (rel and rel.get('r90_relay_to_handheld_m')),
              '| Handheld to relay (r90) | %s m |' % (rel and rel.get('r90_handheld_to_relay_m')),
              '| Links symmetric | %s |' % (rel and rel.get('symmetric')),
              '| Mobility changes reception | %s |' % (mob and mob.get('movement_changes_reception')),
              '| Loss mechanism | receiver-side shim (independent per receiver: %s) |' % (inj and inj.get('independent_ok')),
              '| Delay mechanism | shim (netem delay OK on broadcast: %s) |' % (
                  inj and inj.get('netem_delay_probe', {}).get('netem_delay_ok')),
              '| Maximum node count that passes | %s |' % (sca and sca.get('max_nodes_ok')),
              '| Run overhead (build / stop, 10 nodes) | %s |' % (lif and lif.get('headline', {}).get('build_s_mean')),
              '| Emulator noise floor (mean spread across identical runs) | %s |' % (rep and rep.get('noise_floor'))]
    path = os.path.join(a.results, 'summary.md')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    info('\n  saved %s\n' % path)


# ---------------------------------------------------------------- selftest (no Mininet needed)

def selftest(a):
    """Exercises the helper program, shim, and analysis on the loopback interface."""
    info('selftest: checking helper program and analysis on loopback\n')
    write_tools()
    port, dest, frames, names = 5990, '127.255.255.255', 2000, ['a', 'b', 'c', 'd']
    ok_all = True
    # env
    out = subprocess.run([sys.executable, LB, 'env'], capture_output=True, text=True).stdout
    env = json.loads(out.strip().splitlines()[-1])
    info('  env ok: python %s, cryptography %s\n' % (env['python'], env.get('cryptography', env.get('cryptography_error'))))
    # shim: 4 listeners, one sender broadcasting
    procs = []
    for n in names:
        o = '%s/self_%s.rx' % (TOOLS_DIR, n)
        r = '%s/self_%s.ready' % (TOOLS_DIR, n)
        for p in (o, r):
            if os.path.exists(p):
                os.remove(p)
        procs.append(subprocess.Popen([sys.executable, LB, 'listen', '--port', str(port), '--out', o, '--ready', r,
                                       '--loss', '15', '--delay-ms', '20',
                                       '--seed', str(zlib.crc32(('%s:15' % n).encode())), '--duration', '9']))
    t = mono() + 5
    while mono() < t and not all(os.path.exists('%s/self_%s.ready' % (TOOLS_DIR, n)) for n in names):
        time.sleep(0.05)
    subprocess.run([sys.executable, LB, 'send', '--name', 'tx', '--count', str(frames), '--interval', '0.002',
                    '--size', '333', '--dest', dest, '--port', str(port)])
    for p in procs:
        p.wait()
    rx = {n: read_rx('%s/self_%s.rx' % (TOOLS_DIR, n)) for n in names}
    lost = {n: set(range(frames)) - seqs_from(rx[n], 'tx') for n in names}
    mean_loss = statistics.mean(100.0 * len(lost[n]) / frames for n in names)
    ratio = joint_loss_ratio(lost, frames)
    delays = [(r[4] - r[2]) * 1000.0 for n in names for r in rx[n]]
    info('  shim: mean loss %.2f%% (configured 15), joint-loss ratio %.2f (about 1 means independent), '
         'delay median %.1f ms (configured 20)\n' % (mean_loss, ratio, percentile(delays, 0.5)))
    ok_all &= abs(mean_loss - 15) <= 2 and 0.6 <= ratio <= 1.4 and abs(percentile(delays, 0.5) - 20) <= 5
    # stable seeds: same seed gives the same drops
    o2 = '%s/self_again.rx' % TOOLS_DIR
    p2 = subprocess.Popen([sys.executable, LB, 'listen', '--port', str(port), '--out', o2, '--ready', o2 + '.rdy',
                           '--loss', '15', '--seed', str(zlib.crc32(b'a:15')), '--duration', '5'])
    time.sleep(0.8)
    subprocess.run([sys.executable, LB, 'send', '--name', 'tx', '--count', str(frames), '--interval', '0.002',
                    '--size', '333', '--dest', dest, '--port', str(port)])
    p2.wait()
    again = seqs_from(read_rx(o2), 'tx')
    first = seqs_from(rx['a'], 'tx')
    same = len(again ^ first) <= 0.02 * frames
    info('  same seed gives the same drops (up to timing): %s\n' % same)
    ok_all &= same
    # flood mini-protocol: 4 nodes on loopback
    fn = ['f1', 'f2', 'f3', 'f4']
    fps = []
    for i, n in enumerate(fn):
        o = '%s/self_flood_%s.json' % (TOOLS_DIR, n)
        if os.path.exists(o):
            os.remove(o)
        cmd = [sys.executable, LB, 'flood', '--name', n, '--port', str(port + 1), '--dest', dest, '--out', o,
               '--duration', '8', '--burst-at', '3']
        if i == 0:
            cmd.append('--origin')
        fps.append(subprocess.Popen(cmd))
    for p in fps:
        p.wait()
    summ = {n: json.load(open('%s/self_flood_%s.json' % (TOOLS_DIR, n))) for n in fn}
    reach = []
    for key in summ['f1']['origin_tx']:
        reach.append(sum(1 for n in fn[1:] if key in summ[n]['first_rx']) / 3.0)
    hello_ok = all(summ[n]['hello_rx'] > 0 for n in fn)
    info('  flood: reach %.2f, hello received by all: %s\n' % (statistics.mean(reach), hello_ok))
    ok_all &= statistics.mean(reach) >= 0.99 and hello_ok
    # effective_range and joint ratio on synthetic data
    curve = [{'distance_m': d, 'fraction': f} for d, f in
             [(50, 1.0), (60, 1.0), (70, 0.99), (80, 0.95), (90, 0.8), (100, 0.4), (110, 0.05)]]
    r90, r10 = effective_range(curve)
    info('  effective_range on synthetic curve: r90=%s (expect 80), r10=%s (expect 110)\n' % (r90, r10))
    ok_all &= r90 == 80 and r10 == 110
    same_lost = {n: set(random.Random(7).sample(range(frames), 300)) for n in names}
    r_same = joint_loss_ratio(same_lost, frames)
    info('  joint-loss ratio for IDENTICAL loss patterns: %.1f (expect about 1/p = %.1f)\n' % (r_same, frames / 300.0))
    ok_all &= r_same > 3
    info('\nSELFTEST %s\n' % ('PASS' if ok_all else 'FAIL'))
    return ok_all


# ---------------------------------------------------------------- main

SINGLE_NET = ('env', 'range', 'inject', 'mobility')
TESTS = {'env': test_env, 'range': test_range, 'relay': test_relay, 'inject': test_inject,
         'mobility': test_mobility, 'repeat': test_repeat, 'scale': test_scale, 'lifecycle': test_lifecycle}


def parse_args():
    p = argparse.ArgumentParser(description='WP0 spike for Lifeline (see the module docstring).')
    p.add_argument('--only', default='', help='comma-separated subset of: ' + ','.join(DEFAULT_ORDER))
    p.add_argument('--exp', type=float, default=4.0, help='logDistance path-loss exponent (record it!)')
    p.add_argument('--quick', action='store_true', help='fewer frames / shorter runs for a dry run')
    p.add_argument('--delay-ms', dest='delay_ms', type=float, default=20.0)
    p.add_argument('--r1-max', dest='r1_max', type=int, default=200, help='largest distance in the range sweep (m)')
    p.add_argument('--relay-dists', dest='relay_dists', default='100,200,300,400,500,600')
    p.add_argument('--scale-nodes', dest='scale_nodes', default='5,15,30')
    p.add_argument('--results', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'results'))
    p.add_argument('--hand-tx', dest='hand_tx', type=float, default=None,
                   help='handheld TX power in dBm; if set, range= is not used (fallback for the wmIface crash)')
    p.add_argument('--relay-tx', dest='relay_tx', type=float, default=None,
                   help='relay TX power in dBm; if set, range= is not used')
    p.add_argument('--selftest', action='store_true', help='check helper code on loopback; no Mininet needed')
    a = p.parse_args()
    a.relay_dists = [int(x) for x in a.relay_dists.split(',')]
    a.scale_nodes = [int(x) for x in a.scale_nodes.split(',')]
    return a


def main():
    a = parse_args()
    if a.selftest:
        sys.exit(0 if selftest(a) else 1)
    if os.geteuid() != 0:
        sys.exit('Run with sudo: Mininet-WiFi needs root.')
    if not HAVE_MN:
        sys.exit('Could not import Mininet-WiFi. Run this on the emulation host.')
    setLogLevel('info')
    os.makedirs(a.results, exist_ok=True)
    write_tools()
    wanted = [t for t in (a.only.split(',') if a.only else DEFAULT_ORDER) if t]
    unknown = [t for t in wanted if t not in TESTS]
    if unknown:
        sys.exit('unknown test(s): %s' % ', '.join(unknown))
    for name in wanted:
        try:
            if name in SINGLE_NET:
                isolated(TESTS[name], a)         # one network: own process
            else:
                TESTS[name](a)                   # builds several networks, each isolated inside
        except Exception:
            tb = traceback.format_exc()
            info('\n  !!! %s failed:\n%s\n' % (name, tb))
            save(a, name, {'verdict': 'ERROR', 'error': tb[-1500:]})
            force_cleanup()
    connectivity(a)
    write_summary(a)


if __name__ == '__main__':
    main()