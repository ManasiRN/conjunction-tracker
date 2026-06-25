"""HTTP client for the CelesTrak SOCRATES bulk conjunction feed.

CelesTrak is a free service and will block IP addresses that query it too
aggressively. This client is built to be a *good neighbour*:

* **One bulk download per cycle.** We fetch the whole-catalogue CSV once and
  filter locally, instead of issuing one request per satellite.
* **Conditional GET.** We remember the server's ``ETag`` / ``Last-Modified``
  and send ``If-None-Match`` / ``If-Modified-Since`` on the next request. If the
  data has not changed, the server replies ``304 Not Modified`` with no body --
  near-zero bandwidth and load. (SOCRATES only recomputes ~3x/day.)
* **Identifying User-Agent.** CelesTrak asks clients to identify themselves.
* **Bounded retries with exponential backoff + jitter**, so a transient blip
  does not turn into a hammering retry storm.
* **A sane timeout**, so a hung connection cannot stall the scheduler forever.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

from .config import Settings

logger = logging.getLogger(__name__)


class SocratesError(RuntimeError):
    """Raised when the SOCRATES feed cannot be retrieved after all retries."""


@dataclass
class FetchResult:
    """Outcome of a single fetch attempt."""

    status: str  # "ok" | "not_modified" | "error"
    http_status: int | None = None
    body: str | None = None  # CSV text, present only when status == "ok"
    etag: str | None = None
    last_modified: str | None = None
    bytes_downloaded: int = 0
    error: str | None = None


class SocratesClient:
    """Thin, polite wrapper around the SOCRATES bulk CSV endpoint.

    The caller is responsible for *when* to fetch (the scheduler). This class
    only concerns itself with fetching *correctly and politely* when asked.
    """

    def __init__(self, settings: Settings):
        self._url = settings.socrates_source_url
        self._timeout = settings.http_timeout_seconds
        self._max_retries = settings.http_max_retries
        self._backoff_base = settings.http_backoff_base_seconds
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": settings.http_user_agent,
                "Accept": "text/csv, text/plain, */*",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        # Validator cache for conditional GET, updated after each 200/304.
        self._etag: str | None = None
        self._last_modified: str | None = None

    def fetch(self) -> FetchResult:
        """Fetch the CSV, honouring conditional-GET validators.

        Returns a :class:`FetchResult`. Raises :class:`SocratesError` only if
        every retry attempt failed with a transport/5xx error.
        """
        headers: dict[str, str] = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        last_error: str | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.get(
                    self._url, headers=headers, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_error = f"transport error: {exc}"
                logger.warning(
                    "SOCRATES fetch attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_retries + 1,
                    last_error,
                )
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 304:
                logger.info("SOCRATES data unchanged (304 Not Modified).")
                return FetchResult(
                    status="not_modified",
                    http_status=304,
                    etag=self._etag,
                    last_modified=self._last_modified,
                )

            if resp.status_code == 200:
                # Cache validators for the next conditional request.
                self._etag = resp.headers.get("ETag", self._etag)
                self._last_modified = resp.headers.get(
                    "Last-Modified", self._last_modified
                )
                body = resp.text
                logger.info(
                    "SOCRATES fetch ok: %d bytes (ETag=%s).",
                    len(body),
                    self._etag,
                )
                return FetchResult(
                    status="ok",
                    http_status=200,
                    body=body,
                    etag=self._etag,
                    last_modified=self._last_modified,
                    bytes_downloaded=len(resp.content),
                )

            # 429 (rate limited) / 5xx -> retry with backoff. 4xx -> give up.
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code == 429 or resp.status_code >= 500:
                logger.warning(
                    "SOCRATES returned %s (attempt %d/%d); backing off.",
                    last_error,
                    attempt + 1,
                    self._max_retries + 1,
                )
                self._sleep_backoff(attempt, retry_after=resp.headers.get("Retry-After"))
                continue

            # Non-retryable client error.
            return FetchResult(
                status="error", http_status=resp.status_code, error=last_error
            )

        raise SocratesError(
            f"Failed to fetch SOCRATES feed after "
            f"{self._max_retries + 1} attempts: {last_error}"
        )

    def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        """Exponential backoff with light jitter; honour Retry-After if given.

        Jitter is derived from the attempt number (not a clock/RNG) to keep the
        behaviour deterministic and testable while still de-synchronising
        retries across processes.
        """
        if retry_after:
            try:
                delay = float(retry_after)
                logger.info("Honouring Retry-After: %.1fs", delay)
                time.sleep(delay)
                return
            except ValueError:
                pass  # Retry-After may be an HTTP-date; fall through to backoff.

        delay = self._backoff_base * (2**attempt)
        jitter = (attempt % 3) * 0.5  # small, bounded, deterministic
        time.sleep(delay + jitter)
