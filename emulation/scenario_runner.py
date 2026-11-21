"""Scenario runner (WP9): one command runs one seeded scenario and writes the run directory.

    sudo python3 -m emulation.run emulation/scenarios/proposal_loss05.toml --variant relay
    sudo python3 -m emulation.run emulation/scenarios/proposal_loss05.toml --variant baseline --seed 2
    python3 -m emulation.run emulation/scenarios/proposal_loss05.toml --dry-run   # no Mininet needed

Run directory ``runs/<scenario>_<variant>_s<seed>/`` (design section 3.3):
    n01.jsonl ... nNN.jsonl   node event logs (written by the Lifeline processes)
    n01.out ...               each process's stdout and stderr
    positions.jsonl           t, node, x, y once per second per node
    manifest.json             scenario, seed, variant, all parameters, git commit, start time
    plan.json                 the plan the run executed (placement, paths, workload)
    node.toml                 the node configuration (variant)
    workload/<node>.json      what each node must send, with times in seconds from run start
    state/                    per-node sequence state

The network setup follows the WP0 spike (Mininet-WiFi 2.7, wmediumd interference mode,
logDistance, ad hoc mode g on channel 5). Loss and delay are injected by the node's own
transport, not by the emulator. Node processes read the shared host monotonic clock, so
``--start-at`` is an absolute monotonic time and every node schedules its workload from it.

The node entrypoint must accept these flags (see "integration needs" in the WP9 notes):
--node-class, --receiver-delay-ms, --workload, --start-at, besides the existing ones.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from emulation.planner import Plan, NodePlan, build_plan, position_at, with_seed
from lifeline.runtime.config import NodeConfig, load_scenario

try:
    from mininet.log import log, setLogLevel
    from mn_wifi.link import adhoc, wmediumd
    from mn_wifi.net import Mininet_wifi
    from mn_wifi.wmediumdConnector import interference
    HAVE_MN = True
except ImportError:                      # lets --dry-run and the tests work anywhere
    HAVE_MN = False

    def info(msg):
        sys.stdout.write(msg)
        sys.stdout.flush()

    def setLogLevel(_level):
        pass

REPO = Path(__file__).resolve().parent.parent
BCAST = "10.255.255.255"                 # broadcast address of 10.0.0.0/8, as in the spike
NODE_MARKER = "-m lifeline --name"       # pattern that identifies our node processes


@dataclass(frozen=True)
class RunOptions:
    variant: str = "relay"
    out_root: Path = Path("runs")
    port: int = 5005
    exp: float = 4.0                     # logDistance exponent measured in WP0
    hand_tx: float | None = None         # dBm; if set, range= is not used (WP0 fallback)
    relay_tx: float | None = None
    startup_s: float | None = None       # time allowed for node start-up; default depends on size
    tail_s: float = 3.0                  # run on after duration_s so late events are logged
    step_s: float = 0.5                  # setPosition interval (WP0: 0.5 s steps work)
    position_log_s: float = 1.0
    node_python: str = sys.executable
    force: bool = False
    dry_run: bool = False


# --- pure helpers (no Mininet): these are what the unit tests exercise ----------------------

def run_dir_of(plan: Plan, variant: str, out_root: Path) -> Path:
    return Path(out_root) / f"{plan.name}_{variant}_s{plan.seed}"


def node_ip(index: int) -> str:
    """Station IP, 1-based index, same plan as the spike (10.0.0.N/8)."""
    if not 1 <= index <= 250:
        raise ValueError("the 10.0.0.0/8 plan used here supports 250 stations")
    return f"10.0.0.{index}"


def workload_files(plan: Plan) -> dict[str, list[dict]]:
    """Per-node workload: texts and SOS the node must start, seconds from run start."""
    work: dict[str, list[dict]] = {n.name: [] for n in plan.nodes}
    for job in plan.texts:
        work[job.sender].append({"t": job.t, "kind": "text", "to": job.recipient})
    for job in plan.sos:
        work[job.node].append({"t": job.t, "kind": "sos"})
    for items in work.values():
        items.sort(key=lambda item: item["t"])
    return work


def node_command(plan: Plan, node: NodePlan, index: int, rundir: Path, t0: float,
                 options: RunOptions) -> str:
    parts = [
        options.node_python, "-m", "lifeline",
        "--name", node.name,
        "--config", str(rundir / "node.toml"),
        "--seed", str(plan.seed),
        "--log-dir", str(rundir),
        "--state-dir", str(rundir / "state"),
        "--port", str(options.port),
        "--broadcast", BCAST,
        "--own-addr", node_ip(index),
        "--receiver-loss", str(plan.params["receiver_loss_probability"]),
        "--receiver-delay-ms", str(plan.params["per_hop_delay_ms"]),
        "--node-class", str(node.node_class),
        "--workload", str(rundir / "workload" / f"{node.name}.json"),
        "--start-at", f"{t0:.3f}",
    ]
    return " ".join(shlex.quote(p) for p in parts)


def positions_rows(plan: Plan, t: float) -> list[dict]:
    rows = []
    for node in plan.nodes:
        x, y = position_at(node.waypoints, t)
        rows.append({"t": round(t, 3), "node": node.name, "x": round(x, 2), "y": round(y, 2)})
    return rows


def git_state(repo: Path = REPO) -> dict:
    def run(*args: str) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=15)
            return out.stdout.strip() if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain")
    return {"commit": commit or "unknown", "dirty": bool(status) if status is not None else None}


def build_manifest(plan: Plan, options: RunOptions, t0: float, started_utc: str, git: dict) -> dict:
    return {
        "scenario": plan.name,
        "seed": plan.seed,
        "variant": options.variant,
        "duration_s": plan.duration_s,
        "area_m": [plan.width_m, plan.height_m],
        "nodes": {"count": len(plan.nodes),
                  "relays": sum(1 for n in plan.nodes if n.node_class == 1)},
        "plan_params": plan.params,
        "node_config": NodeConfig(variant=options.variant).to_dict(),
        "emulation": {
            "propagation": "logDistance", "exponent": options.exp, "port": options.port,
            "ssid": "adhocNet", "mode": "g", "channel": 5,
            "hand_tx_dbm": options.hand_tx, "relay_tx_dbm": options.relay_tx,
            "step_s": options.step_s, "position_log_s": options.position_log_s,
        },
        "git": git,
        "started_utc": started_utc,
        "t0_monotonic": round(t0, 3),
    }


def write_run_files(plan: Plan, options: RunOptions, rundir: Path, t0: float) -> None:
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "state").mkdir(exist_ok=True)
    (rundir / "workload").mkdir(exist_ok=True)
    (rundir / "node.toml").write_text(f'variant = "{options.variant}"\n', encoding="utf-8")
    (rundir / "plan.json").write_text(json.dumps(plan.to_dict(), indent=1), encoding="utf-8")
    for name, items in workload_files(plan).items():
        (rundir / "workload" / f"{name}.json").write_text(json.dumps(items), encoding="utf-8")
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest = build_manifest(plan, options, t0, started, git_state())
    (rundir / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


# --- Mininet-WiFi part (Linux, root) --------------------------------------------------------

def force_cleanup() -> None:
    try:
        from mn_wifi.clean import Cleanup
        Cleanup.cleanup_wifi()
    except Exception:
        pass
    subprocess.run("mn -c >/dev/null 2>&1", shell=True)


class _Emulation:
    """One Mininet-WiFi network for one plan. Use as a context manager."""

    def __init__(self, plan: Plan, options: RunOptions, rundir: Path) -> None:
        self.plan, self.options, self.rundir = plan, options, rundir
        self.net = None
        self.stas: dict = {}

    def __enter__(self):
        if not HAVE_MN:
            raise RuntimeError("Mininet-WiFi is not importable here; run on the emulation host with sudo.")
        self.net = Mininet_wifi(link=wmediumd, wmediumd_mode=interference)
        return self

    def build(self) -> None:
        for index, node in enumerate(self.plan.nodes, 1):
            x, y = position_at(node.waypoints, 0.0)
            kw = {}
            tx = self.options.relay_tx if node.node_class == 1 else self.options.hand_tx
            if tx is None:
                kw["range"] = node.range_m
            else:
                kw["txpower"] = tx
            self.stas[node.name] = self.net.addStation(
                node.name, ip=node_ip(index) + "/8", position=f"{x:.2f},{y:.2f},0", **kw)
        self.net.setPropagationModel(model="logDistance", exp=self.options.exp)
        self.net.configureNodes()
        for sta in self.stas.values():
            self.net.addLink(sta, cls=adhoc, intf=f"{sta.name}-wlan0", ssid="adhocNet", mode="g", channel=5)
        self.net.build()
        time.sleep(5.0 + 0.15 * len(self.stas))          # IBSS join, as in the spike

    def launch_nodes(self, t0: float) -> None:
        for index, node in enumerate(self.plan.nodes, 1):
            cmd = node_command(self.plan, node, index, self.rundir, t0, self.options)
            out = shlex.quote(str(self.rundir / f"{node.name}.out"))
            self.stas[node.name].cmd(
                f"cd {shlex.quote(str(REPO))} && PYTHONPATH={shlex.quote(str(REPO))} {cmd} > {out} 2>&1 &")

    def wait_for_logs(self, timeout: float = 60.0) -> list[str]:
        """Names of nodes whose log file did not appear in time."""
        deadline = time.monotonic() + timeout
        missing = [n.name for n in self.plan.nodes]
        while missing and time.monotonic() < deadline:
            missing = [m for m in missing if not (self.rundir / f"{m}.jsonl").exists()]
            time.sleep(0.2)
        return missing

    def run_mobility(self, t0: float) -> None:
        """setPosition on a schedule, and positions.jsonl once per position_log_s."""
        plan, opts = self.plan, self.options
        movers = [n for n in plan.nodes if len(n.waypoints) > 1]
        next_log = 0.0
        step = 0
        with open(self.rundir / "positions.jsonl", "w", encoding="utf-8") as log:
            while True:
                wait = t0 + step * opts.step_s - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                t = time.monotonic() - t0
                if t > plan.duration_s:
                    break
                for node in movers:
                    x, y = position_at(node.waypoints, t)
                    self.stas[node.name].setPosition(f"{x:.2f},{y:.2f},0")
                if t >= next_log:
                    for row in positions_rows(plan, t):
                        log.write(json.dumps(row) + "\n")
                    next_log += opts.position_log_s
                step += 1

    def stop_nodes(self, grace_s: float = 15.0) -> None:
        any_sta = next(iter(self.stas.values()))
        any_sta.cmd(f"pkill -TERM -f '{NODE_MARKER}' 2>/dev/null")   # SIGTERM: node flushes its log and exits
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline:
            if any_sta.cmd(f"pgrep -fc -- '{NODE_MARKER}'").strip().split()[-1:] == ["0"]:
                return
            time.sleep(0.3)
        any_sta.cmd(f"pkill -KILL -f '{NODE_MARKER}' 2>/dev/null")

    def __exit__(self, *exc):
        if self.net is not None:
            try:
                self.net.stop()
            except Exception:
                force_cleanup()
            self.net = None
        return False


def node_errors(plan: Plan, rundir: Path) -> list[str]:
    """Nodes whose stdout/stderr file contains a Python traceback."""
    bad = []
    for node in plan.nodes:
        path = rundir / f"{node.name}.out"
        try:
            if "Traceback" in path.read_text(encoding="utf-8", errors="replace"):
                bad.append(node.name)
        except OSError:
            bad.append(node.name)
    return bad


def run_scenario(scenario, options: RunOptions) -> Path:
    plan = build_plan(scenario)
    rundir = run_dir_of(plan, options.variant, options.out_root)
    if rundir.exists() and not options.force:
        raise FileExistsError(f"{rundir} already exists (use --force to overwrite)")
    startup = options.startup_s if options.startup_s is not None else 15.0 + 0.5 * len(plan.nodes)

    if options.dry_run:
        t0 = time.monotonic() + startup
        write_run_files(plan, options, rundir, t0)
        for index, node in enumerate(plan.nodes, 1):
            print(node_command(plan, node, index, rundir, t0, options))
        return rundir

    if not HAVE_MN:
        raise RuntimeError("Could not import Mininet-WiFi. Run this on the emulation host.")
    if os.geteuid() != 0:
        raise RuntimeError("Run with sudo: Mininet-WiFi needs root.")
    setLogLevel("info")
    try:
        with _Emulation(plan, options, rundir) as emu:
            emu.build()
            t0 = time.monotonic() + startup
            write_run_files(plan, options, rundir, t0)
            emu.launch_nodes(t0)
            missing = emu.wait_for_logs()
            late = time.monotonic() > t0
            info(f"nodes launched; start in {max(0.0, t0 - time.monotonic()):.1f} s"
                 + (f"; no log yet from {missing}" if missing else "") + "\n")
            emu.run_mobility(t0)
            time.sleep(options.tail_s)
            emu.stop_nodes()
    except BaseException:
        force_cleanup()
        raise
    manifest_path = rundir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"finished_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "nodes_without_log_at_start": missing, "started_late": late,
                     "nodes_with_errors": node_errors(plan, rundir)})
    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return rundir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m emulation.run", description="Run one seeded scenario.")
    p.add_argument("scenario", type=Path, help="scenario TOML (contract C4 section 4.2)")
    p.add_argument("--variant", choices=("relay", "baseline"), default="relay")
    p.add_argument("--seed", type=int, default=None, help="override run.seed")
    p.add_argument("--out", type=Path, default=Path("runs"))
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--exp", type=float, default=4.0)
    p.add_argument("--hand-tx", type=float, default=None, help="dBm; skips range= (WP0 fallback)")
    p.add_argument("--relay-tx", type=float, default=None, help="dBm; skips range= (WP0 fallback)")
    p.add_argument("--startup-s", type=float, default=None)
    p.add_argument("--force", action="store_true", help="overwrite an existing run directory")
    p.add_argument("--dry-run", action="store_true", help="write the run files and print node commands only")
    ns = p.parse_args(argv)
    options = RunOptions(variant=ns.variant, out_root=ns.out, port=ns.port, exp=ns.exp,
                         hand_tx=ns.hand_tx, relay_tx=ns.relay_tx, startup_s=ns.startup_s,
                         force=ns.force, dry_run=ns.dry_run)
    try:
        scenario = load_scenario(ns.scenario)
        if ns.seed is not None:
            scenario = with_seed(scenario, ns.seed)
        rundir = run_scenario(scenario, options)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 2
    print(f"run directory: {rundir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())