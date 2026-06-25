"""Configuration for the Conjunction Tracker.

The service is configured in two deliberately separate layers:

1. **Operational settings** (`Settings`) come from environment variables.
   These are deployment concerns (URLs, intervals, paths, credentials) and,
   per the assignment, must never be hardcoded. Defaults are provided so the
   service runs out-of-the-box for evaluation.

2. **Mission configuration** (`load_satellites`) -- *which* satellites we
   monitor -- lives in a human-editable YAML file. Onboarding a newly launched
   satellite is a one-line edit with no code change. This is what makes the
   service forward-compatible with future Pixxel launches.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Operational settings, sourced from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Data source ---
    socrates_source_url: str = Field(
        default="https://celestrak.org/SOCRATES/sort-minRange.csv",
        description="Bulk SOCRATES conjunction CSV (whole catalogue, one download).",
    )

    # --- Polite-client / anti-ban controls ---
    fetch_interval_hours: float = Field(default=6.0, gt=0)
    min_fetch_interval_minutes: float = Field(default=60.0, ge=0)
    http_user_agent: str = Field(
        default="Pixxel-FDS-ConjunctionTracker/1.0 (flightdynamics@pixxel.co.in)"
    )
    http_timeout_seconds: float = Field(default=60.0, gt=0)
    http_max_retries: int = Field(default=4, ge=0)
    http_backoff_base_seconds: float = Field(default=5.0, gt=0)
    fetch_on_startup: bool = Field(default=True)

    # --- Risk classification (Flight Dynamics decision thresholds) ---
    # Pc (probability of collision) is the primary maneuver-decision driver;
    # miss distance is an independent geometric backstop. Defaults follow common
    # operational practice (e.g. NASA CARA): red at Pc>=1e-4, yellow at >=1e-7.
    pc_red_threshold: float = Field(default=1e-4, gt=0)
    pc_yellow_threshold: float = Field(default=1e-7, gt=0)
    miss_red_km: float = Field(default=1.0, gt=0)
    miss_yellow_km: float = Field(default=5.0, gt=0)
    # TLE age (days since epoch) beyond which a prediction is flagged low-confidence.
    stale_data_days: float = Field(default=3.0, gt=0)
    # Planning lead time: a maneuver decision must be made this long before TCA.
    maneuver_lead_hours: float = Field(default=24.0, gt=0)
    # Health reports "degraded" once the last successful SOCRATES contact is
    # older than this many fetch intervals -- stale awareness == no awareness.
    health_stale_factor: float = Field(default=2.0, gt=1)

    # --- Storage ---
    database_path: str = Field(default="./data/conjunctions.db")

    # --- Mission config file ---
    satellite_config_path: str = Field(default="./config/satellites.yaml")

    # --- API server ---
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    # --- Logging ---
    log_level: str = Field(default="INFO")

    def risk_thresholds(self):
        """Build the (pure) RiskThresholds used by the classifier.

        Imported lazily so the risk module stays free of any config dependency
        and remains trivially unit-testable on its own.
        """
        from .risk import RiskThresholds

        return RiskThresholds(
            pc_red=self.pc_red_threshold,
            pc_yellow=self.pc_yellow_threshold,
            miss_red_km=self.miss_red_km,
            miss_yellow_km=self.miss_yellow_km,
            stale_days=self.stale_data_days,
            lead_time_hours=self.maneuver_lead_hours,
        )

    @property
    def fetch_interval_seconds(self) -> float:
        return self.fetch_interval_hours * 3600.0

    @property
    def min_fetch_interval_seconds(self) -> float:
        return self.min_fetch_interval_minutes * 60.0


class MonitoredSatellite(BaseModel):
    """One satellite we watch for conjunctions, as declared in the YAML config."""

    norad_id: int = Field(..., gt=0, description="NORAD catalog number.")
    name: str | None = Field(default=None, description="Operator short name.")
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so settings are parsed from the environment only once."""
    return Settings()


def load_satellites(path: str | None = None) -> list[MonitoredSatellite]:
    """Load and validate the monitored-satellite list from YAML.

    Raises a clear error if the file is missing, malformed, or empty, because
    a service with no satellites configured is almost always a deployment
    mistake we want to fail loudly on at startup.
    """
    cfg_path = Path(path or get_settings().satellite_config_path)
    if not cfg_path.is_file():
        raise FileNotFoundError(f"Satellite config file not found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    entries = raw.get("satellites")
    if not entries:
        raise ValueError(
            f"No 'satellites' entries found in config file: {cfg_path}. "
            "Add at least one satellite with a 'norad_id'."
        )

    satellites = [MonitoredSatellite(**entry) for entry in entries]

    # Guard against duplicate NORAD IDs, a common copy-paste error.
    seen: set[int] = set()
    for sat in satellites:
        if sat.norad_id in seen:
            raise ValueError(f"Duplicate norad_id in config: {sat.norad_id}")
        seen.add(sat.norad_id)

    return satellites


def monitored_id_set(path: str | None = None) -> set[int]:
    """Convenience: just the set of NORAD IDs we monitor (used by the filter)."""
    return {sat.norad_id for sat in load_satellites(path)}
