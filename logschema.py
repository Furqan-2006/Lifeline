"""C3 event log schema and validators.

Value formats below are proposals awaiting agreement with M2 (who reads the
logs) and M1. Changing a format is a one-line edit to the constants here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# --- Value formats (contract question 1) -----------------------------------

#: Message IDs are 8 bytes (C1.1), rendered as 16 lowercase hex characters.
MID_HEX_LEN = 16
#: NodeIDs are 16 bytes (C1.1), rendered as 32 lowercase hex characters.
NODE_ID_HEX_LEN = 32
#: Message type names as written in the ``type`` field.
MESSAGE_TYPES = frozenset({"HELLO", "TEXT", "SOS", "ACK", "KEY_REQUEST", "KEY_REPLY"})
#: SOS_STATE ``status`` values.
SOS_STATUSES = frozenset({"active", "cancel"})

# --- Closed value sets from C3 ----------------------------------------------

DROP_REASONS = frozenset({"MALFORMED", "RATE_LIMIT", "DUPLICATE", "BAD_SIG", "REPLAY", "TTL"})
TEXT_END_OUTCOMES = frozenset({"delivered", "expired"})
REOFFER_TRIGGERS = frozenset({"timer", "encounter"})
KEY_VIA = frozenset({"hello", "reply", "qr"})
KEY_TRUST = frozenset({"verified", "unverified"})

# --- Field kinds --------------------------------------------------------------

MID = "mid"                  # 16 lowercase hex characters
NODE_ID = "node_id"          # 32 lowercase hex characters
NONNEG_INT = "nonneg_int"
NUMBER = "number"            # float or int, used for ``t``
STRING = "string"            # node name, no format check beyond non-empty
TYPE = "type"                # one of MESSAGE_TYPES
SOS_STATUS = "sos_status"    # one of SOS_STATUSES
ENUM_PREFIX = "enum:"        # e.g. "enum:DROP_REASONS"

_ENUMS: dict[str, frozenset[str]] = {
    "DROP_REASONS": DROP_REASONS,
    "TEXT_END_OUTCOMES": TEXT_END_OUTCOMES,
    "REOFFER_TRIGGERS": REOFFER_TRIGGERS,
    "KEY_VIA": KEY_VIA,
    "KEY_TRUST": KEY_TRUST,
}

# --- The C3.2 table -----------------------------------------------------------
# event -> (required fields, optional fields); each field maps to its kind.

_TX = {"mid": MID, "type": TYPE, "sender": NODE_ID, "ttl": NONNEG_INT, "size": NONNEG_INT}

EVENTS: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "TX": (_TX, {}),
    "RX": (
        {**_TX, "tx": STRING},
        {},
    ),
    "DROP": (
        {"reason": f"{ENUM_PREFIX}DROP_REASONS"},
        {"mid": MID},
    ),
    "DELIVER": (
        {"mid": MID, "type": TYPE, "sender": NODE_ID},
        {},
    ),
    "TEXT_SEND": (
        {"mid": MID, "recipient": NODE_ID, "size": NONNEG_INT},
        {},
    ),
    "ACK_RX": ({"mid": MID}, {}),
    "TEXT_END": (
        {"mid": MID, "outcome": f"{ENUM_PREFIX}TEXT_END_OUTCOMES"},
        {},
    ),
    "BUF_PUT": (
        {"mid": MID, "type": TYPE, "buffer_size": NONNEG_INT},
        {},
    ),
    "BUF_EVICT": (
        {"mid": MID, "type": TYPE, "buffer_size": NONNEG_INT},
        {},
    ),
    "REOFFER": (
        {"mid": MID, "attempt": NONNEG_INT, "trigger": f"{ENUM_PREFIX}REOFFER_TRIGGERS"},
        {},
    ),
    "NEIGHBOR_UP": ({"peer": NODE_ID}, {}),
    "NEIGHBOR_DOWN": ({"peer": NODE_ID}, {}),
    "KEY_LEARNED": (
        {
            "peer": NODE_ID,
            "via": f"{ENUM_PREFIX}KEY_VIA",
            "trust": f"{ENUM_PREFIX}KEY_TRUST",
        },
        {},
    ),
    "SOS_STATE": (
        {"mid": MID, "status": SOS_STATUS},
        {},
    ),
}

COMMON_FIELDS = {"t": NUMBER, "node": STRING, "event": STRING}


# --- Validation ----------------------------------------------------------------

_HEX = frozenset("0123456789abcdef")


def _is_lower_hex(value: Any, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(c in _HEX for c in value)
    )


def _check_value(kind: str, value: Any) -> str | None:
    """Return an error description, or None if the value matches ``kind``."""
    if kind == MID:
        return None if _is_lower_hex(value, MID_HEX_LEN) else f"mid must be {MID_HEX_LEN} lowercase hex chars"
    if kind == NODE_ID:
        return None if _is_lower_hex(value, NODE_ID_HEX_LEN) else f"NodeID must be {NODE_ID_HEX_LEN} lowercase hex chars"
    if kind == NONNEG_INT:
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        return None if ok else "must be a non-negative integer"
    if kind == NUMBER:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return None if ok else "must be a number"
    if kind == STRING:
        return None if isinstance(value, str) and value else "must be a non-empty string"
    if kind == TYPE:
        return None if value in MESSAGE_TYPES else f"type must be one of {sorted(MESSAGE_TYPES)}"
    if kind == SOS_STATUS:
        return None if value in SOS_STATUSES else f"status must be one of {sorted(SOS_STATUSES)}"
    if kind.startswith(ENUM_PREFIX):
        allowed = _ENUMS[kind[len(ENUM_PREFIX):]]
        return None if value in allowed else f"must be one of {sorted(allowed)}"
    raise AssertionError(f"unknown field kind {kind}")  # schema bug, not data


def validate_line(obj: Any) -> list[str]:
    """Return a list of error strings; an empty list means the line is valid."""
    if not isinstance(obj, dict):
        return ["line is not a JSON object"]
    errors: list[str] = []

    for name, kind in COMMON_FIELDS.items():
        if name not in obj:
            errors.append(f"missing common field '{name}'")
        else:
            problem = _check_value(kind, obj[name])
            if problem:
                errors.append(f"{name}: {problem}")

    event = obj.get("event")
    if not isinstance(event, str) or event not in EVENTS:
        errors.append(f"unknown event {event!r}")
        return errors

    required, optional = EVENTS[event]
    allowed = set(COMMON_FIELDS) | set(required) | set(optional)

    for name, kind in required.items():
        if name not in obj:
            errors.append(f"{event}: missing required field '{name}'")
        else:
            problem = _check_value(kind, obj[name])
            if problem:
                errors.append(f"{event}.{name}: {problem}")

    for name, kind in optional.items():
        if name in obj:
            problem = _check_value(kind, obj[name])
            if problem:
                errors.append(f"{event}.{name}: {problem}")

    for name in obj:
        if name not in allowed:
            errors.append(f"{event}: extra field '{name}'")

    return errors


def _iter_lines(source: str | Path | Iterable[str]) -> Iterable[tuple[int, str]]:
    if isinstance(source, (str, Path)):
        with Path(source).open("r", encoding="utf-8") as fh:
            yield from enumerate(fh, start=1)
    else:
        yield from enumerate(source, start=1)


def validate_file(path: str | Path) -> list[tuple[int, list[str]]]:
    """Validate every line of a JSONL file. Returns (line_number, errors) for bad lines."""
    return _validate_lines(_iter_lines(path))


def _validate_lines(lines: Iterable[tuple[int, str]]) -> list[tuple[int, list[str]]]:
    failures: list[tuple[int, list[str]]] = []
    for number, raw in lines:
        raw = raw.rstrip("\r\n")
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            failures.append((number, [f"invalid JSON: {exc.msg}"]))
            continue
        errors = validate_line(obj)
        if errors:
            failures.append((number, errors))
    return failures


def assert_valid_log(path_or_lines: str | Path | Iterable[str]) -> None:
    """Raise AssertionError listing every bad line. For use in pytest."""
    if isinstance(path_or_lines, (str, Path)):
        failures = validate_file(path_or_lines)
    else:
        failures = _validate_lines(_iter_lines(path_or_lines))
    if failures:
        details = "\n".join(f"line {n}: {'; '.join(errs)}" for n, errs in failures[:20])
        raise AssertionError(f"{len(failures)} invalid line(s):\n{details}")