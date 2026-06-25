"""Unit tests for the Flight-Dynamics risk classifier (pure, no network)."""

from datetime import datetime, timedelta, timezone

from app.risk import RISK_RANK, RiskThresholds, assess, assess_trend

NOW = datetime(2026, 6, 20, 0, 0, tzinfo=timezone.utc)
T = RiskThresholds()  # defaults: pc_red=1e-4, pc_yellow=1e-7, miss_red=1, miss_yellow=5


def _assess(pc, miss, *, tca_hours=48.0, dse=0.0):
    return assess(
        max_probability=pc,
        miss_distance_km=miss,
        tca=NOW + timedelta(hours=tca_hours),
        now=NOW,
        data_age_days=dse,
        thresholds=T,
    )


def test_high_pc_is_red():
    assert _assess(2e-4, miss=10.0).level == "red"


def test_mid_pc_is_yellow():
    assert _assess(1e-6, miss=10.0).level == "yellow"


def test_low_pc_far_miss_is_green():
    a = _assess(1e-9, miss=20.0)
    assert a.level == "green"
    assert a.label == "CLEAR"


def test_close_miss_escalates_even_with_tiny_pc():
    # SOCRATES Pc optimistic but geometry is scary -> backstop fires.
    a = _assess(1e-12, miss=0.5)
    assert a.level == "red"
    assert any("miss" in d for d in a.drivers)


def test_overall_is_worse_of_the_two():
    # yellow Pc + red miss -> red
    assert _assess(1e-6, miss=0.4).level == "red"


def test_stale_data_lowers_confidence():
    a = _assess(2e-4, miss=0.5, dse=7.0)
    assert a.confidence == "low"
    assert a.is_stale is True


def test_fresh_data_is_high_confidence():
    assert _assess(2e-4, miss=0.5, dse=0.5).confidence == "high"


def test_missing_pc_does_not_crash_and_uses_miss_only():
    a = _assess(None, miss=0.5)
    assert a.level == "red"  # from miss backstop


def test_decision_by_is_lead_hours_before_tca():
    a = _assess(2e-4, miss=0.5, tca_hours=48.0)
    decision = datetime.fromisoformat(a.decision_by_utc)
    expected = NOW + timedelta(hours=48) - timedelta(hours=T.lead_time_hours)
    assert decision == expected


def test_past_event_has_no_decision_window():
    a = _assess(2e-4, miss=0.5, tca_hours=-5.0)
    assert a.decision_by_utc is None


def test_red_outranks_yellow_outranks_green_in_score():
    red = _assess(2e-4, miss=0.5)
    yellow = _assess(1e-6, miss=10.0)
    green = _assess(1e-12, miss=50.0)
    assert red.score > yellow.score > green.score
    assert RISK_RANK[red.level] > RISK_RANK[yellow.level] > RISK_RANK[green.level]


def test_sooner_tca_scores_higher_within_same_level():
    soon = _assess(1e-6, miss=10.0, tca_hours=6.0)
    later = _assess(1e-6, miss=10.0, tca_hours=120.0)
    assert soon.level == later.level == "yellow"
    assert soon.score > later.score


# --- trend ------------------------------------------------------------------


def test_trend_single_screening_is_new():
    t = assess_trend(screenings=1, first_miss_km=5.0, last_miss_km=5.0)
    assert t.direction == "new"


def test_trend_shrinking_miss_is_worsening():
    # 5 km -> 2 km between first and latest screening: getting closer.
    t = assess_trend(screenings=4, first_miss_km=5.0, last_miss_km=2.0)
    assert t.direction == "worsening"
    assert t.miss_delta_km == -3.0


def test_trend_opening_miss_is_improving():
    t = assess_trend(screenings=4, first_miss_km=2.0, last_miss_km=5.0)
    assert t.direction == "improving"


def test_trend_tiny_change_is_stable():
    # <5% relative change should not flip the verdict (screening noise).
    t = assess_trend(screenings=3, first_miss_km=10.0, last_miss_km=10.2)
    assert t.direction == "stable"


def test_trend_rising_pc_is_worsening_even_if_miss_flat():
    t = assess_trend(
        screenings=3, first_miss_km=10.0, last_miss_km=10.0,
        first_pc=1e-6, last_pc=1e-4,
    )
    assert t.direction == "worsening"
    assert t.pc_ratio == 100.0


def test_trend_handles_missing_metrics_gracefully():
    t = assess_trend(screenings=3)
    assert t.direction in {"stable", "worsening", "improving"}
