from pathlib import Path

import pytest

from lifeline.config import (
    ConfigError,
    NodeConfig,
    load_node_config,
    load_scenario,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"


def write(tmp_path: Path, text: str, name: str = "c.toml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- NodeConfig defaults ------------------------------------------------------

def test_defaults_match_c4_table():
    cfg = NodeConfig()
    assert cfg.variant == "relay"
    assert cfg.hello_interval_s == 2.0
    assert cfg.neighbor_timeout_missed == 3
    assert cfg.default_ttl == 16
    assert cfg.jitter_ms == (10, 80)
    assert cfg.counter_suppression_k == 2
    assert cfg.seq_window == 256
    assert cfg.buffer_capacity == 200
    assert cfg.retention_s == 1800
    assert cfg.reoffer_initial_s == 5
    assert cfg.reoffer_factor == 2
    assert cfg.reoffer_max_attempts == 5
    assert cfg.rate_limit_tx_fps == 20
    assert cfg.rate_limit_sos_origin_s == 10
    assert cfg.sos_repeat_interval_s == 15


def test_empty_file_gives_all_defaults(tmp_path):
    assert load_node_config(write(tmp_path, "")) == NodeConfig()


def test_full_file_loads_every_key(tmp_path):
    text = (EXAMPLES / "node.toml").read_text(encoding="utf-8")
    assert load_node_config(write(tmp_path, text)) == NodeConfig()


def test_partial_file_overrides_only_given_keys(tmp_path):
    cfg = load_node_config(write(tmp_path, 'default_ttl = 8\nvariant = "baseline"\n'))
    assert cfg.default_ttl == 8
    assert cfg.variant == "baseline"
    assert cfg.buffer_capacity == 200


def test_jitter_array_becomes_tuple(tmp_path):
    cfg = load_node_config(write(tmp_path, "jitter_ms = [5, 40]\n"))
    assert cfg.jitter_ms == (5, 40)


def test_example_node_file_loads():
    assert load_node_config(EXAMPLES / "node.toml") == NodeConfig()


# --- load errors -------------------------------------------------------------

def test_unknown_key_in_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="hello_intrval_s"):
        load_node_config(write(tmp_path, "hello_intrval_s = 1.0\n"))


def test_invalid_toml_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_node_config(write(tmp_path, "variant = \n"))


def test_wrong_type_names_the_key(tmp_path):
    with pytest.raises(ConfigError, match="default_ttl must be an integer"):
        load_node_config(write(tmp_path, 'default_ttl = "16"\n'))


def test_boolean_is_not_a_number(tmp_path):
    with pytest.raises(ConfigError, match="not a boolean"):
        load_node_config(write(tmp_path, "hello_interval_s = true\n"))


def test_variant_must_be_relay_or_baseline(tmp_path):
    with pytest.raises(ConfigError, match="variant"):
        load_node_config(write(tmp_path, 'variant = "naive"\n'))


@pytest.mark.parametrize(
    "line, key",
    [
        ("hello_interval_s = 0", "hello_interval_s"),
        ("hello_interval_s = -1.0", "hello_interval_s"),
        ("retention_s = 0", "retention_s"),
        ("reoffer_initial_s = 0", "reoffer_initial_s"),
        ("rate_limit_tx_fps = 0", "rate_limit_tx_fps"),
        ("rate_limit_sos_origin_s = 0", "rate_limit_sos_origin_s"),
        ("sos_repeat_interval_s = 0", "sos_repeat_interval_s"),
        ("neighbor_timeout_missed = 0", "neighbor_timeout_missed"),
        ("counter_suppression_k = 0", "counter_suppression_k"),
        ("seq_window = 0", "seq_window"),
        ("buffer_capacity = 0", "buffer_capacity"),
        ("reoffer_factor = 0.5", "reoffer_factor"),
        ("reoffer_max_attempts = -1", "reoffer_max_attempts"),
        ("default_ttl = 0", "default_ttl"),
        ("default_ttl = 256", "default_ttl"),
    ],
)
def test_out_of_range_values_name_the_key(tmp_path, line, key):
    with pytest.raises(ConfigError, match=key):
        load_node_config(write(tmp_path, line + "\n"))


def test_jitter_minimum_above_maximum_rejected(tmp_path):
    with pytest.raises(ConfigError, match="jitter_ms minimum must not exceed maximum"):
        load_node_config(write(tmp_path, "jitter_ms = [80, 10]\n"))


def test_jitter_negative_minimum_rejected(tmp_path):
    with pytest.raises(ConfigError, match="jitter_ms minimum must be non-negative"):
        load_node_config(write(tmp_path, "jitter_ms = [-1, 10]\n"))


def test_jitter_wrong_shape_rejected(tmp_path):
    with pytest.raises(ConfigError, match="jitter_ms"):
        load_node_config(write(tmp_path, "jitter_ms = [10]\n"))


def test_sos_repeat_must_exceed_rate_limit(tmp_path):
    with pytest.raises(ConfigError, match="sos_repeat_interval_s must be greater"):
        load_node_config(
            write(tmp_path, "rate_limit_sos_origin_s = 15\nsos_repeat_interval_s = 15\n")
        )


def test_missing_file_raises_os_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_node_config(tmp_path / "absent.toml")


# --- variant flags ------------------------------------------------------------

def test_relay_enables_all_extra_mechanisms():
    cfg = NodeConfig(variant="relay")
    assert cfg.use_jitter
    assert cfg.use_counter_suppression
    assert cfg.use_rate_limits
    assert cfg.use_priorities
    assert cfg.use_duplicate_suppression


def test_baseline_disables_exactly_the_four_mechanisms_but_keeps_duplicates():
    cfg = NodeConfig(variant="baseline")
    assert not cfg.use_jitter
    assert not cfg.use_counter_suppression
    assert not cfg.use_rate_limits
    assert not cfg.use_priorities
    assert cfg.use_duplicate_suppression


def test_baseline_keeps_parameter_values():
    cfg = NodeConfig(variant="baseline")
    assert cfg.jitter_ms == (10, 80)
    assert cfg.counter_suppression_k == 2
    assert cfg.rate_limit_tx_fps == 20


def test_variant_flags_are_read_only():
    cfg = NodeConfig()
    with pytest.raises(AttributeError):
        cfg.use_jitter = False  # type: ignore[misc]


def test_config_is_frozen():
    with pytest.raises(AttributeError):
        NodeConfig().default_ttl = 3  # type: ignore[misc]


# --- to_dict and replace ----------------------------------------------------

def test_to_dict_is_json_serializable_and_complete():
    import json

    data = NodeConfig().to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["jitter_ms"] == [10, 80]
    assert set(data) == {
        "variant", "hello_interval_s", "neighbor_timeout_missed", "default_ttl",
        "jitter_ms", "counter_suppression_k", "seq_window", "buffer_capacity",
        "retention_s", "reoffer_initial_s", "reoffer_factor", "reoffer_max_attempts",
        "rate_limit_tx_fps", "rate_limit_sos_origin_s", "sos_repeat_interval_s",
    }


def test_to_dict_round_trips_through_load(tmp_path):
    import json

    original = NodeConfig(variant="baseline", default_ttl=9, jitter_ms=(0, 30))
    path = write(tmp_path, "\n".join(f"{k} = {_toml_value(v)}" for k, v in original.to_dict().items()))
    assert load_node_config(path) == original


def _toml_value(value):
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def test_replace_returns_validated_copy():
    base = NodeConfig()
    changed = base.replace(default_ttl=4)
    assert changed.default_ttl == 4
    assert base.default_ttl == 16


def test_replace_rejects_invalid_value():
    with pytest.raises(ConfigError, match="default_ttl"):
        NodeConfig().replace(default_ttl=0)


def test_replace_rejects_unknown_key():
    with pytest.raises(ConfigError, match="unknown key"):
        NodeConfig().replace(nonsense=1)


def test_replace_validates_jitter_and_cross_constraint():
    with pytest.raises(ConfigError, match="jitter_ms minimum"):
        NodeConfig().replace(jitter_ms=(90, 10))
    with pytest.raises(ConfigError, match="sos_repeat_interval_s must be greater"):
        NodeConfig().replace(sos_repeat_interval_s=5)


def test_direct_construction_is_validated():
    with pytest.raises(ConfigError):
        NodeConfig(variant="weird")


# --- scenario -----------------------------------------------------------------

def test_example_scenario_loads():
    scenario = load_scenario(EXAMPLES / "scenario.toml")
    assert scenario.run.name == "smoke-5-nodes"
    assert scenario.run.seed == 12345
    assert scenario.nodes.count == 5
    assert scenario.nodes.relay_count == 1
    assert scenario.contacts.verified_pairs == (("n01", "n02"),)


SCENARIO_TEMPLATE = """
[run]
name = "t"
seed = 1
duration_s = 10

[area]
width_m = 100
height_m = 100

[nodes]
count = 4
relay_count = 1
handheld_range_m = 100
relay_range_m = 500

[mobility]
model = "static"
speed_min_mps = 0
speed_max_mps = 1

[loss]
receiver_loss_probability = 0.1
per_hop_delay_ms = 0

[workload]
texts = 0
text_min_separation_m = 0
sos_beacons = 0

[contacts]
verified_pairs = []
"""


def test_scenario_template_is_valid(tmp_path):
    scenario = load_scenario(write(tmp_path, SCENARIO_TEMPLATE))
    assert scenario.contacts.verified_pairs == ()
    assert scenario.loss.receiver_loss_probability == 0.1


def test_scenario_missing_section_rejected(tmp_path):
    text = SCENARIO_TEMPLATE.split("[loss]")[0]
    with pytest.raises(ConfigError, match="missing section"):
        load_scenario(write(tmp_path, text))


def test_scenario_unknown_section_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown section"):
        load_scenario(write(tmp_path, SCENARIO_TEMPLATE + "\n[extra]\nx = 1\n"))


def test_scenario_unknown_key_rejected(tmp_path):
    text = SCENARIO_TEMPLATE.replace("texts = 0", "texts = 0\nfoo = 1")
    with pytest.raises(ConfigError, match="foo"):
        load_scenario(write(tmp_path, text))


def test_scenario_missing_key_rejected(tmp_path):
    text = SCENARIO_TEMPLATE.replace("sos_beacons = 0\n", "")
    with pytest.raises(ConfigError, match="sos_beacons"):
        load_scenario(write(tmp_path, text))


@pytest.mark.parametrize(
    "old, new, message",
    [
        ("count = 4", "count = 0\nrelay_count = 1", "nodes.count must be >= nodes.relay_count"),
        ("relay_count = 1", "relay_count = 5", "nodes.count must be >= nodes.relay_count"),
        ("relay_count = 1", "relay_count = -1", "nodes.count must be >= nodes.relay_count"),
        ("receiver_loss_probability = 0.1", "receiver_loss_probability = 1.5", "in [0, 1]"),
        ("receiver_loss_probability = 0.1", "receiver_loss_probability = -0.1", "in [0, 1]"),
        ("width_m = 100", "width_m = 0", "area.width_m"),
        ("height_m = 100", "height_m = -5", "area.height_m"),
        ("handheld_range_m = 100", "handheld_range_m = 0", "handheld_range_m"),
        ("relay_range_m = 500", "relay_range_m = 0", "relay_range_m"),
        ("speed_max_mps = 1", "speed_max_mps = 0", "speed_max_mps"),
        ("speed_min_mps = 0", "speed_min_mps = -1", "speed_min_mps"),
        ("duration_s = 10", "duration_s = 0", "duration_s"),
        ("seed = 1", "seed = -1", "run.seed"),
        ('name = "t"', 'name = ""', "run.name"),
        ("texts = 0", "texts = -2", "workload.texts"),
        ("per_hop_delay_ms = 0", "per_hop_delay_ms = -1", "per_hop_delay_ms"),
    ],
)
def test_scenario_invalid_values_rejected(tmp_path, old, new, message):
    text = SCENARIO_TEMPLATE.replace(old, new, 1)
    with pytest.raises(ConfigError, match=message):
        load_scenario(write(tmp_path, text))


def test_scenario_wrong_type_names_the_key(tmp_path):
    text = SCENARIO_TEMPLATE.replace("count = 4", 'count = "4"')
    with pytest.raises(ConfigError, match="nodes.count must be an integer"):
        load_scenario(write(tmp_path, text))


def test_verified_pairs_shape_checked(tmp_path):
    text = SCENARIO_TEMPLATE.replace("verified_pairs = []", 'verified_pairs = [["n01"]]')
    with pytest.raises(ConfigError, match="two-element list"):
        load_scenario(write(tmp_path, text))


def test_verified_pairs_must_be_distinct(tmp_path):
    text = SCENARIO_TEMPLATE.replace("verified_pairs = []", 'verified_pairs = [["n01", "n01"]]')
    with pytest.raises(ConfigError, match="distinct"):
        load_scenario(write(tmp_path, text))


def test_verified_pairs_parsed_to_tuples(tmp_path):
    text = SCENARIO_TEMPLATE.replace("verified_pairs = []", 'verified_pairs = [["n01", "n02"], ["n03", "n04"]]')
    scenario = load_scenario(write(tmp_path, text))
    assert scenario.contacts.verified_pairs == (("n01", "n02"), ("n03", "n04"))