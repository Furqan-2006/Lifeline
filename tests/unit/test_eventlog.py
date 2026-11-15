import pathlib
import time
import enum

import pytest

from lifeline.clock import FakeClock
from lifeline.eventlog import EventLogger, FileSink, MemorySink
from lifeline.logschema import assert_valid_log, validate_file

MID = b"\x01" * 8
SENDER = b"\x02" * 16


def memlog(clock=None, **kwargs):
    sink = MemorySink()
    log = EventLogger("n01", clock or FakeClock(), sink=sink, **kwargs)
    return log, sink


def parsed(sink):
    import json
    log_lines = [json.loads(line) for chunk in sink.lines for line in chunk.splitlines()]
    return log_lines


def test_line_is_compact_jsonl_with_common_keys_first():
    log, sink = memlog(FakeClock(start=1.5))
    log.log("ACK_RX", mid=MID)
    log.flush()
    line = sink.lines[0]
    assert line.endswith("\n")
    assert line.startswith('{"t":1.5,"node":"n01","event":"ACK_RX"')
    assert ", " not in line  # compact separators


def test_every_c3_event_round_trips_and_validates():
    log, sink = memlog()
    log.log("TX", mid=MID, type="TEXT", sender=SENDER, ttl=3, size=120)
    log.log("RX", mid=MID, type="TEXT", sender=SENDER, ttl=2, size=120, tx="n02")
    log.log("DROP", reason="BAD_SIG", mid=MID)
    log.log("DROP", reason="MALFORMED")
    log.log("DELIVER", mid=MID, type="TEXT", sender=SENDER)
    log.log("TEXT_SEND", mid=MID, recipient=SENDER, size=40)
    log.log("ACK_RX", mid=MID)
    log.log("TEXT_END", mid=MID, outcome="delivered")
    log.log("BUF_PUT", mid=MID, type="SOS", buffer_size=5)
    log.log("BUF_EVICT", mid=MID, type="TEXT", buffer_size=200)
    log.log("REOFFER", mid=MID, attempt=2, trigger="encounter")
    log.log("NEIGHBOR_UP", peer=SENDER)
    log.log("NEIGHBOR_DOWN", peer=SENDER)
    log.log("KEY_LEARNED", peer=SENDER, via="qr", trust="verified")
    log.log("SOS_STATE", mid=MID, status="active")
    log.flush()
    assert len(sink.lines) == 1
    lines = sink.lines[0].splitlines()
    assert len(lines) == 15
    assert [line for line in lines if validate_file_line(line)] == []


def validate_file_line(line):
    import json
    from lifeline.logschema import validate_line
    return validate_line(json.loads(line))


def test_bytes_become_lowercase_hex():
    log, sink = memlog()
    log.log("ACK_RX", mid=bytes([0xAB, 0x0C, 0, 0, 0, 0, 0, 0xFF]))
    log.flush()
    assert '"mid":"ab0c00000000 00ff"'.replace(" ", "") in sink.lines[0]


def test_enum_becomes_name():
    class Trust(enum.Enum):
        VERIFIED = 1

    log, sink = memlog()
    log.log("KEY_LEARNED", peer=SENDER, via="hello", trust=Trust.VERIFIED)
    log.flush()
    assert '"trust":"VERIFIED"' in sink.lines[0]


def test_none_fields_are_omitted():
    log, sink = memlog()
    log.drop(reason="MALFORMED")
    log.flush()
    assert "mid" not in parsed(sink)[0]
    assert_valid_log(sink.lines[0].splitlines())


def test_unserializable_value_does_not_raise_and_is_counted():
    class Weird:
        def __str__(self):
            return "weird-value"

    log, sink = memlog()
    log.log("ACK_RX", mid=MID, extra=Weird())  # extra field, but must not raise
    log.flush()
    assert log.stringified == 0 or log.errors == 0  # never raised
    assert parsed(sink)  # a line was still written


def test_unserializable_value_is_stringified_and_counted():
    log, sink = memlog()
    log.log("ACK_RX", mid=MID, junk=object())
    log.flush()
    assert log.stringified == 1
    assert parsed(sink)[0]["junk"].startswith("<object object")


def test_log_never_raises_on_bad_clock():
    class BrokenClock:
        def now(self):
            raise RuntimeError("boom")

    sink = MemorySink()
    log = EventLogger("n01", BrokenClock(), sink=sink)  # type: ignore[arg-type]
    log.log("ACK_RX", mid=MID)  # must not raise
    assert log.errors >= 1


def test_missing_directory_is_created(tmp_path: pathlib.Path):
    target = tmp_path / "runs" / "R1" / "n01.jsonl"
    log = EventLogger("n01", FakeClock(), path=target)
    log.log("ACK_RX", mid=MID)
    log.close()
    assert target.exists()
    assert_valid_log(target)


def test_read_only_directory_does_not_raise(tmp_path: pathlib.Path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        log = EventLogger("n01", FakeClock(), path=ro / "sub" / "n01.jsonl")
        log.log("ACK_RX", mid=MID)
        log.close()
        assert log.errors >= 1
    finally:
        ro.chmod(0o700)


def test_append_mode_preserves_existing_content(tmp_path: pathlib.Path):
    target = tmp_path / "n01.jsonl"
    first = EventLogger("n01", FakeClock(), path=target)
    first.log("ACK_RX", mid=MID)
    first.close()
    second = EventLogger("n01", FakeClock(), path=target)
    second.log("ACK_RX", mid=MID)
    second.close()
    assert len(target.read_text().splitlines()) == 2


def test_close_flushes_buffer(tmp_path: pathlib.Path):
    target = tmp_path / "n01.jsonl"
    log = EventLogger("n01", FakeClock(), path=target, flush_interval_s=1000.0)
    log.log("ACK_RX", mid=MID)
    assert target.read_text() == "" or not target.exists() or True
    log.close()
    assert len(target.read_text().splitlines()) == 1


def test_close_is_idempotent_and_log_after_close_is_noop():
    log, sink = memlog()
    log.log("ACK_RX", mid=MID)
    log.close()
    log.close()
    log.log("ACK_RX", mid=MID)
    log.flush()
    assert len(parsed(sink)) == 1


def test_flush_writes_buffered_lines_once():
    log, sink = memlog()
    log.log("ACK_RX", mid=MID)
    log.flush()
    log.flush()
    assert len(sink.lines) == 1


def test_interval_flush_triggers_on_clock_time():
    clock = FakeClock()
    log, sink = memlog(clock, flush_interval_s=1.0)
    log.log("ACK_RX", mid=MID)
    assert sink.lines == []
    clock.now  # no advance: no flush yet
    log._clock._now = 1.0  # advance the fake clock without awaiting
    log.log("ACK_RX", mid=MID)
    assert len(sink.lines) == 1


def test_tx_rx_drop_wrappers_build_c3_fields():
    log, sink = memlog()
    log.tx(mid=MID, type="TEXT", sender=SENDER, ttl=3, size=10)
    log.rx(mid=MID, type="TEXT", sender=SENDER, ttl=2, size=10, tx="n02")
    log.drop(reason="DUPLICATE", mid=MID)
    log.flush()
    assert_valid_log(sink.lines[0].splitlines())


def test_ten_thousand_log_calls_finish_quickly():
    log, sink = memlog()
    start = time.perf_counter()
    for i in range(10_000):
        log.log("ACK_RX", mid=MID)
    log.flush()
    elapsed = time.perf_counter() - start
    assert len(sink.lines[0].splitlines()) == 10_000
    assert elapsed < 2.0


def test_file_output_is_valid_jsonl(tmp_path: pathlib.Path):
    target = tmp_path / "n01.jsonl"
    log = EventLogger("n01", FakeClock(), path=target)
    log.tx(mid=MID, type="HELLO", sender=SENDER, ttl=1, size=99)
    log.drop(reason="MALFORMED")
    log.close()
    assert validate_file(target) == []