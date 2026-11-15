"""Per-node JSONL event logger (contract C2.7 and C3).

Setup for a test, with no file::

    from lifeline.clock import FakeClock
    from lifeline.eventlog import EventLogger, MemorySink

    sink = MemorySink()
    log = EventLogger("n01", FakeClock(), sink=sink)
    log.log("TX", mid=b"\\x01" * 8, type="TEXT", sender=b"\\x02" * 16, ttl=3, size=120)
    log.flush()
    sink.lines  # list of JSON strings

Setup for a real run, one file per node::

    log = EventLogger("n07", SystemClock(), path="runs/R1/n07.jsonl")
"""

from __future__ import annotations

import enum
import json
import threading
from pathlib import Path
from typing import Any, Protocol

from clock import Clock

_COMMON_KEYS = ("t", "node", "event")
_TIME_DECIMALS = 6


class Sink(Protocol):
    def write(self, text: str) -> None: ...
    def close(self) -> None: ...


class MemorySink:
    """Keeps lines in memory; for tests and harness analysis."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)

    def close(self) -> None:
        pass


class FileSink:
    """Appends to a file, creating parent directories first."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")

    def write(self, text: str) -> None:
        self._fh.write(text)

    def close(self) -> None:
        self._fh.close()


def _normalize(value: Any) -> Any:
    """Convert a field value to something JSON can carry, following C3 conventions."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)) or value is None:
        return value
    return value  # left for json; unserializable values are handled in log()


class EventLogger:
    """Buffered JSONL writer. Never raises; never blocks the protocol.

    Writes are buffered in memory and flushed on ``flush()``, on ``close()``,
    and whenever ``flush_interval_s`` of clock time has passed since the last
    flush (checked on each ``log`` call, so no background task is needed).
    """

    def __init__(
        self,
        node_name: str,
        clock: Clock,
        path: str | Path | None = None,
        sink: Sink | None = None,
        flush_interval_s: float = 1.0,
    ) -> None:
        self.node_name = node_name
        self._clock = clock
        self._flush_interval_s = flush_interval_s
        self._buffer: list[str] = []
        self._lock = threading.Lock()
        self._closed = False
        self._last_flush = clock.now()
        self.errors = 0
        self.stringified = 0
        self._sink: Sink | None = None
        self._open_error: Exception | None = None
        try:
            if sink is not None:
                self._sink = sink
            elif path is not None:
                self._sink = FileSink(path)
            else:
                self._sink = MemorySink()
        except Exception as exc:  # setup failure must not crash the node
            self._open_error = exc
            self.errors += 1

    # --- public API ----------------------------------------------------------

    def log(self, event: str, **fields: Any) -> None:
        try:
            if self._closed or self._sink is None:
                return
            record: dict[str, Any] = {
                "t": round(self._clock.now(), _TIME_DECIMALS),
                "node": self.node_name,
                "event": event,
            }
            for key, value in fields.items():
                if value is None:
                    continue
                record[key] = _normalize(value)
            line = self._encode(record)
            with self._lock:
                self._buffer.append(line)
            if self._clock.now() - self._last_flush >= self._flush_interval_s:
                self.flush()
        except Exception:
            self.errors += 1

    def flush(self) -> None:
        try:
            with self._lock:
                if not self._buffer or self._sink is None:
                    self._last_flush = self._clock.now()
                    return
                text = "".join(self._buffer)
                self._buffer.clear()
            self._sink.write(text)
            self._last_flush = self._clock.now()
        except Exception:
            self.errors += 1

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True
        try:
            if self._sink is not None:
                self._sink.close()
        except Exception:
            self.errors += 1

    # --- helpers that build C3 field sets -------------------------------------

    def tx(self, *, mid: bytes, type: str, sender: bytes, ttl: int, size: int) -> None:
        self.log("TX", mid=mid, type=type, sender=sender, ttl=ttl, size=size)

    def rx(self, *, mid: bytes, type: str, sender: bytes, ttl: int, size: int, tx: str) -> None:
        self.log("RX", mid=mid, type=type, sender=sender, ttl=ttl, size=size, tx=tx)

    def drop(self, *, reason: str, mid: bytes | None = None) -> None:
        self.log("DROP", reason=reason, mid=mid)

    # --- internal -------------------------------------------------------------

    def _encode(self, record: dict[str, Any]) -> str:
        try:
            return json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        except (TypeError, ValueError):
            self.stringified += 1
            safe = {k: (v if _json_safe(v) else str(v)) for k, v in record.items()}
            return json.dumps(safe, separators=(",", ":"), ensure_ascii=False) + "\n"


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False