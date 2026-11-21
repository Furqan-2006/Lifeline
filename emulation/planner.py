"""Scenario planner (WP9): turns a scenario file and a seed into one reproducible plan.

The plan holds everything a run needs to be repeated exactly: node names and
classes, start positions and random-waypoint paths, the text workload and the
SOS schedule. It does not depend on the protocol variant, so Relay and baseline
runs of the same seed see the same nodes, the same movement and the same
messages. The runner (Linux, Mininet-WiFi) only executes the plan.

Command line, no emulator needed::

    python3 -m emulation.planner emulation/scenarios/proposal_loss05.toml --out plan.json
    python3 -m emulation.planner scenario.toml --seed 2

Conventions that are not in contract C4 (all recorded in ``Plan.params``):
  * the last ``relay_count`` nodes are relays; they stay where they are placed
    unless ``relays_move`` is set
  * random waypoint needs ``speed_min_mps`` above zero (with zero, nodes slow
    down and get stuck); pauses are uniform in [0, ``pause_max_s``]
  * texts and SOS start between ``warmup_s`` and ``duration_s - drain_s``, so
    neighbor discovery has finished and late messages have time to be delivered
  * texts go between two different handhelds that are at least
    ``text_min_separation_m`` apart at the moment of sending
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_right
from dataclasses import dataclass, replace
from pathlib import Path

from lifeline.runtime.config import Scenario, load_scenario
from lifeline.runtime.rng import Rng

NODE_CLASS_HANDHELD = 0
NODE_CLASS_RELAY = 1
MOBILITY_MODELS = ("random_waypoint", "static")

Waypoint = tuple[float, float, float]  # time in seconds, x, y; straight lines in between


@dataclass(frozen=True)
class NodePlan:
    name: str
    node_class: int
    range_m: float
    waypoints: tuple[Waypoint, ...]


@dataclass(frozen=True)
class TextJob:
    t: float
    sender: str
    recipient: str


@dataclass(frozen=True)
class SosJob:
    t: float
    node: str


@dataclass(frozen=True)
class Plan:
    name: str
    seed: int
    duration_s: float
    width_m: float
    height_m: float
    params: dict
    nodes: tuple[NodePlan, ...]
    texts: tuple[TextJob, ...]
    sos: tuple[SosJob, ...]
    verified_pairs: tuple[tuple[str, str], ...]

    def node(self, name: str) -> NodePlan:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(name)

    def position_of(self, name: str, t: float) -> tuple[float, float]:
        return position_at(self.node(name).waypoints, t)

    def to_dict(self) -> dict:
        """JSON-serializable form, for the run directory and the manifest."""
        return {
            "name": self.name,
            "seed": self.seed,
            "duration_s": self.duration_s,
            "area": [self.width_m, self.height_m],
            "params": self.params,
            "nodes": [
                {
                    "name": n.name,
                    "node_class": n.node_class,
                    "range_m": n.range_m,
                    "waypoints": [list(w) for w in n.waypoints],
                }
                for n in self.nodes
            ],
            "texts": [{"t": j.t, "sender": j.sender, "recipient": j.recipient} for j in self.texts],
            "sos": [{"t": j.t, "node": j.node} for j in self.sos],
            "verified_pairs": [list(p) for p in self.verified_pairs],
        }


def position_at(waypoints: tuple[Waypoint, ...], t: float) -> tuple[float, float]:
    """Position at time ``t``: straight lines between waypoints, clamped at both ends."""
    if t <= waypoints[0][0]:
        return waypoints[0][1], waypoints[0][2]
    if t >= waypoints[-1][0]:
        return waypoints[-1][1], waypoints[-1][2]
    i = bisect_right(waypoints, t, key=lambda w: w[0])
    t0, x0, y0 = waypoints[i - 1]
    t1, x1, y1 = waypoints[i]
    if t1 <= t0:
        return x1, y1
    f = (t - t0) / (t1 - t0)
    return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f


def with_seed(scenario: Scenario, seed: int) -> Scenario:
    """The same scenario under another seed (the three evaluation seeds)."""
    return replace(scenario, run=replace(scenario.run, seed=seed))


def _random_waypoint(rng: Rng, start: tuple[float, float], width: float, height: float,
                     speed_min: float, speed_max: float, duration: float,
                     pause_max: float) -> tuple[Waypoint, ...]:
    points: list[Waypoint] = [(0.0, start[0], start[1])]
    t, x, y = 0.0, start[0], start[1]
    while t < duration:
        nx, ny = rng.uniform(0.0, width), rng.uniform(0.0, height)
        speed = rng.uniform(speed_min, speed_max)
        t += max(math.dist((x, y), (nx, ny)) / speed, 1e-6)
        x, y = nx, ny
        points.append((t, x, y))
        pause = rng.uniform(0.0, pause_max) if pause_max > 0 else 0.0
        if pause > 0:
            t += pause
            points.append((t, x, y))
    return tuple(points)


def build_plan(scenario: Scenario, *, warmup_s: float = 60.0, drain_s: float = 120.0,
               pause_max_s: float = 30.0, relays_move: bool = False) -> Plan:
    run, area, nodes, mob = scenario.run, scenario.area, scenario.nodes, scenario.mobility
    if mob.model not in MOBILITY_MODELS:
        raise ValueError(f"mobility.model must be one of {list(MOBILITY_MODELS)}, got {mob.model!r}")
    if mob.model == "random_waypoint" and mob.speed_min_mps <= 0:
        raise ValueError("random_waypoint needs mobility.speed_min_mps above zero")
    if warmup_s < 0 or drain_s < 0 or pause_max_s < 0:
        raise ValueError("warmup_s, drain_s and pause_max_s must be non-negative")

    root = Rng(run.seed).child("plan")
    width = max(2, len(str(nodes.count)))
    names = [f"n{i:0{width}d}" for i in range(1, nodes.count + 1)]
    first_relay = nodes.count - nodes.relay_count

    planned: list[NodePlan] = []
    for index, name in enumerate(names):
        is_relay = index >= first_relay
        rng = root.child(f"node:{name}")  # own stream: other nodes' paths do not depend on this one
        start = (rng.uniform(0.0, area.width_m), rng.uniform(0.0, area.height_m))
        moves = mob.model == "random_waypoint" and (relays_move or not is_relay)
        waypoints = (
            _random_waypoint(rng, start, area.width_m, area.height_m,
                             mob.speed_min_mps, mob.speed_max_mps, run.duration_s, pause_max_s)
            if moves else ((0.0, start[0], start[1]),)
        )
        planned.append(NodePlan(
            name=name,
            node_class=NODE_CLASS_RELAY if is_relay else NODE_CLASS_HANDHELD,
            range_m=nodes.relay_range_m if is_relay else nodes.handheld_range_m,
            waypoints=waypoints,
        ))

    handhelds = [n for n in planned if n.node_class == NODE_CLASS_HANDHELD]
    work = scenario.workload
    lo, hi = warmup_s, run.duration_s - drain_s
    if (work.texts or work.sos_beacons) and hi <= lo:
        raise ValueError("duration_s is too short for the warmup and drain windows")

    texts: list[TextJob] = []
    if work.texts:
        if len(handhelds) < 2:
            raise ValueError("texts need at least two handheld nodes")
        trng = root.child("texts")
        for t in sorted(trng.uniform(lo, hi) for _ in range(work.texts)):
            for _ in range(1000):
                sender, recipient = trng.choice(handhelds), trng.choice(handhelds)
                if sender is recipient:
                    continue
                if math.dist(position_at(sender.waypoints, t),
                             position_at(recipient.waypoints, t)) >= work.text_min_separation_m:
                    texts.append(TextJob(t, sender.name, recipient.name))
                    break
            else:
                raise ValueError(
                    f"no handheld pair is {work.text_min_separation_m} m apart at t={t:.1f}; "
                    "lower text_min_separation_m or enlarge the area"
                )

    sos: list[SosJob] = []
    if work.sos_beacons:
        if work.sos_beacons > len(handhelds):
            raise ValueError("sos_beacons is larger than the number of handheld nodes")
        srng = root.child("sos")
        chosen = list(handhelds)
        srng.shuffle(chosen)
        times = sorted(srng.uniform(lo, hi) for _ in range(work.sos_beacons))
        sos = [SosJob(t, node.name) for t, node in zip(times, chosen)]

    known = set(names)
    for pair in scenario.contacts.verified_pairs:
        if not set(pair) <= known:
            raise ValueError(f"verified pair {pair} names a node that does not exist")

    return Plan(
        name=run.name,
        seed=run.seed,
        duration_s=run.duration_s,
        width_m=area.width_m,
        height_m=area.height_m,
        params={
            "mobility_model": mob.model,
            "speed_min_mps": mob.speed_min_mps,
            "speed_max_mps": mob.speed_max_mps,
            "pause_max_s": pause_max_s,
            "relays_move": relays_move,
            "warmup_s": warmup_s,
            "drain_s": drain_s,
            "receiver_loss_probability": scenario.loss.receiver_loss_probability,
            "per_hop_delay_ms": scenario.loss.per_hop_delay_ms,
        },
        nodes=tuple(planned),
        texts=tuple(texts),
        sos=tuple(sos),
        verified_pairs=scenario.contacts.verified_pairs,
    )


def largest_component_fraction(plan: Plan, t: float = 0.0) -> float:
    """Share of nodes in the largest connected group at time ``t``.

    Ideal-disc model as in the WP0 report: two nodes are linked when they are
    within the larger of their two ranges (a handheld reaches a relay at the
    relay's range). A layout sanity check, not a prediction of delivery.
    """
    n = len(plan.nodes)
    if n == 0:
        return 0.0
    positions = [position_at(node.waypoints, t) for node in plan.nodes]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            reach = max(plan.nodes[i].range_m, plan.nodes[j].range_m)
            if math.dist(positions[i], positions[j]) <= reach:
                parent[find(i)] = find(j)
    sizes: dict[int, int] = {}
    for i in range(n):
        root = find(i)
        sizes[root] = sizes.get(root, 0) + 1
    return max(sizes.values()) / n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m emulation.planner",
                                     description="Build a run plan from a scenario file.")
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--seed", type=int, default=None, help="override run.seed")
    parser.add_argument("--out", type=Path, default=None, help="write the plan as JSON (default: summary only)")
    ns = parser.parse_args(argv)
    try:
        scenario = load_scenario(ns.scenario)
        if ns.seed is not None:
            scenario = with_seed(scenario, ns.seed)
        plan = build_plan(scenario)
    except (ValueError, OSError) as exc:
        print(f"planner: {exc}", file=sys.stderr)
        return 2
    if ns.out is not None:
        ns.out.parent.mkdir(parents=True, exist_ok=True)
        ns.out.write_text(json.dumps(plan.to_dict(), indent=1), encoding="utf-8")
    relays = sum(1 for n in plan.nodes if n.node_class == NODE_CLASS_RELAY)
    print(f"{plan.name} seed={plan.seed}: {len(plan.nodes)} nodes ({relays} relays), "
          f"{len(plan.texts)} texts, {len(plan.sos)} SOS, "
          f"largest component at t=0: {largest_component_fraction(plan):.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())