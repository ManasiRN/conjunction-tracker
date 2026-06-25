"""Pydantic response models.

These define the public API contract and drive the auto-generated OpenAPI /
Swagger documentation. Field descriptions here become the docs the FDS team
reads, so they are written for a human consumer, not just a machine.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ObjectInfo(BaseModel):
    norad_id: int = Field(..., description="NORAD catalog number of the object.")
    name: str = Field(..., description="Object name (status suffix removed).")
    status: str = Field(..., description="Operational status, decoded from SOCRATES.")
    days_since_epoch: float | None = Field(
        None, description="Age of this object's orbital data at TCA, in days."
    )
    is_monitored: bool = Field(
        ..., description="True if this object is one of our configured satellites."
    )
    is_debris: bool = Field(
        ..., description="Heuristic flag: debris or rocket body (cannot maneuver)."
    )


class RiskOut(BaseModel):
    """Operational verdict for a conjunction (the Flight Dynamics triage layer)."""

    level: str = Field(..., description="'red' | 'yellow' | 'green'.")
    label: str = Field(..., description="'ACTION' | 'MONITOR' | 'CLEAR'.")
    score: float = Field(
        ..., description="Urgency for ranking; higher means act sooner."
    )
    drivers: list[str] = Field(
        default_factory=list,
        description="Plain-English reasons for the level (Pc, miss, staleness).",
    )
    decision_by_utc: str | None = Field(
        None, description="Latest sensible maneuver-decision time (TCA - lead)."
    )
    confidence: str = Field(
        "high", description="'low' when based on stale orbit data (old TLE)."
    )
    is_stale: bool = Field(
        False, description="True if either object's TLE is older than the limit."
    )
    data_age_days: float | None = Field(
        None, description="Age of the oldest of the two TLEs, in days."
    )


class TrendOut(BaseModel):
    """How the prediction has evolved across screenings (the 'which way is it
    going' signal an analyst reads right after the risk level)."""

    direction: str = Field(
        ..., description="'worsening' | 'improving' | 'stable' | 'new'."
    )
    screenings: int = Field(
        ..., description="Number of screenings the trend is based on."
    )
    miss_delta_km: float | None = Field(
        None, description="Latest miss minus first miss, km (negative = closer)."
    )
    pc_ratio: float | None = Field(
        None, description="Latest Pc / first Pc (>1 means probability is rising)."
    )
    note: str = Field("", description="Plain-English summary of the trend.")


class ConjunctionOut(BaseModel):
    id: str = Field(..., description="Stable conjunction identifier.")
    tca_utc: str = Field(..., description="Time of Closest Approach (UTC, ISO-8601).")
    is_historical: bool = Field(
        ..., description="True if the TCA is in the past relative to 'now'."
    )
    hours_to_tca: float = Field(
        ..., description="Hours from now until TCA (negative if already past)."
    )
    miss_distance_km: float = Field(..., description="Predicted miss distance at TCA, km.")
    relative_speed_km_s: float | None = Field(
        None, description="Relative velocity magnitude at TCA, km/s."
    )
    max_probability: float | None = Field(
        None, description="Maximum collision probability (dimensionless)."
    )
    dilution_km: float | None = Field(
        None, description="Dilution threshold (prediction uncertainty), km."
    )
    satellite: ObjectInfo = Field(
        ..., description="The monitored Pixxel satellite in this conjunction."
    )
    threat: ObjectInfo = Field(
        ..., description="The other object approaching the satellite."
    )
    both_monitored: bool = Field(
        False, description="True if both objects are monitored satellites."
    )
    screening_count: int = Field(
        ..., description="Number of SOCRATES screenings this event has appeared in."
    )
    first_seen_utc: str = Field(..., description="When we first ingested this event.")
    last_updated_utc: str = Field(..., description="When we last refreshed it.")
    risk: RiskOut = Field(..., description="Operational risk verdict (triage).")
    trend: TrendOut = Field(
        ..., description="Whether the event is worsening across screenings."
    )


class ObservationOut(BaseModel):
    observed_at_utc: str = Field(..., description="When this screening was ingested.")
    tca_utc: str
    miss_distance_km: float | None = None
    relative_speed_km_s: float | None = None
    max_probability: float | None = None
    dilution_km: float | None = None


class ConjunctionDetailOut(ConjunctionOut):
    observations: list[ObservationOut] = Field(
        default_factory=list,
        description="History of every screening, to show how the prediction evolved.",
    )


class ConjunctionListOut(BaseModel):
    count: int = Field(..., description="Number of results in this page.")
    total: int = Field(..., description="Total matching the filter (ignoring paging).")
    limit: int
    offset: int
    results: list[ConjunctionOut]


class SatelliteOut(BaseModel):
    norad_id: int
    name: str | None = None
    notes: str | None = None
    upcoming_conjunctions: int
    total_conjunctions: int


class FetchInfo(BaseModel):
    status: str | None = None
    http_status: int | None = None
    started_at_utc: str | None = None
    finished_at_utc: str | None = None
    rows_relevant: int | None = None
    conjunctions_new: int | None = None
    conjunctions_updated: int | None = None
    duration_ms: int | None = None
    error: str | None = None


class HealthOut(BaseModel):
    status: str = Field(
        ..., description="'ok' or 'degraded' (degraded if data is stale or absent)."
    )
    version: str
    time_utc: str
    monitored_satellite_ids: list[int]
    database: dict
    data_fresh: bool = Field(
        True,
        description="False if the last successful SOCRATES contact is older than "
        "the freshness limit -- stale awareness is as dangerous as none.",
    )
    data_age_seconds: float | None = Field(
        None, description="Seconds since the last successful SOCRATES contact (incl. 304)."
    )
    data_age_limit_seconds: float | None = Field(
        None, description="Age beyond which data is considered stale (degraded)."
    )
    last_fetch: FetchInfo | None = None


class AlertsOut(BaseModel):
    """The Flight Dynamics 'what must I act on right now' view."""

    generated_at_utc: str
    threshold: str = Field(..., description="Lowest level included ('red' or 'yellow').")
    red_count: int = Field(..., description="Upcoming RED (ACTION) conjunctions.")
    yellow_count: int = Field(..., description="Upcoming YELLOW (MONITOR) conjunctions.")
    next_decision_by_utc: str | None = Field(
        None, description="Soonest maneuver-decision deadline among the alerts."
    )
    alerts: list[ConjunctionOut] = Field(
        default_factory=list, description="Alerting conjunctions, most urgent first."
    )


class ThreatGroupOut(BaseModel):
    """One external object grouped against the whole monitored fleet.

    The three Pixxel Fireflies fly as a cluster in similar orbits, so a single
    debris object or rocket body often has close approaches with more than one
    of them in the same pass. Grouping by the threatening object turns N
    separate rows into one fleet-level situational picture -- the question an
    FDS analyst actually asks: *'what is coming at my constellation, and how
    many of my satellites does it endanger?'*
    """

    threat: ObjectInfo = Field(..., description="The threatening (usually external) object.")
    satellites_threatened: list[int] = Field(
        ..., description="NORAD IDs of monitored satellites this object approaches."
    )
    satellite_count: int = Field(
        ..., description="How many distinct monitored satellites it threatens."
    )
    worst_risk_level: str = Field(..., description="Worst risk level across its approaches.")
    soonest_tca_utc: str = Field(..., description="Earliest TCA among its approaches.")
    closest_miss_km: float = Field(..., description="Closest miss distance among its approaches.")
    conjunctions: list[ConjunctionOut] = Field(
        default_factory=list, description="The individual approaches, worst first."
    )


class ThreatsOut(BaseModel):
    """Fleet-level threat picture: objects grouped by what they endanger."""

    generated_at_utc: str
    min_satellites: int = Field(
        ..., description="Only objects threatening at least this many satellites are listed."
    )
    fleet_threat_count: int = Field(
        ..., description="Objects threatening 2+ monitored satellites (constellation-wide)."
    )
    threats: list[ThreatGroupOut] = Field(
        default_factory=list,
        description="Threat groups, most satellites-threatened (then worst risk) first.",
    )


class RefreshOut(BaseModel):
    status: str
    detail: str
    http_status: int | None = None
    rows_relevant: int | None = None
    conjunctions_new: int | None = None
    conjunctions_updated: int | None = None
    duration_ms: int | None = None
    error: str | None = None
