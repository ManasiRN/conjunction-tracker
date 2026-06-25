"""Fetch orchestration: glue between the SOCRATES client, parser, and database.

One public entry point, :meth:`Fetcher.run_once`, performs a full cycle:

    fetch (conditional GET) -> parse + filter -> upsert + log observations
                            -> write an audit row to fetch_log

It is deliberately defensive: every cycle is wrapped so that a failure is
recorded and logged but never crashes the scheduler or the API process. A
``min_interval`` guard prevents accidental hammering of CelesTrak (e.g. from a
manual ``/refresh`` storm), which is part of the anti-ban strategy.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Settings, monitored_id_set
from .database import Database
from .parser import parse_and_filter
from .socrates_client import SocratesClient, SocratesError

logger = logging.getLogger(__name__)


@dataclass
class CycleReport:
    """Human/JSON-friendly summary of a fetch cycle (returned by /refresh)."""

    status: str
    http_status: int | None = None
    rows_relevant: int = 0
    conjunctions_new: int = 0
    conjunctions_updated: int = 0
    bytes_downloaded: int = 0
    duration_ms: int = 0
    skipped_reason: str | None = None
    error: str | None = None


class Fetcher:
    def __init__(self, settings: Settings, db: Database, client: SocratesClient):
        self._settings = settings
        self._db = db
        self._client = client
        self._lock = threading.Lock()  # only one cycle runs at a time
        self._last_attempt_monotonic: float | None = None

    def run_once(self, *, force: bool = False) -> CycleReport:
        """Run a single fetch cycle. ``force`` bypasses the min-interval guard.

        Concurrency: if a cycle is already in progress we skip rather than queue,
        so overlapping triggers (scheduler + manual) cannot pile up.
        """
        if not self._lock.acquire(blocking=False):
            logger.info("Fetch already in progress; skipping this trigger.")
            return CycleReport(status="skipped", skipped_reason="already_running")
        try:
            if not force and self._too_soon():
                wait = self._settings.min_fetch_interval_seconds
                logger.info("Min-interval guard active; skipping fetch.")
                return CycleReport(
                    status="skipped",
                    skipped_reason=f"min_interval_{int(wait)}s_not_elapsed",
                )
            self._last_attempt_monotonic = time.monotonic()
            return self._do_cycle()
        finally:
            self._lock.release()

    def _too_soon(self) -> bool:
        if self._last_attempt_monotonic is None:
            return False
        elapsed = time.monotonic() - self._last_attempt_monotonic
        return elapsed < self._settings.min_fetch_interval_seconds

    def _do_cycle(self) -> CycleReport:
        started = time.monotonic()
        log_id = self._db.start_fetch_log()
        try:
            result = self._client.fetch()

            if result.status == "not_modified":
                duration_ms = int((time.monotonic() - started) * 1000)
                self._db.finish_fetch_log(
                    log_id,
                    status="not_modified",
                    http_status=304,
                    duration_ms=duration_ms,
                )
                return CycleReport(
                    status="not_modified", http_status=304, duration_ms=duration_ms
                )

            if result.status != "ok" or result.body is None:
                duration_ms = int((time.monotonic() - started) * 1000)
                self._db.finish_fetch_log(
                    log_id,
                    status="error",
                    http_status=result.http_status,
                    duration_ms=duration_ms,
                    error=result.error or "non-ok fetch",
                )
                return CycleReport(
                    status="error",
                    http_status=result.http_status,
                    duration_ms=duration_ms,
                    error=result.error,
                )

            monitored = monitored_id_set(self._settings.satellite_config_path)
            observed_at = datetime.now(timezone.utc).isoformat()
            parsed = parse_and_filter(result.body, monitored)
            new_count, updated_count = self._db.upsert_conjunctions(
                parsed, observed_at=observed_at
            )

            duration_ms = int((time.monotonic() - started) * 1000)
            self._db.finish_fetch_log(
                log_id,
                status="ok",
                http_status=200,
                rows_relevant=len(parsed),
                conjunctions_new=new_count,
                conjunctions_updated=updated_count,
                bytes_downloaded=result.bytes_downloaded,
                duration_ms=duration_ms,
            )
            logger.info(
                "Fetch cycle ok: %d relevant (%d new, %d updated) in %d ms.",
                len(parsed), new_count, updated_count, duration_ms,
            )
            return CycleReport(
                status="ok",
                http_status=200,
                rows_relevant=len(parsed),
                conjunctions_new=new_count,
                conjunctions_updated=updated_count,
                bytes_downloaded=result.bytes_downloaded,
                duration_ms=duration_ms,
            )

        except SocratesError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._db.finish_fetch_log(
                log_id, status="error", duration_ms=duration_ms, error=str(exc)
            )
            logger.error("Fetch cycle failed: %s", exc)
            return CycleReport(status="error", duration_ms=duration_ms, error=str(exc))
        except Exception as exc:  # never let a cycle crash the host process
            duration_ms = int((time.monotonic() - started) * 1000)
            self._db.finish_fetch_log(
                log_id, status="error", duration_ms=duration_ms, error=repr(exc)
            )
            logger.exception("Unexpected error during fetch cycle.")
            return CycleReport(status="error", duration_ms=duration_ms, error=repr(exc))
