"""Unit tests for the SOCRATES CSV parser/filter."""

from pathlib import Path

import pytest

from app.parser import parse_and_filter, parse_tca, _split_name_status

SAMPLE = Path(__file__).parent / "data" / "socrates_sample.csv"
MONITORED = {65320, 65322, 65319}


@pytest.fixture
def csv_text() -> str:
    return SAMPLE.read_text(encoding="utf-8")


def test_only_monitored_rows_kept(csv_text):
    items = parse_and_filter(csv_text, MONITORED)
    # Every result must involve at least one monitored satellite.
    assert items
    for c in items:
        assert c.norad_id_1 in MONITORED or c.norad_id_2 in MONITORED


def test_pair_is_normalised_low_id_first(csv_text):
    # Regardless of SOCRATES column order, id_1 < id_2 so the key is stable.
    for c in parse_and_filter(csv_text, MONITORED):
        assert c.norad_id_1 < c.norad_id_2


def test_conjunction_id_is_deterministic(csv_text):
    a = parse_and_filter(csv_text, MONITORED)
    b = parse_and_filter(csv_text, MONITORED)
    assert [c.conjunction_id for c in a] == [c.conjunction_id for c in b]


def test_name_and_status_split():
    assert _split_name_status("FFLY01 [+]") == ("FFLY01", "operational")
    assert _split_name_status("FENGYUN 1C DEB [-]") == ("FENGYUN 1C DEB", "nonoperational")
    assert _split_name_status("COSMOS 2228 [?]") == ("COSMOS 2228", "unknown")
    assert _split_name_status("NO BRACKETS") == ("NO BRACKETS", "unknown")


def test_tca_parsing_utc():
    dt = parse_tca("2026-06-24 23:42:14.024")
    assert dt.year == 2026 and dt.minute == 42
    assert dt.tzinfo is not None  # must be timezone-aware (UTC)
    # also parse without fractional seconds
    assert parse_tca("2026-06-24 23:42:14").second == 14


def test_bad_tca_raises():
    with pytest.raises(ValueError):
        parse_tca("not-a-date")


def test_empty_csv_returns_empty():
    assert parse_and_filter("", MONITORED) == []


def test_malformed_rows_are_skipped_not_fatal():
    text = (
        "NORAD_CAT_ID_1,OBJECT_NAME_1,DSE_1,NORAD_CAT_ID_2,OBJECT_NAME_2,DSE_2,"
        "TCA,TCA_RANGE,TCA_RELATIVE_SPEED,MAX_PROB,DILUTION\n"
        "garbage,row,with,too,few\n"
        "65320,FFLY01 [+],1.0,30716,FENGYUN 1C DEB [-],1.0,"
        "2026-06-20 12:00:00.000,2.5,14.0,1.0E-06,0.5\n"
    )
    items = parse_and_filter(text, MONITORED)
    assert len(items) == 1
    assert items[0].norad_id_1 == 30716  # normalised: lower id first
    assert items[0].norad_id_2 == 65320
