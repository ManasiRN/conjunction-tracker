"""FastAPI application: REST API + lifecycle wiring.

``create_app`` builds the whole service: database, SOCRATES client, fetcher,
and background scheduler, all bound to the app lifespan so a single process
(one container) runs everything. Interactive docs are served at ``/docs``
(Swagger UI) and ``/redoc``; the raw OpenAPI spec at ``/openapi.json``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query

from . import __version__
from .config import Settings, get_settings, load_satellites
from .database import Database
from .fetcher import CycleReport, Fetcher
from .scheduler import FetchScheduler
from .risk import RISK_RANK, assess, assess_trend
from .schemas import (
    AlertsOut,
    ConjunctionDetailOut,
    ConjunctionListOut,
    ConjunctionOut,
    FetchInfo,
    HealthOut,
    ObjectInfo,
    ObservationOut,
    RefreshOut,
    RiskOut,
    SatelliteOut,
    ThreatGroupOut,
    ThreatsOut,
    TrendOut,
)
from .socrates_client import SocratesClient

logger = logging.getLogger(__name__)

_DEBRIS_MARKERS = ("DEB", "DEBRIS", "R/B", "ROCKET BODY", "COOLANT", "FRAG")


def _is_debris(name: str) -> bool:
    upper = (name or "").upper()
    return any(marker in upper for marker in _DEBRIS_MARKERS)


def _max_dse(row: sqlite3.Row) -> float | None:
    """Oldest of the two objects' TLE ages (days since epoch), if known."""
    ages = [a for a in (row["dse_1"], row["dse_2"]) if a is not None]
    return max(ages) if ages else None


def _trend_out(row: sqlite3.Row, summary: dict | None) -> TrendOut:
    """Build the trend verdict for a row from a (possibly absent) screening
    summary, falling back to the row's own screening_count when no history
    summary was supplied (e.g. a freshly-inserted event)."""
    if summary is None:
        summary = {"screenings": row["screening_count"]}
    t = assess_trend(
        screenings=summary.get("screenings", row["screening_count"]),
        first_miss_km=summary.get("first_miss_km"),
        last_miss_km=summary.get("last_miss_km"),
        first_pc=summary.get("first_pc"),
        last_pc=summary.get("last_pc"),
    )
    return TrendOut(
        direction=t.direction,
        screenings=t.screenings,
        miss_delta_km=t.miss_delta_km,
        pc_ratio=t.pc_ratio,
        note=t.note,
    )


def _row_to_conjunction(
    row: sqlite3.Row,
    monitored: set[int],
    now: datetime,
    thresholds=None,
    trend_summary: dict | None = None,
) -> ConjunctionOut:
    """Map a DB row to the API shape, labelling which object is *ours*."""
    o1_monitored = row["norad_id_1"] in monitored
    o2_monitored = row["norad_id_2"] in monitored

    obj1 = ObjectInfo(
        norad_id=row["norad_id_1"], name=row["name_1"] or "", status=row["status_1"] or "unknown",
        days_since_epoch=row["dse_1"], is_monitored=o1_monitored,
        is_debris=_is_debris(row["name_1"]),
    )
    obj2 = ObjectInfo(
        norad_id=row["norad_id_2"], name=row["name_2"] or "", status=row["status_2"] or "unknown",
        days_since_epoch=row["dse_2"], is_monitored=o2_monitored,
        is_debris=_is_debris(row["name_2"]),
    )

    # Present the monitored object as "satellite", the other as "threat".
    if o1_monitored:
        satellite, threat = obj1, obj2
    else:
        satellite, threat = obj2, obj1

    tca = datetime.fromisoformat(row["tca_utc"])
    hours_to_tca = (tca - now).total_seconds() / 3600.0

    a = assess(
        max_probability=row["max_probability"],
        miss_distance_km=row["miss_distance_km"],
        tca=tca,
        now=now,
        data_age_days=_max_dse(row),
        thresholds=thresholds,
    )
    risk = RiskOut(
        level=a.level, label=a.label, score=a.score, drivers=a.drivers,
        decision_by_utc=a.decision_by_utc, confidence=a.confidence,
        is_stale=a.is_stale, data_age_days=a.data_age_days,
    )

    return ConjunctionOut(
        id=row["conjunction_id"],
        tca_utc=row["tca_utc"],
        is_historical=tca < now,
        hours_to_tca=round(hours_to_tca, 3),
        miss_distance_km=row["miss_distance_km"],
        relative_speed_km_s=row["relative_speed_km_s"],
        max_probability=row["max_probability"],
        dilution_km=row["dilution_km"],
        satellite=satellite,
        threat=threat,
        both_monitored=o1_monitored and o2_monitored,
        screening_count=row["screening_count"],
        first_seen_utc=row["first_seen_utc"],
        last_updated_utc=row["last_updated_utc"],
        risk=risk,
        trend=_trend_out(row, trend_summary),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    satellites = load_satellites(settings.satellite_config_path)
    monitored = {s.norad_id for s in satellites}
    thresholds = settings.risk_thresholds()

    db = Database(settings.database_path)
    client = SocratesClient(settings)
    fetcher = Fetcher(settings, db, client)
    scheduler = FetchScheduler(settings, fetcher)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("Monitoring %d satellites: %s", len(monitored), sorted(monitored))
        if settings.fetch_on_startup:
            # Run the initial fetch in a background thread so the API (and its
            # /health endpoint, used by the Docker healthcheck) is available
            # immediately. The ~16 MB download + parse must not block startup;
            # data populates a few seconds later.
            logger.info("Scheduling startup fetch in background...")
            threading.Thread(
                target=fetcher.run_once,
                kwargs={"force": True},
                name="startup-fetch",
                daemon=True,
            ).start()
        scheduler.start()
        try:
            yield
        finally:
            scheduler.shutdown()
            db.close()

    app = FastAPI(
        title="Pixxel FDS - Conjunction Tracker",
        version=__version__,
        lifespan=lifespan,
        description=(
            "Conjunction-awareness service for Pixxel Flight Dynamics.\n\n"
            "Fetches close-approach predictions from CelesTrak SOCRATES for the "
            "configured satellites, stores current and historical data, and "
            "serves it over this API. Interactive docs: `/docs`.\n\n"
            "**Caveat on probability:** SOCRATES `max_probability` (Pc) is derived "
            "from public TLEs with an assumed hard-body radius, not from precise "
            "operational ephemeris. Treat it as a *screening* signal: a RED means "
            "'pull a real CDM / operator ephemeris before maneuvering', not a final "
            "maneuver trigger. The `risk.confidence` field flags events computed "
            "from stale orbit data, and `trend` shows whether an event is worsening "
            "across screenings."
        ),
    )

    def now_utc() -> datetime:
        return datetime.now(timezone.utc)

    # ---------------------------------------------------------------- health
    @app.get("/health", response_model=HealthOut, tags=["service"])
    def health():
        """Liveness + operational status (last fetch, DB counts)."""
        last = db.get_last_fetch()
        last_info = None
        if last is not None:
            last_info = FetchInfo(
                status=last["status"],
                http_status=last["http_status"],
                started_at_utc=last["started_at_utc"],
                finished_at_utc=last["finished_at_utc"],
                rows_relevant=last["rows_relevant"],
                conjunctions_new=last["conjunctions_new"],
                conjunctions_updated=last["conjunctions_updated"],
                duration_ms=last["duration_ms"],
                error=last["error"],
            )
        # Data-freshness alarm: a service that fetched once and then went silent
        # is as dangerous as one that never fetched -- stale awareness reads as
        # current awareness on the board. A 304 counts as a successful contact
        # (source unchanged => our data is still current).
        now = now_utc()
        limit_seconds = settings.fetch_interval_seconds * settings.health_stale_factor
        last_contact = db.get_last_successful_contact()
        data_age_seconds: float | None = None
        if last_contact is not None and last_contact["finished_at_utc"]:
            data_age_seconds = round(
                (now - datetime.fromisoformat(last_contact["finished_at_utc"]))
                .total_seconds(),
                1,
            )
        ever_succeeded = db.get_last_successful_fetch() is not None
        data_fresh = (
            data_age_seconds is not None and data_age_seconds <= limit_seconds
        )
        return HealthOut(
            status="ok" if (ever_succeeded and data_fresh) else "degraded",
            version=__version__,
            time_utc=now.isoformat(),
            monitored_satellite_ids=sorted(monitored),
            database=db.stats(),
            data_fresh=data_fresh,
            data_age_seconds=data_age_seconds,
            data_age_limit_seconds=round(limit_seconds, 1),
            last_fetch=last_info,
        )

    # ----------------------------------------------------------- satellites
    @app.get("/satellites", response_model=list[SatelliteOut], tags=["satellites"])
    def list_satellites():
        """List configured satellites with conjunction counts."""
        out = []
        for s in satellites:
            out.append(
                SatelliteOut(
                    norad_id=s.norad_id,
                    name=s.name,
                    notes=s.notes,
                    upcoming_conjunctions=db.count_conjunctions(
                        norad_id=s.norad_id, time_filter="upcoming"
                    ),
                    total_conjunctions=db.count_conjunctions(norad_id=s.norad_id),
                )
            )
        return out

    @app.get(
        "/satellites/{norad_id}/conjunctions",
        response_model=ConjunctionListOut,
        tags=["satellites"],
    )
    def satellite_conjunctions(
        norad_id: int,
        time_filter: str = Query("upcoming", pattern="^(upcoming|historical|all)$"),
        order_by: str = Query("tca", pattern="^(tca|miss|probability)$"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """Conjunctions for one configured satellite."""
        if norad_id not in monitored:
            raise HTTPException(
                status_code=404,
                detail=f"Satellite {norad_id} is not configured for monitoring.",
            )
        return _list(
            norad_id=norad_id, time_filter=time_filter, order_by=order_by,
            limit=limit, offset=offset,
        )

    # ---------------------------------------------------------- conjunctions
    @app.get("/conjunctions", response_model=ConjunctionListOut, tags=["conjunctions"])
    def conjunctions(
        norad_id: int | None = Query(None, description="Filter to one monitored satellite."),
        time_filter: str = Query("upcoming", pattern="^(upcoming|historical|all)$"),
        max_miss_km: float | None = Query(None, ge=0, description="Only closer than this."),
        min_probability: float | None = Query(None, ge=0, description="Only riskier than this."),
        min_risk: str | None = Query(
            None, pattern="^(red|yellow|green)$",
            description="Only this risk level or worse (red|yellow|green).",
        ),
        order_by: str = Query("tca", pattern="^(tca|miss|probability)$"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """Query conjunctions across all monitored satellites.

        Defaults to *upcoming* events sorted by soonest TCA -- the FDS's primary
        operational view.
        """
        return _list(
            norad_id=norad_id, time_filter=time_filter, max_miss_km=max_miss_km,
            min_probability=min_probability, min_risk=min_risk, order_by=order_by,
            limit=limit, offset=offset,
        )

    @app.get(
        "/conjunctions/{conjunction_id}",
        response_model=ConjunctionDetailOut,
        tags=["conjunctions"],
    )
    def conjunction_detail(conjunction_id: str):
        """A single conjunction plus its full screening history (trend)."""
        row = db.get_conjunction(conjunction_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Conjunction not found.")
        trends = db.get_trend_summaries([conjunction_id])
        base = _row_to_conjunction(
            row, monitored, now_utc(), thresholds, trends.get(conjunction_id)
        )
        observations = [
            ObservationOut(
                observed_at_utc=o["observed_at_utc"],
                tca_utc=o["tca_utc"],
                miss_distance_km=o["miss_distance_km"],
                relative_speed_km_s=o["relative_speed_km_s"],
                max_probability=o["max_probability"],
                dilution_km=o["dilution_km"],
            )
            for o in db.get_observations(conjunction_id)
        ]
        return ConjunctionDetailOut(**base.model_dump(), observations=observations)

    # ---------------------------------------------------------------- alerts
    @app.get("/alerts", response_model=AlertsOut, tags=["service"])
    def alerts(
        threshold: str = Query(
            "yellow", pattern="^(red|yellow)$",
            description="Lowest level to include: 'red' (ACTION only) or 'yellow'.",
        ),
    ):
        """Actionable triage: upcoming RED/YELLOW conjunctions, most urgent first.

        This is the Flight Dynamics 'what do I need to do right now' view -- it
        answers the operational question directly instead of leaving the analyst
        to interpret a chart. Sorted by urgency (risk level, then soonest TCA).
        """
        now = now_utc()
        floor = RISK_RANK[threshold]
        rows = db.query_conjunctions(
            time_filter="upcoming", order_by="tca", limit=1000, offset=0
        )
        trends = db.get_trend_summaries([r["conjunction_id"] for r in rows])
        assessed = [
            _row_to_conjunction(
                r, monitored, now, thresholds, trends.get(r["conjunction_id"])
            )
            for r in rows
        ]
        red = [c for c in assessed if c.risk.level == "red"]
        yellow = [c for c in assessed if c.risk.level == "yellow"]
        alerting = [c for c in assessed if RISK_RANK[c.risk.level] >= floor]
        alerting.sort(key=lambda c: c.risk.score, reverse=True)
        deadlines = [c.risk.decision_by_utc for c in alerting if c.risk.decision_by_utc]
        return AlertsOut(
            generated_at_utc=now.isoformat(),
            threshold=threshold,
            red_count=len(red),
            yellow_count=len(yellow),
            next_decision_by_utc=min(deadlines) if deadlines else None,
            alerts=alerting,
        )

    # --------------------------------------------------------------- threats
    @app.get("/threats", response_model=ThreatsOut, tags=["service"])
    def threats(
        min_satellites: int = Query(
            1, ge=1,
            description="Only list objects threatening at least this many monitored "
            "satellites. Set to 2 for constellation-wide threats only.",
        ),
        time_filter: str = Query("upcoming", pattern="^(upcoming|historical|all)$"),
    ):
        """Fleet-level threat picture: conjunctions grouped by the threatening object.

        The Pixxel Fireflies fly as a cluster, so one debris object can endanger
        several of them in a single pass. This view answers *'what is coming at my
        constellation, and how many of my satellites does it threaten?'* -- a
        question the per-conjunction list cannot, because it scatters one physical
        threat across multiple rows.
        """
        now = now_utc()
        rows = db.query_conjunctions(
            time_filter=time_filter, order_by="tca", limit=1_000_000, offset=0
        )
        trends = db.get_trend_summaries([r["conjunction_id"] for r in rows])
        assessed = [
            _row_to_conjunction(
                r, monitored, now, thresholds, trends.get(r["conjunction_id"])
            )
            for r in rows
        ]

        # Group by the threatening object (the side that is not ours).
        groups: dict[int, list] = {}
        for c in assessed:
            groups.setdefault(c.threat.norad_id, []).append(c)

        threat_groups = []
        for _, items in groups.items():
            sats = sorted({c.satellite.norad_id for c in items})
            if len(sats) < min_satellites:
                continue
            items.sort(key=lambda c: c.risk.score, reverse=True)
            worst = max(items, key=lambda c: RISK_RANK[c.risk.level])
            threat_groups.append(
                ThreatGroupOut(
                    threat=worst.threat,
                    satellites_threatened=sats,
                    satellite_count=len(sats),
                    worst_risk_level=worst.risk.level,
                    soonest_tca_utc=min(c.tca_utc for c in items),
                    closest_miss_km=min(c.miss_distance_km for c in items),
                    conjunctions=items,
                )
            )

        # Constellation-wide threats first, then by worst risk.
        threat_groups.sort(
            key=lambda g: (g.satellite_count, RISK_RANK[g.worst_risk_level]),
            reverse=True,
        )
        fleet_count = sum(1 for g in threat_groups if g.satellite_count >= 2)
        return ThreatsOut(
            generated_at_utc=now.isoformat(),
            min_satellites=min_satellites,
            fleet_threat_count=fleet_count,
            threats=threat_groups,
        )

    # --------------------------------------------------------------- refresh
    @app.post("/refresh", response_model=RefreshOut, tags=["service"])
    def refresh():
        """Trigger a fetch cycle now (subject to the min-interval guard).

        Useful for demos/ops. The guard prevents this from being used to hammer
        CelesTrak; use the scheduler for routine updates.
        """
        report: CycleReport = fetcher.run_once(force=False)
        detail = {
            "ok": "Fetch completed.",
            "not_modified": "Source unchanged since last fetch (304).",
            "skipped": f"Skipped: {report.skipped_reason}.",
            "error": "Fetch failed; see error.",
        }.get(report.status, report.status)
        return RefreshOut(
            status=report.status,
            detail=detail,
            http_status=report.http_status,
            rows_relevant=report.rows_relevant,
            conjunctions_new=report.conjunctions_new,
            conjunctions_updated=report.conjunctions_updated,
            duration_ms=report.duration_ms,
            error=report.error,
        )

    # ------------------------------------------------------------- internals
    def _list(
        *, norad_id=None, time_filter="upcoming", max_miss_km=None,
        min_probability=None, min_risk=None, order_by="tca", limit=100, offset=0,
    ) -> ConjunctionListOut:
        now = now_utc()

        def assemble(rows):
            trends = db.get_trend_summaries([r["conjunction_id"] for r in rows])
            return [
                _row_to_conjunction(
                    r, monitored, now, thresholds, trends.get(r["conjunction_id"])
                )
                for r in rows
            ]

        if min_risk is None:
            # SQL-only filters: let the DB do LIMIT/OFFSET and the count.
            rows = db.query_conjunctions(
                norad_id=norad_id, time_filter=time_filter, max_miss_km=max_miss_km,
                min_probability=min_probability, order_by=order_by,
                limit=limit, offset=offset,
            )
            total = db.count_conjunctions(
                norad_id=norad_id, time_filter=time_filter,
                max_miss_km=max_miss_km, min_probability=min_probability,
            )
            results = assemble(rows)
        else:
            # Risk is computed, not stored, so it cannot be filtered in SQL.
            # Assess the full filtered set first, then filter by risk, then
            # paginate -- otherwise paging would hide RED events on later pages
            # and `total` would be wrong. (Dataset is small: hundreds of rows.)
            all_rows = db.query_conjunctions(
                norad_id=norad_id, time_filter=time_filter, max_miss_km=max_miss_km,
                min_probability=min_probability, order_by=order_by,
                limit=1_000_000, offset=0,
            )
            floor = RISK_RANK[min_risk]
            matched = [c for c in assemble(all_rows) if RISK_RANK[c.risk.level] >= floor]
            total = len(matched)
            results = matched[offset : offset + limit]

        return ConjunctionListOut(
            count=len(results), total=total, limit=limit, offset=offset,
            results=results,
        )

    return app
