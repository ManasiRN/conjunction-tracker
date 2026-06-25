"""Tests for the SQLite layer: dedup/upsert and observation history."""

from pathlib import Path

import pytest

from app.database import Database
from app.parser import parse_and_filter

SAMPLE = Path(__file__).parent / "data" / "socrates_sample.csv"
MONITORED = {65320, 65322, 65319}


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


@pytest.fixture
def items():
    return parse_and_filter(SAMPLE.read_text(encoding="utf-8"), MONITORED)


def test_first_ingest_inserts(db, items):
    new, updated = db.upsert_conjunctions(items)
    assert new == len(items)
    assert updated == 0


def test_reingest_updates_not_duplicates(db, items):
    db.upsert_conjunctions(items)
    new, updated = db.upsert_conjunctions(items)
    assert new == 0
    assert updated == len(items)
    # Total stored rows must not grow on re-ingest.
    assert db.stats()["total_conjunctions"] == len(items)


def test_observations_accumulate_per_screening(db, items):
    db.upsert_conjunctions(items)
    db.upsert_conjunctions(items)
    db.upsert_conjunctions(items)
    stats = db.stats()
    # Three screenings => 3 observations per conjunction.
    assert stats["total_observations"] == 3 * len(items)
    # screening_count on the row reflects the same.
    any_id = items[0].conjunction_id
    row = db.get_conjunction(any_id)
    assert row["screening_count"] == 3
    assert len(db.get_observations(any_id)) == 3


def test_filter_by_satellite_matches_either_column(db, items):
    db.upsert_conjunctions(items)
    rows = db.query_conjunctions(norad_id=65322, time_filter="all", limit=1000)
    for r in rows:
        assert 65322 in (r["norad_id_1"], r["norad_id_2"])


def test_upcoming_vs_historical_partition(db, items):
    db.upsert_conjunctions(items)
    total = db.count_conjunctions(time_filter="all")
    up = db.count_conjunctions(time_filter="upcoming")
    hist = db.count_conjunctions(time_filter="historical")
    assert up + hist == total


def test_trend_summary_captures_first_and_last_screening(db, items):
    import dataclasses

    # Screening 1: original values.
    db.upsert_conjunctions(items, observed_at="2026-06-20T00:00:00+00:00")
    # Screening 2: same events, but the miss distance has halved (got closer).
    closer = [dataclasses.replace(c, miss_distance_km=c.miss_distance_km / 2) for c in items]
    db.upsert_conjunctions(closer, observed_at="2026-06-20T06:00:00+00:00")

    cid = items[0].conjunction_id
    summary = db.get_trend_summaries([cid])[cid]
    assert summary["screenings"] == 2
    assert summary["first_miss_km"] == items[0].miss_distance_km
    assert summary["last_miss_km"] == items[0].miss_distance_km / 2


def test_trend_summary_empty_for_unknown_ids(db, items):
    db.upsert_conjunctions(items)
    assert db.get_trend_summaries([]) == {}
    assert db.get_trend_summaries(["does-not-exist"]) == {}
