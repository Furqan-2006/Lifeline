"""Node and scenario configuration (contract C4).

Pure functions only: nothing is cached, nothing global, and the only file
read is the path the caller gives.

Test setup::

    from lifeline.config import NodeConfig, load_node_config

    cfg = NodeConfig()                       # all C4 defaults
    fast = cfg.replace(hello_interval_s=1.0) # validated copy
    assert not NodeConfig(variant="baseline").use_jitter
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

VARIANTS = ("relay", "baseline")

# --- Bounds (contract question 2). Each is a one-line change. ----------------
_TTL_MAX = 255
_NUMERIC_BOUNDS: dict[str, tuple[float, float | None]] = {
    # name: (minimum allowed, maximum allowed or None for no maximum)
    "hello_interval_s": (0.0, None),           # must be > 0, checked below
    "neighbor_timeout_missed": (1, None),
    "counter_suppression_k": (1, None),
    "seq_window": (1, None),
    "buffer_capacity": (1, None),
    "retention_s": (0.0, None),                # must be > 0, checked below
    "reoffer_initial_s": (0.0, None),          # must be > 0, checked below
    "reoffer_factor": (1.0, None),
    "reoffer_max_attempts": (0, None),
    "rate_limit_tx_fps": (0.0, None),          # must be > 0, checked below
    "rate_limit_sos_origin_s": (0.0, None),    # must be > 0, checked below
    "sos_repeat_interval_s": (0.0, None),      # must be > 0, checked below
}
_STRICTLY_POSITIVE = {
    "hello_interval_s",
    "retention_s",
    "reoffer_initial_s",
    "rate_limit_tx_fps",
    "rate_limit_sos_origin_s",
    "sos_repeat_interval_s",
}
_INT_KEYS = {
    "neighbor_timeout_missed",
    "default_ttl",
    "counter_suppression_k",
    "seq_window",
    "buffer_capacity",
    "reoffer_max_attempts",
}


class ConfigError(ValueError):
    """Raised for any invalid configuration. The message names the key."""


@dataclass(frozen=True)
class NodeConfig:
    """Node parameters, C4 section 4.1. Defaults are the initial values from the architecture document."""

    variant: str = "relay"
    hello_interval_s: float = 2.0
    neighbor_timeout_missed: int = 3
    default_ttl: int = 16
    jitter_ms: tuple[float, float] = (10, 80)
    counter_suppression_k: int = 2
    seq_window: int = 256
    buffer_capacity: int = 200
    retention_s: float = 1800
    reoffer_initial_s: float = 5
    reoffer_factor: float = 2
    reoffer_max_attempts: int = 5
    rate_limit_tx_fps: float = 20
    rate_limit_sos_origin_s: float = 10
    sos_repeat_interval_s: float = 15

    def __post_init__(self) -> None:
        _validate_node(self)

    # --- derived variant flags (C4 4.1) -------------------------------------

    @property
    def use_jitter(self) -> bool:
        return self.variant == "relay"

    @property
    def use_counter_suppression(self) -> bool:
        return self.variant == "relay"

    @property
    def use_rate_limits(self) -> bool:
        return self.variant == "relay"

    @property
    def use_priorities(self) -> bool:
        return self.variant == "relay"

    @property
    def use_duplicate_suppression(self) -> bool:
        return True  # on in both variants (C4 4.1, decision D3)

    # --- helpers --------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable copy of every parameter, for the run manifest."""
        data = asdict(self)
        data["jitter_ms"] = list(self.jitter_ms)
        return data

    def replace(self, **overrides: Any) -> "NodeConfig":
        """Return a validated copy with ``overrides`` applied."""
        known = {f.name for f in fields(self)}
        unknown = set(overrides) - known
        if unknown:
            raise ConfigError(f"unknown key(s): {sorted(unknown)}")
        if "jitter_ms" in overrides:
            overrides["jitter_ms"] = _as_jitter(overrides["jitter_ms"], "jitter_ms")
        return replace(self, **overrides)


def _as_jitter(value: Any, key: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ConfigError(f"{key} must be a two-element array [min, max]")
    lo, hi = value
    for item in (lo, hi):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ConfigError(f"{key} entries must be numbers")
    if lo < 0:
        raise ConfigError(f"{key} minimum must be non-negative")
    if lo > hi:
        raise ConfigError(f"{key} minimum must not exceed maximum")
    return (lo, hi)


def _check_number(key: str, value: Any, *, integer: bool) -> None:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number, not a boolean")
    if integer:
        if not isinstance(value, int):
            raise ConfigError(f"{key} must be an integer")
    elif not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number")


def _validate_node(cfg: NodeConfig) -> None:
    if cfg.variant not in VARIANTS:
        raise ConfigError(f"variant must be one of {list(VARIANTS)}, got {cfg.variant!r}")

    for key, (lo, hi) in _NUMERIC_BOUNDS.items():
        value = getattr(cfg, key)
        _check_number(key, value, integer=key in _INT_KEYS)
        if value < lo:
            raise ConfigError(f"{key} must be at least {lo}, got {value}")
        if hi is not None and value > hi:
            raise ConfigError(f"{key} must be at most {hi}, got {value}")
        if key in _STRICTLY_POSITIVE and value <= 0:
            raise ConfigError(f"{key} must be greater than zero, got {value}")

    _check_number("default_ttl", cfg.default_ttl, integer=True)
    if not 1 <= cfg.default_ttl <= _TTL_MAX:
        raise ConfigError(f"default_ttl must be between 1 and {_TTL_MAX}, got {cfg.default_ttl}")

    _as_jitter(cfg.jitter_ms, "jitter_ms")

    if cfg.sos_repeat_interval_s <= cfg.rate_limit_sos_origin_s:
        raise ConfigError(
            "sos_repeat_interval_s must be greater than rate_limit_sos_origin_s "
            f"({cfg.sos_repeat_interval_s} <= {cfg.rate_limit_sos_origin_s})"
        )


def _load_toml(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def load_node_config(path: str | Path) -> NodeConfig:
    """Read a node TOML file. Missing keys take defaults; unknown keys are errors."""
    raw = _load_toml(path)
    known = {f.name for f in fields(NodeConfig)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown key(s) in {path}: {sorted(unknown)}")
    kwargs = dict(raw)
    if "jitter_ms" in kwargs:
        kwargs["jitter_ms"] = _as_jitter(kwargs["jitter_ms"], "jitter_ms")
    return NodeConfig(**kwargs)


# --- Scenario (C4 section 4.2) ------------------------------------------------

@dataclass(frozen=True)
class RunSection:
    name: str
    seed: int
    duration_s: float


@dataclass(frozen=True)
class AreaSection:
    width_m: float
    height_m: float


@dataclass(frozen=True)
class NodesSection:
    count: int
    relay_count: int
    handheld_range_m: float
    relay_range_m: float


@dataclass(frozen=True)
class MobilitySection:
    model: str
    speed_min_mps: float
    speed_max_mps: float


@dataclass(frozen=True)
class LossSection:
    receiver_loss_probability: float
    per_hop_delay_ms: float


@dataclass(frozen=True)
class WorkloadSection:
    texts: int
    text_min_separation_m: float
    sos_beacons: int


@dataclass(frozen=True)
class ContactsSection:
    verified_pairs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Scenario:
    run: RunSection
    area: AreaSection
    nodes: NodesSection
    mobility: MobilitySection
    loss: LossSection
    workload: WorkloadSection
    contacts: ContactsSection


_SCENARIO_SECTIONS: dict[str, tuple[type, dict[str, type]]] = {
    "run": (RunSection, {"name": str, "seed": int, "duration_s": float}),
    "area": (AreaSection, {"width_m": float, "height_m": float}),
    "nodes": (
        NodesSection,
        {"count": int, "relay_count": int, "handheld_range_m": float, "relay_range_m": float},
    ),
    "mobility": (MobilitySection, {"model": str, "speed_min_mps": float, "speed_max_mps": float}),
    "loss": (LossSection, {"receiver_loss_probability": float, "per_hop_delay_ms": float}),
    "workload": (WorkloadSection, {"texts": int, "text_min_separation_m": float, "sos_beacons": int}),
    "contacts": (ContactsSection, {"verified_pairs": list}),
}


def _check_section_values(section: str, values: dict[str, Any]) -> None:
    if section == "run":
        if not values["name"]:
            raise ConfigError("run.name must be non-empty")
        if values["seed"] < 0 or values["seed"] >= 2**64:
            raise ConfigError("run.seed must be in [0, 2**64)")
        if values["duration_s"] <= 0:
            raise ConfigError("run.duration_s must be greater than zero")
    elif section == "area":
        if values["width_m"] <= 0:
            raise ConfigError("area.width_m must be greater than zero")
        if values["height_m"] <= 0:
            raise ConfigError("area.height_m must be greater than zero")
    elif section == "nodes":
        if values["count"] < 0:
            raise ConfigError("nodes.count must be non-negative")
        if not values["count"] >= values["relay_count"] >= 0:
            raise ConfigError("nodes.count must be >= nodes.relay_count >= 0")
        if values["handheld_range_m"] <= 0:
            raise ConfigError("nodes.handheld_range_m must be greater than zero")
        if values["relay_range_m"] <= 0:
            raise ConfigError("nodes.relay_range_m must be greater than zero")
    elif section == "mobility":
        if not values["model"]:
            raise ConfigError("mobility.model must be non-empty")
        if values["speed_min_mps"] < 0:
            raise ConfigError("mobility.speed_min_mps must be non-negative")
        if values["speed_max_mps"] < values["speed_min_mps"]:
            raise ConfigError("mobility.speed_max_mps must be at least speed_min_mps")
    elif section == "loss":
        p = values["receiver_loss_probability"]
        if not 0.0 <= p <= 1.0:
            raise ConfigError("loss.receiver_loss_probability must be in [0, 1]")
        if values["per_hop_delay_ms"] < 0:
            raise ConfigError("loss.per_hop_delay_ms must be non-negative")
    elif section == "workload":
        if values["texts"] < 0:
            raise ConfigError("workload.texts must be non-negative")
        if values["text_min_separation_m"] < 0:
            raise ConfigError("workload.text_min_separation_m must be non-negative")
        if values["sos_beacons"] < 0:
            raise ConfigError("workload.sos_beacons must be non-negative")


def _parse_verified_pairs(raw: Any) -> tuple[tuple[str, str], ...]:
    # Contract question 1: list of two-element lists of distinct node names.
    if not isinstance(raw, list):
        raise ConfigError("contacts.verified_pairs must be a list")
    pairs: list[tuple[str, str]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, list) or len(item) != 2:
            raise ConfigError(f"contacts.verified_pairs[{i}] must be a two-element list")
        a, b = item
        if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
            raise ConfigError(f"contacts.verified_pairs[{i}] must hold two non-empty node names")
        if a == b:
            raise ConfigError(f"contacts.verified_pairs[{i}] must name two distinct nodes")
        pairs.append((a, b))
    return tuple(pairs)


def load_scenario(path: str | Path) -> Scenario:
    """Read and validate a scenario TOML file. No placement or mobility is generated."""
    raw = _load_toml(path)
    known = set(_SCENARIO_SECTIONS)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown section(s) in {path}: {sorted(unknown)}")

    built: dict[str, Any] = {}
    for section, (cls, schema) in _SCENARIO_SECTIONS.items():
        if section not in raw:
            raise ConfigError(f"missing section [{section}] in {path}")
        values = raw[section]
        if not isinstance(values, dict):
            raise ConfigError(f"[{section}] must be a table")
        unknown_keys = set(values) - set(schema)
        if unknown_keys:
            raise ConfigError(f"unknown key(s) in [{section}]: {sorted(unknown_keys)}")
        missing = set(schema) - set(values)
        if missing:
            raise ConfigError(f"missing key(s) in [{section}]: {sorted(missing)}")
        for key, expected in schema.items():
            value = values[key]
            if expected is float:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ConfigError(f"{section}.{key} must be a number")
                values[key] = float(value)
            elif expected is int:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ConfigError(f"{section}.{key} must be an integer")
            elif expected is str:
                if not isinstance(value, str):
                    raise ConfigError(f"{section}.{key} must be a string")
        if section == "contacts":
            built[section] = cls(verified_pairs=_parse_verified_pairs(values["verified_pairs"]))
            continue
        _check_section_values(section, values)
        built[section] = cls(**{k: values[k] for k in schema})

    nodes: NodesSection = built["nodes"]
    _ = nodes  # relay_count <= count is checked in _check_section_values
    return Scenario(**built)