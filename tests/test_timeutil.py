from datetime import UTC, datetime

from energy_hub.timeutil import parse_iso_epoch


def test_utc_z_suffix():
    assert parse_iso_epoch("2026-04-12T12:00:00Z") == datetime(2026, 4, 12, 12, tzinfo=UTC).timestamp()


def test_explicit_offset():
    assert parse_iso_epoch("2026-04-12T14:00:00+02:00") == datetime(2026, 4, 12, 12, tzinfo=UTC).timestamp()


def test_naive_is_local_time():
    assert parse_iso_epoch("2026-04-12T14:30:00") == datetime(2026, 4, 12, 14, 30).timestamp()


def test_empty_none_garbage():
    assert parse_iso_epoch("") is None
    assert parse_iso_epoch(None) is None
    assert parse_iso_epoch("garbage") is None
    assert parse_iso_epoch(12345) is None
