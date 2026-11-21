"""WP9 planner checks: same seed, same plan; mobility and workload obey their rules.

    python3 -m pytest tests/test_planner.py -v
"""

import math
from pathlib import Path

import pytest

from emulation.planner import (
    NODE_CLASS_HANDHELD,
    NODE_CLASS_RELAY,
    build_plan,
    largest_component_fraction,
    position_at,
    with_seed,
)
from lifeline.runtime.config import (
    AreaSection,
    ContactsSection,
    LossSection,
    MobilitySection,
    NodesSection,
    RunSection,
    Scenario,
    WorkloadSection,
    load_scenario,
)


def _scenario(seed=1, duration=600.0, area=(1200.0, 1200.0), count=30, relays=6,
              hh_range=100.0, relay_range=500.0, model="random_waypoint", speeds=(0.5, 2.0),
              texts=20, min_sep=300.0, sos=3, pairs=(("n01", "n02"),)):
    return Scenario(
        run=RunSection(name="t", seed=seed, duration_s=duration),
        area=AreaSection(width_m=area[0], height_m=area[1]),
        nodes=NodesSection(count=count, relay_count=relays,
                           handheld_range_m=hh_range, relay_range_m=relay_range),
        mobility=MobilitySection(model=model, speed_min_mps=speeds[0], speed_max_mps=speeds[1]),
        loss=LossSection(receiver_loss_probability=0.05, per_hop_delay_ms=20.0),
        workload=WorkloadSection(texts=texts, text_min_separation_m=min_sep, sos_beacons=sos),
        contacts=ContactsSection(verified_pairs=tuple(pairs)),
    )


def test_same_seed_gives_the_same_plan_and_another_seed_differs():
    a, b, c = build_plan(_scenario(seed=1)), build_plan(_scenario(seed=1)), build_plan(_scenario(seed=2))
    assert a == b
    assert a != c
    assert build_plan(with_seed(_scenario(seed=1), 2)) == c


def test_node_names_classes_and_ranges():
    plan = build_plan(_scenario(count=30, relays=6))
    assert plan.nodes[0].name == "n01" and plan.nodes[-1].name == "n30"
    assert [n.node_class for n in plan.nodes] == [NODE_CLASS_HANDHELD] * 24 + [NODE_CLASS_RELAY] * 6
    assert {n.range_m for n in plan.nodes if n.node_class == NODE_CLASS_HANDHELD} == {100.0}
    assert {n.range_m for n in plan.nodes if n.node_class == NODE_CLASS_RELAY} == {500.0}


def test_random_waypoint_stays_in_the_area_with_speeds_in_range():
    plan = build_plan(_scenario(duration=600.0))
    for node in plan.nodes:
        if node.node_class == NODE_CLASS_RELAY:
            continue
        assert node.waypoints[-1][0] >= 600.0  # the path covers the whole run
        for t, x, y in node.waypoints:
            assert 0.0 <= x <= 1200.0 and 0.0 <= y <= 1200.0
        for (t0, x0, y0), (t1, x1, y1) in zip(node.waypoints, node.waypoints[1:]):
            distance = math.dist((x0, y0), (x1, y1))
            if distance > 0:
                assert 0.5 - 1e-9 <= distance / (t1 - t0) <= 2.0 + 1e-9


def test_relays_are_static_unless_asked_to_move():
    still = build_plan(_scenario())
    moving = build_plan(_scenario(), relays_move=True)
    relays = lambda p: [n for n in p.nodes if n.node_class == NODE_CLASS_RELAY]
    assert all(len(n.waypoints) == 1 for n in relays(still))
    assert all(len(n.waypoints) > 1 for n in relays(moving))


def test_static_model_never_moves_and_zero_min_speed_is_rejected():
    plan = build_plan(_scenario(model="static"))
    assert all(len(n.waypoints) == 1 for n in plan.nodes)
    with pytest.raises(ValueError):
        build_plan(_scenario(speeds=(0.0, 2.0)))
    with pytest.raises(ValueError):
        build_plan(_scenario(model="teleport"))


def test_a_nodes_path_does_not_depend_on_how_many_nodes_there_are():
    small = build_plan(_scenario(count=10, relays=1, texts=0, sos=0))
    large = build_plan(_scenario(count=11, relays=1, texts=0, sos=0))
    assert small.node("n01") == large.node("n01")
    assert small.node("n05") == large.node("n05")


def test_position_at_interpolates_and_clamps():
    path = ((0.0, 0.0, 0.0), (10.0, 100.0, 0.0), (20.0, 100.0, 0.0))  # walk, then pause
    assert position_at(path, 5.0) == (50.0, 0.0)
    assert position_at(path, 15.0) == (100.0, 0.0)
    assert position_at(path, -1.0) == (0.0, 0.0)
    assert position_at(path, 99.0) == (100.0, 0.0)
    assert position_at(((0.0, 7.0, 8.0),), 3.0) == (7.0, 8.0)


def test_texts_fall_in_the_window_between_distant_handhelds():
    plan = build_plan(_scenario(texts=25, min_sep=300.0), warmup_s=60.0, drain_s=120.0)
    handhelds = {n.name for n in plan.nodes if n.node_class == NODE_CLASS_HANDHELD}
    assert len(plan.texts) == 25
    assert [j.t for j in plan.texts] == sorted(j.t for j in plan.texts)
    for job in plan.texts:
        assert 60.0 <= job.t <= 480.0  # duration 600 minus drain 120
        assert job.sender != job.recipient
        assert job.sender in handhelds and job.recipient in handhelds
        apart = math.dist(plan.position_of(job.sender, job.t), plan.position_of(job.recipient, job.t))
        assert apart >= 300.0


def test_an_impossible_separation_is_reported():
    with pytest.raises(ValueError):
        build_plan(_scenario(area=(100.0, 100.0), min_sep=5000.0))


def test_sos_beacons_use_distinct_handhelds_inside_the_window():
    plan = build_plan(_scenario(sos=5))
    handhelds = {n.name for n in plan.nodes if n.node_class == NODE_CLASS_HANDHELD}
    assert len(plan.sos) == 5
    assert len({j.node for j in plan.sos}) == 5 and {j.node for j in plan.sos} <= handhelds
    assert all(60.0 <= j.t <= 480.0 for j in plan.sos)
    with pytest.raises(ValueError):
        build_plan(_scenario(count=4, relays=1, sos=5))


def test_verified_pairs_must_name_existing_nodes():
    assert build_plan(_scenario(pairs=(("n01", "n02"),))).verified_pairs == (("n01", "n02"),)
    with pytest.raises(ValueError):
        build_plan(_scenario(pairs=(("n01", "n99"),)))


def test_a_run_too_short_for_warmup_and_drain_is_rejected():
    with pytest.raises(ValueError):
        build_plan(_scenario(duration=100.0))


def test_largest_component_fraction_is_a_layout_sanity_check():
    packed = build_plan(_scenario(area=(50.0, 50.0), count=10, relays=1, texts=0, sos=0, min_sep=0.0))
    assert largest_component_fraction(packed) == 1.0
    scattered = build_plan(_scenario(count=10, relays=1, hh_range=1.0, relay_range=1.0,
                                     model="static", texts=0, sos=0))
    assert largest_component_fraction(scattered) <= 0.3


def test_plan_serializes_and_the_example_scenario_builds():
    plan = build_plan(_scenario())
    data = plan.to_dict()
    assert data["seed"] == 1 and len(data["nodes"]) == 30 and len(data["texts"]) == 20
    assert data["params"]["relays_move"] is False
    path = Path(__file__).resolve().parent.parent / "emulation" / "scenarios" / "loss5.toml"
    print(path)
    example = build_plan(load_scenario(path))
    assert len(example.nodes) == 30 and len(example.texts) == 30 and len(example.sos) == 3