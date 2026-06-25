"""Periodic, in-process scheduler that drives the fetch cycle.

We use APScheduler's ``BackgroundScheduler`` so the whole service -- scheduled
fetcher *and* REST API -- runs inside a single process and therefore a single
Docker container, as required.

The interval (default 6h) is matched to SOCRATES' ~3x/day recompute cadence:
frequent enough to never miss an update, infrequent enough to be a polite,
ban-proof client.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from .config import Settings
from .fetcher import Fetcher

logger = logging.getLogger(__name__)


class FetchScheduler:
    def __init__(self, settings: Settings, fetcher: Fetcher):
        self._settings = settings
        self._fetcher = fetcher
        self._scheduler = BackgroundScheduler(timezone="UTC")

    def start(self) -> None:
        interval = self._settings.fetch_interval_seconds
        self._scheduler.add_job(
            self._safe_run,
            trigger="interval",
            seconds=interval,
            id="socrates_fetch",
            # If a run overruns the interval, don't stack jobs; collapse missed
            # runs into one and allow a generous grace period.
            coalesce=True,
            max_instances=1,
            misfire_grace_time=int(min(interval, 3600)),
        )
        self._scheduler.start()
        logger.info(
            "Scheduler started: fetching every %.1f h.",
            self._settings.fetch_interval_hours,
        )

    def _safe_run(self) -> None:
        # The scheduler's own job is throttle-exempt: it *is* the cadence.
        self._fetcher.run_once(force=True)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped.")
