import pytest

from lifeline.logschema import assert_valid_log, validate_file, validate_line

MID = "ab" * 8
NID = "cd" * 16


def ok(**fields):
    base = {"t": 0.0, "node": "n01", "event": fields.pop("event")}
    base.update(fields)
    return base


def test_valid_examples_pass():
    assert validate_line(ok(event="TX", mid=MID, type="TEXT", sender=NID, ttl=3, size=10)) == []
    assert validate_line(ok(event="DROP", reason="REPLAY")) == []
    assert validate_line(ok(event="SOS_STATE", mid=MID, status="cancel")) == []


@pytest.mark.parametrize(
    "obj, expected",
    [
        ("not a dict", "not a JSON object"),
        ({"node": "n01", "event": "ACK_RX", "mid": MID}, "missing common field 't'"),
        ({"t": 0, "event": "ACK_RX", "mid": MID}, "missing common field 'node'"),
        ({"t": 0, "node": "n01", "mid": MID}, "missing common field 'event'"),
        ({"t": "zero", "node": "n01", "event": "ACK_RX", "mid": MID}, "t: must be a number"),
        ({"t": 0, "node": "", "event": "ACK_RX", "mid": MID}, "node: must be a non-empty string"),
    ],
)
def test_common_field_failures(obj, expected):
    assert any(expected in e for e in validate_line(obj))


def test_unknown_event_fails():
    assert any("unknown event" in e for e in validate_line(ok(event="WHATEVER")))


def test_missing_required_field_fails():
    errors = validate_line(ok(event="TX", mid=MID, type="TEXT", sender=NID, ttl=3))
    assert any("missing required field 'size'" in e for e in errors)


def test_extra_field_fails():
    errors = validate_line(ok(event="ACK_RX", mid=MID, surprise=1))
    assert any("extra field 'surprise'" in e for e in errors)


@pytest.mark.parametrize("bad_mid", ["AB" * 8, "ab" * 7, "zz" * 8, "ab" * 9, 123])
def test_mid_must_be_16_lowercase_hex(bad_mid):
    errors = validate_line(ok(event="ACK_RX", mid=bad_mid))
    assert any("mid must be 16 lowercase hex" in e for e in errors)


@pytest.mark.parametrize("bad_nid", ["CD" * 16, "cd" * 15, "cd" * 17, "g" * 32])
def test_node_id_must_be_32_lowercase_hex(bad_nid):
    errors = validate_line(ok(event="NEIGHBOR_UP", peer=bad_nid))
    assert any("NodeID must be 32 lowercase hex" in e for e in errors)


@pytest.mark.parametrize("field", ["ttl", "size"])
@pytest.mark.parametrize("bad", [-1, 1.5, "3", True])
def test_nonneg_int_rule_for_tx_fields(field, bad):
    fields = {"mid": MID, "type": "TEXT", "sender": NID, "ttl": 1, "size": 1}
    fields[field] = bad
    errors = validate_line(ok(event="TX", **fields))
    assert any(f"TX.{field}: must be a non-negative integer" in e for e in errors)


def test_attempt_and_buffer_size_are_non_negative_integers():
    assert any(
        "REOFFER.attempt" in e
        for e in validate_line(ok(event="REOFFER", mid=MID, attempt=-1, trigger="timer"))
    )
    assert any(
        "BUF_PUT.buffer_size" in e
        for e in validate_line(ok(event="BUF_PUT", mid=MID, type="TEXT", buffer_size=2.0))
    )


@pytest.mark.parametrize("reason", ["bad_sig", "BAD", "", "malformed"])
def test_drop_reason_is_closed_set(reason):
    errors = validate_line(ok(event="DROP", reason=reason))
    assert any("DROP.reason" in e for e in errors)


def test_text_end_outcome_closed_set():
    assert validate_line(ok(event="TEXT_END", mid=MID, outcome="delivered")) == []
    assert any(
        "TEXT_END.outcome" in e
        for e in validate_line(ok(event="TEXT_END", mid=MID, outcome="lost"))
    )


def test_reoffer_trigger_closed_set():
    assert any(
        "REOFFER.trigger" in e
        for e in validate_line(ok(event="REOFFER", mid=MID, attempt=1, trigger="manual"))
    )


def test_key_learned_via_and_trust_closed_sets():
    assert any(
        "KEY_LEARNED.via" in e
        for e in validate_line(ok(event="KEY_LEARNED", peer=NID, via="dns", trust="verified"))
    )
    assert any(
        "KEY_LEARNED.trust" in e
        for e in validate_line(ok(event="KEY_LEARNED", peer=NID, via="qr", trust="maybe"))
    )


def test_type_must_be_uppercase_message_type():
    assert any(
        "type must be one of" in e
        for e in validate_line(ok(event="BUF_PUT", mid=MID, type="text", buffer_size=1))
    )


def test_sos_status_must_be_active_or_cancel():
    assert any(
        "status must be one of" in e
        for e in validate_line(ok(event="SOS_STATE", mid=MID, status="ACTIVE"))
    )


def test_validate_file_reports_line_numbers(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(
        '{"t":0,"node":"n01","event":"ACK_RX","mid":"' + MID + '"}\n'
        "not json\n"
        '{"t":0,"node":"n01","event":"DROP","reason":"nope"}\n'
    )
    failures = validate_file(path)
    assert [n for n, _ in failures] == [2, 3]
    assert "invalid JSON" in failures[0][1][0]


def test_validate_file_ignores_blank_lines(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text('{"t":0,"node":"n01","event":"ACK_RX","mid":"' + MID + '"}\n\n')
    assert validate_file(path) == []


def test_assert_valid_log_raises_listing_bad_lines():
    with pytest.raises(AssertionError, match="line 1"):
        assert_valid_log(['{"t":0,"node":"n01","event":"DROP","reason":"x"}'])


def test_assert_valid_log_accepts_good_lines():
    assert_valid_log(['{"t":0,"node":"n01","event":"ACK_RX","mid":"' + MID + '"}'])