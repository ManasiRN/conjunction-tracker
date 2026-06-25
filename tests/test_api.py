"""API tests using FastAPI's TestClient (no network: startup fetch disabled)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.database import Database
from app.parser import parse_and_filter

SAMPLE = Path(__file__).parent / "data" / "socrates_sample.csv"
MONITORED = {65320, 65322, 65319}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Disable startup fetch so the tests never touch the network.
    settings = Settings(
        fetch_on_startup=False,
        database_path=str(tmp_path / "api.db"),
        satellite_config_path="./config/satellites.yaml",
    )
    app = create_app(settings)
    # Pre-load data straight into the same DB file the app uses.
    db = Database(settings.database_path)
    db.upsert_conjunctions(parse_and_filter(SAMPLE.read_text(encoding="utf-8"), MONITORED))
    db.close()
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body["monitored_satellite_ids"]) == MONITORED
    assert body["database"]["total_conjunctions"] > 0


def test_list_conjunctions_default_upcoming(client):
    r = client.get("/conjunctions")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] <= body["total"]
    for c in body["results"]:
        assert c["satellite"]["is_monitored"] is True


def test_sort_by_miss_distance_ascending(client):
    r = client.get("/conjunctions", params={"time_filter": "all", "order_by": "miss"})
    misses = [c["miss_distance_km"] for c in r.json()["results"]]
    assert misses == sorted(misses)


def test_satellite_404_when_not_monitored(client):
    assert client.get("/satellites/99999/conjunctions").status_code == 404


def test_conjunction_detail_has_observations(client):
    first = client.get("/conjunctions", params={"time_filter": "all"}).json()["results"][0]
    d = client.get(f"/conjunctions/{first['id']}").json()
    assert d["id"] == first["id"]
    assert len(d["observations"]) >= 1


def test_min_risk_filters_before_paging(client):
    # A small page must not hide higher-risk events that sort later, and every
    # returned row must meet the floor. (Regression: min_risk was applied AFTER
    # LIMIT/OFFSET, so RED events on later pages were invisible.)
    full = client.get(
        "/conjunctions", params={"min_risk": "red", "time_filter": "all", "limit": 1000}
    ).json()
    paged = client.get(
        "/conjunctions", params={"min_risk": "red", "time_filter": "all", "limit": 2}
    ).json()
    # total is the count of matching (RED) events, not all events, and is stable.
    assert paged["total"] == full["total"] == full["count"]
    assert paged["count"] <= 2
    for c in paged["results"]:
        assert c["risk"]["level"] == "red"


def test_total_reflects_max_miss_filter(client):
    body = client.get(
        "/conjunctions", params={"time_filter": "all", "max_miss_km": 1.0, "limit": 1}
    ).json()
    everything = client.get("/conjunctions", params={"time_filter": "all", "limit": 1}).json()
    # total must shrink to the filtered set, not report the whole table.
    assert body["total"] <= everything["total"]
    assert body["total"] >= body["count"]


def test_conjunctions_include_trend(client):
    body = client.get("/conjunctions", params={"time_filter": "all"}).json()
    for c in body["results"]:
        assert "trend" in c
        assert c["trend"]["direction"] in {"worsening", "improving", "stable", "new"}
        # Sample data has one screening per event => trend is "new".
        assert c["trend"]["screenings"] >= 1


def test_health_reports_freshness_fields(client):
    body = client.get("/health").json()
    assert "data_fresh" in body
    assert "data_age_limit_seconds" in body
    # The test fixture never fetches, so there is no successful contact => stale.
    assert body["data_fresh"] is False
    assert body["status"] == "degraded"


def test_threats_groups_by_object(client):
    body = client.get("/threats", params={"time_filter": "all"}).json()
    assert "fleet_threat_count" in body
    for g in body["threats"]:
        # satellite_count must match the distinct list, and the threat object
        # must not be one of the satellites it is grouped against.
        assert g["satellite_count"] == len(set(g["satellites_threatened"]))
        assert g["threat"]["norad_id"] not in g["satellites_threatened"]
        assert len(g["conjunctions"]) >= 1
    # Sorted: most satellites threatened first.
    counts = [g["satellite_count"] for g in body["threats"]]
    assert counts == sorted(counts, reverse=True)


def test_threats_min_satellites_filter(client):
    fleet = client.get("/threats", params={"time_filter": "all", "min_satellites": 2}).json()
    for g in fleet["threats"]:
        assert g["satellite_count"] >= 2


def test_openapi_docs_served(client):
    assert client.get("/openapi.json").status_code == 200
