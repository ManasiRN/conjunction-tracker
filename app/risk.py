"""Flight-dynamics risk classification for conjunctions.

This is the layer that turns three raw SOCRATES numbers -- collision probability
(Pc), miss distance, and time-to-TCA -- plus data staleness into the single
thing a Flight Dynamics analyst actually needs: an **operational verdict**.

    RED    (ACTION)   -- treat as a real threat; plan a collision-avoidance
                         maneuver (COLA) and pull a precise CDM.
    YELLOW (MONITOR)  -- credible; track every screening and re-assess.
    GREEN  (CLEAR)    -- no action.

Method (follows common operational practice, e.g. NASA CARA):

* **Pc is the primary driver.** Pc >= 1e-4 is the classic "red" maneuver-decision
  threshold; 1e-7..1e-4 is "yellow".
* **Miss distance is an independent backstop.** SOCRATES' Pc is derived from
  *public TLEs*, not precise operational ephemeris, so it can be optimistic. A
  very close geometric miss therefore escalates the verdict on its own, even when
  the reported Pc looks small.
* The overall level is the **worse** of the two -- conservative, which is the
  right bias for an *early-warning* tool.
* **Staleness lowers confidence.** A scary number computed from a multi-day-old
  TLE is not trustworthy; we flag it so the analyst knows to get fresh data
  rather than react to noise.
* A maneuver needs lead time, so we also publish a **decision-by** deadline
  (TCA minus a configurable planning lead).

The functions here are pure (no I/O, no clock of their own -- ``now`` is passed
in) so they are trivially unit-testable and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Ordered worst-last so max() over rank gives the more severe level.
RISK_RANK = {"green": 0, "yellow": 1, "red": 2}
RISK_LABEL = {"red": "ACTION", "yellow": "MONITOR", "green": "CLEAR"}


@dataclass(frozen=True)
class RiskThresholds:
    """Tunable decision thresholds (sourced from Settings in production)."""

    pc_red: float = 1e-4
    pc_yellow: float = 1e-7
    miss_red_km: float = 1.0
    miss_yellow_km: float = 5.0
    stale_days: float = 3.0
    lead_time_hours: float = 24.0


@dataclass(frozen=True)
class RiskAssessment:
    """The verdict for one conjunction."""

    level: str  # "red" | "yellow" | "green"
    label: str  # "ACTION" | "MONITOR" | "CLEAR"
    score: float  # urgency for sorting; higher = act sooner
    drivers: list[str] = field(default_factory=list)  # why this level
    decision_by_utc: str | None = None  # last sensible maneuver-decision time
    confidence: str = "high"  # "high" | "low" (low => stale orbit data)
    is_stale: bool = False
    data_age_days: float | None = None  # max TLE age of the two objects, days


def _pc_level(pc: float | None, t: RiskThresholds) -> str:
    if pc is None:
        return "green"
    if pc >= t.pc_red:
        return "red"
    if pc >= t.pc_yellow:
        return "yellow"
    return "green"


def _miss_level(miss_km: float | None, t: RiskThresholds) -> str:
    if miss_km is None:
        return "green"
    if miss_km < t.miss_red_km:
        return "red"
    if miss_km < t.miss_yellow_km:
        return "yellow"
    return "green"


def _worse(a: str, b: str) -> str:
    return a if RISK_RANK[a] >= RISK_RANK[b] else b


def _urgency(hours_to_tca: float | None, horizon_h: float = 168.0) -> float:
    """0..1 within-level tiebreak: sooner TCA => closer to 1 (one-week horizon)."""
    if hours_to_tca is None:
        return 0.0
    if hours_to_tca <= 0:
        return 1.0
    return max(0.0, 1.0 - hours_to_tca / horizon_h)


def assess(
    *,
    max_probability: float | None,
    miss_distance_km: float | None,
    tca: datetime,
    now: datetime,
    data_age_days: float | None = None,
    thresholds: RiskThresholds | None = None,
) -> RiskAssessment:
    """Classify one conjunction into an operational verdict.

    ``tca``/``now`` are timezone-aware UTC datetimes; everything else is the
    SOCRATES-derived geometry. Returns a :class:`RiskAssessment`.
    """
    t = thresholds or RiskThresholds()

    pc_lvl = _pc_level(max_probability, t)
    miss_lvl = _miss_level(miss_distance_km, t)
    level = _worse(pc_lvl, miss_lvl)

    # Human-readable justification -- this is what an analyst reads first.
    drivers: list[str] = []
    if pc_lvl != "green" and max_probability is not None:
        drivers.append(f"Pc {max_probability:.1e} ({pc_lvl})")
    if miss_lvl != "green" and miss_distance_km is not None:
        drivers.append(f"miss {miss_distance_km:.3g} km ({miss_lvl})")
    if not drivers:
        drivers.append("below all action thresholds")

    hours_to_tca = (tca - now).total_seconds() / 3600.0
    decision_by = tca - timedelta(hours=t.lead_time_hours)
    # Past events carry no actionable decision window.
    decision_by_utc = decision_by.isoformat() if hours_to_tca > 0 else None

    is_stale = data_age_days is not None and data_age_days > t.stale_days
    confidence = "low" if (is_stale or data_age_days is None) else "high"
    if is_stale:
        drivers.append(f"stale data ({data_age_days:.1f} d old) -> low confidence")

    score = round(RISK_RANK[level] + _urgency(hours_to_tca) * 0.99, 4)

    return RiskAssessment(
        level=level,
        label=RISK_LABEL[level],
        score=score,
        drivers=drivers,
        decision_by_utc=decision_by_utc,
        confidence=confidence,
        is_stale=is_stale,
        data_age_days=data_age_days,
    )


# --------------------------------------------------------------------------- #
#  Trend: is this event getting worse across screenings?
# --------------------------------------------------------------------------- #
# After "how risky is it", the next question a CA analyst asks is "which way is
# it trending". SOCRATES re-screens ~3x/day, so each event accumulates a time
# series; a YELLOW whose miss distance is shrinking screening-over-screening is
# the one to watch, while a RED that is opening up is calming down. We mine the
# stored observation history to answer that, geometry-first (TLE-derived Pc is
# noisier than the miss-distance geometry), and stay conservative: if either
# signal says "worse", we surface "worsening".

TREND_DIRECTIONS = ("worsening", "improving", "stable", "new")


@dataclass(frozen=True)
class Trend:
    """How an event's prediction has evolved from its first to latest screening."""

    direction: str  # "worsening" | "improving" | "stable" | "new"
    screenings: int  # number of screenings the trend is based on
    miss_delta_km: float | None = None  # latest miss - first miss (neg = closer)
    pc_ratio: float | None = None  # latest Pc / first Pc (>1 = rising)
    note: str = ""  # plain-English summary for the analyst


def assess_trend(
    *,
    screenings: int,
    first_miss_km: float | None = None,
    last_miss_km: float | None = None,
    first_pc: float | None = None,
    last_pc: float | None = None,
    rel_threshold: float = 0.05,
) -> Trend:
    """Classify the evolution of one conjunction across its screening history.

    ``rel_threshold`` is the minimum *relative* change (5% by default) before a
    signal counts as movement, so screening noise does not flip the verdict. The
    Pc signal uses a wider band (4x the threshold, i.e. 20%) because Pc is far
    noisier than the geometry. Pure and deterministic: no I/O, no clock.
    """
    if screenings < 2:
        return Trend(
            direction="new",
            screenings=screenings,
            note="single screening so far — no trend yet",
        )

    miss_delta = None
    if first_miss_km is not None and last_miss_km is not None:
        miss_delta = last_miss_km - first_miss_km
    pc_ratio = None
    if first_pc and last_pc and first_pc > 0:
        pc_ratio = last_pc / first_pc

    worse = better = 0
    # Geometry: a shrinking miss distance is worse.
    if miss_delta is not None and first_miss_km:
        rel = miss_delta / first_miss_km
        if rel <= -rel_threshold:
            worse += 1
        elif rel >= rel_threshold:
            better += 1
    # Probability: a rising Pc is worse (wider deadband — Pc is noisy).
    if pc_ratio is not None:
        if pc_ratio >= 1.0 + 4 * rel_threshold:
            worse += 1
        elif pc_ratio <= 1.0 - 4 * rel_threshold:
            better += 1

    if worse > better:
        direction = "worsening"
    elif better > worse:
        direction = "improving"
    else:
        direction = "stable"

    bits = []
    if miss_delta is not None:
        verb = "closer" if miss_delta < 0 else "farther"
        bits.append(f"miss {abs(miss_delta):.3g} km {verb}")
    if pc_ratio is not None:
        bits.append(f"Pc x{pc_ratio:.2g}")
    detail = ", ".join(bits) if bits else "no change in tracked metrics"
    note = f"{direction} over {screenings} screenings ({detail})"

    return Trend(
        direction=direction,
        screenings=screenings,
        miss_delta_km=round(miss_delta, 4) if miss_delta is not None else None,
        pc_ratio=round(pc_ratio, 4) if pc_ratio is not None else None,
        note=note,
    )
