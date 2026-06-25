"""SQLite persistence layer.

Why SQLite? For a single-container service tracking tens-to-thousands of
conjunctions, SQLite is the right tool: zero-ops, a single portable file on a
mounted volume, transactional, and fast for this read-mostly workload. The
repository pattern below keeps all SQL in one place, so swapping to Postgres
later would be a localised change.

Three tables:

* ``conjunctions``        -- current state of each unique conjunction event
                             (upserted; never deleted -> this *is* the history).
* ``conjunction_observations`` -- append-only log of every screening in which a
                             conjunction appeared, so the FDS can see how a
                             prediction (miss distance / probability) *evolved*
                             over time -- exactly the trend an analyst watches
                             when deciding whether to plan a maneuver.
* ``fetch_log``           -- audit of every fetch cycle (health + anti-ban proof).

"Upcoming" vs "historical" is derived from TCA relative to now, not stored as a
flag, so it is always correct without a background job to flip statuses.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .parser import ParsedConjunction

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conjunctions (
    conjunction_id      TEXT PRIMARY KEY,
    norad_id_1          INTEGER NOT NULL,
    name_1              TEXT,
    status_1            TEXT,
    dse_1               REAL,
    norad_id_2          INTEGER NOT NULL,
    name_2              TEXT,
    status_2            TEXT,
    dse_2               REAL,
    tca_utc             TEXT NOT NULL,
    miss_distance_km    REAL NOT NULL,
    relative_speed_km_s REAL,
    max_probability     REAL,
    dilution_km         REAL,
    first_seen_utc      TEXT NOT NULL,
    last_updated_utc    TEXT NOT NULL,
    screening_count     INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_conj_tca   ON conjunctions(tca_utc);
CREATE INDEX IF NOT EXISTS idx_conj_id1   ON conjunctions(norad_id_1);
CREATE INDEX IF NOT EXISTS idx_conj_id2   ON conjunctions(norad_id_2);
CREATE INDEX IF NOT EXISTS idx_conj_range ON conjunctions(miss_distance_km);

CREATE TABLE IF NOT EXISTS conjunction_observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conjunction_id      TEXT NOT NULL,
    observed_at_utc     TEXT NOT NULL,
    tca_utc             TEXT NOT NULL,
    miss_distance_km    REAL,
    relative_speed_km_s REAL,
    max_probability     REAL,
    dilution_km         REAL,
    FOREIGN KEY (conjunction_id) REFERENCES conjunctions(conjunction_id)
);

CREATE INDEX IF NOT EXISTS idx_obs_conj ON conjunction_observations(conjunction_id);

CREATE TABLE IF NOT EXISTS fetch_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc      TEXT NOT NULL,
    finished_at_utc     TEXT,
    status              TEXT NOT NULL,
    http_status         INTEGER,
    rows_total          INTEGER,
    rows_relevant       INTEGER,
    conjunctions_new    INTEGER,
    conjunctions_updated INTEGER,
    bytes_downloaded    INTEGER,
    duration_ms         INTEGER,
    error               TEXT
);
"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Thread-safe SQLite repository.

    The API (async, threadpool) and the scheduler (background thread) both touch
    the DB, so we run in WAL mode (concurrent readers + one writer) and guard
    writes with a process-level lock for safety on the single connection.
    """

    def __init__(self, db_path: str):
        self._path = db_path
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.execute("PRAGMA busy_timeout=30000;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """Serialise writers and commit/rollback as a unit."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # -- ingestion -----------------------------------------------------------

    def upsert_conjunctions(
        self, items: Iterable[ParsedConjunction], observed_at: str | None = None
    ) -> tuple[int, int]:
        """Insert new conjunctions / update existing ones; log an observation
        for each. Returns ``(new_count, updated_count)``.
        """
        observed_at = observed_at or _utcnow_iso()
        new_count = 0
        updated_count = 0
        with self._write() as conn:
            for c in items:
                exists = conn.execute(
                    "SELECT 1 FROM conjunctions WHERE conjunction_id = ?",
                    (c.conjunction_id,),
                ).fetchone()

                if exists:
                    conn.execute(
                        """
                        UPDATE conjunctions SET
                            name_1=?, status_1=?, dse_1=?,
                            name_2=?, status_2=?, dse_2=?,
                            tca_utc=?, miss_distance_km=?, relative_speed_km_s=?,
                            max_probability=?, dilution_km=?,
                            last_updated_utc=?,
                            screening_count = screening_count + 1
                        WHERE conjunction_id=?
                        """,
                        (
                            c.name_1, c.status_1, c.dse_1,
                            c.name_2, c.status_2, c.dse_2,
                            c.tca_utc, c.miss_distance_km, c.relative_speed_km_s,
                            c.max_probability, c.dilution_km,
                            observed_at,
                            c.conjunction_id,
                        ),
                    )
                    updated_count += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO conjunctions (
                            conjunction_id, norad_id_1, name_1, status_1, dse_1,
                            norad_id_2, name_2, status_2, dse_2,
                            tca_utc, miss_distance_km, relative_speed_km_s,
                            max_probability, dilution_km,
                            first_seen_utc, last_updated_utc, screening_count
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                        """,
                        (
                            c.conjunction_id, c.norad_id_1, c.name_1, c.status_1, c.dse_1,
                            c.norad_id_2, c.name_2, c.status_2, c.dse_2,
                            c.tca_utc, c.miss_distance_km, c.relative_speed_km_s,
                            c.max_probability, c.dilution_km,
                            observed_at, observed_at,
                        ),
                    )
                    new_count += 1

                # Append-only observation (prediction snapshot for trend analysis).
                conn.execute(
                    """
                    INSERT INTO conjunction_observations (
                        conjunction_id, observed_at_utc, tca_utc,
                        miss_distance_km, relative_speed_km_s,
                        max_probability, dilution_km
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        c.conjunction_id, observed_at, c.tca_utc,
                        c.miss_distance_km, c.relative_speed_km_s,
                        c.max_probability, c.dilution_km,
                    ),
                )
        return new_count, updated_count

    # -- queries -------------------------------------------------------------

    def query_conjunctions(
        self,
        *,
        norad_id: int | None = None,
        time_filter: str = "all",  # "upcoming" | "historical" | "all"
        max_miss_km: float | None = None,
        min_probability: float | None = None,
        order_by: str = "tca",  # "tca" | "miss" | "probability"
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        now = _utcnow_iso()

        if norad_id is not None:
            clauses.append("(norad_id_1 = ? OR norad_id_2 = ?)")
            params.extend([norad_id, norad_id])
        if time_filter == "upcoming":
            clauses.append("tca_utc >= ?")
            params.append(now)
        elif time_filter == "historical":
            clauses.append("tca_utc < ?")
            params.append(now)
        if max_miss_km is not None:
            clauses.append("miss_distance_km <= ?")
            params.append(max_miss_km)
        if min_probability is not None:
            clauses.append("max_probability >= ?")
            params.append(min_probability)

        order_col = {
            "tca": "tca_utc ASC",
            "miss": "miss_distance_km ASC",
            "probability": "max_probability DESC",
        }.get(order_by, "tca_utc ASC")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM conjunctions {where} "
            f"ORDER BY {order_col} LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        return self._conn.execute(sql, params).fetchall()

    def count_conjunctions(
        self,
        *,
        norad_id: int | None = None,
        time_filter: str = "all",
        max_miss_km: float | None = None,
        min_probability: float | None = None,
    ) -> int:
        """Count conjunctions matching the SQL-expressible filters.

        Mirrors :meth:`query_conjunctions` so a list endpoint's ``total`` is the
        true count for the same filter (minus paging). Risk level is *computed*,
        not stored, so ``min_risk`` is not handled here -- the API applies that
        after assessment.
        """
        clauses: list[str] = []
        params: list[Any] = []
        now = _utcnow_iso()
        if norad_id is not None:
            clauses.append("(norad_id_1 = ? OR norad_id_2 = ?)")
            params.extend([norad_id, norad_id])
        if time_filter == "upcoming":
            clauses.append("tca_utc >= ?")
            params.append(now)
        elif time_filter == "historical":
            clauses.append("tca_utc < ?")
            params.append(now)
        if max_miss_km is not None:
            clauses.append("miss_distance_km <= ?")
            params.append(max_miss_km)
        if min_probability is not None:
            clauses.append("max_probability >= ?")
            params.append(min_probability)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) AS n FROM conjunctions {where}", params
        ).fetchone()
        return int(row["n"])

    def get_conjunction(self, conjunction_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM conjunctions WHERE conjunction_id = ?",
            (conjunction_id,),
        ).fetchone()

    def get_observations(self, conjunction_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM conjunction_observations "
            "WHERE conjunction_id = ? ORDER BY observed_at_utc ASC",
            (conjunction_id,),
        ).fetchall()

    def get_trend_summaries(
        self, conjunction_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """First and latest screened values per conjunction, in one query.

        Returns ``{conjunction_id: {screenings, first_miss_km, last_miss_km,
        first_pc, last_pc}}`` -- just enough for :func:`risk.assess_trend` to
        decide whether an event is worsening, without loading full histories for
        a whole page of results.
        """
        if not conjunction_ids:
            return {}
        placeholders = ",".join("?" for _ in conjunction_ids)
        rows = self._conn.execute(
            f"""
            WITH ranked AS (
                SELECT
                    conjunction_id, miss_distance_km, max_probability,
                    ROW_NUMBER() OVER (
                        PARTITION BY conjunction_id ORDER BY observed_at_utc ASC, id ASC
                    ) AS rn_first,
                    ROW_NUMBER() OVER (
                        PARTITION BY conjunction_id ORDER BY observed_at_utc DESC, id DESC
                    ) AS rn_last,
                    COUNT(*) OVER (PARTITION BY conjunction_id) AS n
                FROM conjunction_observations
                WHERE conjunction_id IN ({placeholders})
            )
            SELECT conjunction_id, miss_distance_km, max_probability, n, rn_first, rn_last
            FROM ranked
            WHERE rn_first = 1 OR rn_last = 1
            """,
            conjunction_ids,
        ).fetchall()

        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            cid = r["conjunction_id"]
            entry = out.setdefault(cid, {"screenings": int(r["n"])})
            if r["rn_first"] == 1:
                entry["first_miss_km"] = r["miss_distance_km"]
                entry["first_pc"] = r["max_probability"]
            if r["rn_last"] == 1:
                entry["last_miss_km"] = r["miss_distance_km"]
                entry["last_pc"] = r["max_probability"]
        return out

    # -- fetch audit / health ------------------------------------------------

    def start_fetch_log(self) -> int:
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO fetch_log (started_at_utc, status) VALUES (?, 'running')",
                (_utcnow_iso(),),
            )
            return int(cur.lastrowid)

    def finish_fetch_log(self, log_id: int, **fields: Any) -> None:
        fields.setdefault("finished_at_utc", _utcnow_iso())
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._write() as conn:
            conn.execute(
                f"UPDATE fetch_log SET {cols} WHERE id = ?",
                (*fields.values(), log_id),
            )

    def get_last_fetch(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM fetch_log ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def get_last_successful_fetch(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM fetch_log WHERE status = 'ok' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def get_last_successful_contact(self) -> sqlite3.Row | None:
        """Last cycle that successfully reached SOCRATES, counting a 304.

        A ``not_modified`` (304) response means the source has not changed, so
        our stored data is still current -- it is a *successful* contact for
        freshness purposes, even though it brought no new rows. Used by the
        health endpoint's data-freshness alarm.
        """
        return self._conn.execute(
            "SELECT * FROM fetch_log WHERE status IN ('ok', 'not_modified') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def stats(self) -> dict[str, Any]:
        now = _utcnow_iso()
        total = self._conn.execute(
            "SELECT COUNT(*) AS n FROM conjunctions"
        ).fetchone()["n"]
        upcoming = self._conn.execute(
            "SELECT COUNT(*) AS n FROM conjunctions WHERE tca_utc >= ?", (now,)
        ).fetchone()["n"]
        observations = self._conn.execute(
            "SELECT COUNT(*) AS n FROM conjunction_observations"
        ).fetchone()["n"]
        return {
            "total_conjunctions": int(total),
            "upcoming_conjunctions": int(upcoming),
            "historical_conjunctions": int(total) - int(upcoming),
            "total_observations": int(observations),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
