"""Parse the SOCRATES CSV and filter it down to our monitored satellites.

SOCRATES bulk CSV header (RFC 4180, one row per conjunction):

    NORAD_CAT_ID_1, OBJECT_NAME_1, DSE_1,
    NORAD_CAT_ID_2, OBJECT_NAME_2, DSE_2,
    TCA, TCA_RANGE, TCA_RELATIVE_SPEED, MAX_PROB, DILUTION

Key field meanings (from CelesTrak's format doc):
    DSE_n  -- days since each object's orbital-element epoch (data staleness)
    TCA    -- time of closest approach, UTC
    TCA_RANGE          -- miss distance at TCA, km
    TCA_RELATIVE_SPEED -- relative velocity magnitude at TCA, km/s
    MAX_PROB           -- maximum collision probability
    DILUTION           -- dilution threshold (uncertainty), km

Design notes
------------
* A conjunction is a *pair*. Our satellite may appear as object 1 **or** object
  2, so we filter on both ID columns.
* We **normalise** each record so the pair is stored in a stable order
  (lower NORAD ID first). This makes the de-duplication key independent of the
  column order SOCRATES happens to emit, so the same physical event is
  recognised across re-screenings.
* Object names carry an operational-status suffix in brackets, e.g.
  ``"FENGYUN 1C DEB [-]"``. We split that into a clean name + a status code.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

EXPECTED_HEADER = [
    "NORAD_CAT_ID_1",
    "OBJECT_NAME_1",
    "DSE_1",
    "NORAD_CAT_ID_2",
    "OBJECT_NAME_2",
    "DSE_2",
    "TCA",
    "TCA_RANGE",
    "TCA_RELATIVE_SPEED",
    "MAX_PROB",
    "DILUTION",
]

# CelesTrak ops-status codes appended to object names in [brackets].
_STATUS_MAP = {
    "+": "operational",
    "-": "nonoperational",
    "P": "partially_operational",
    "B": "backup",
    "S": "spare",
    "X": "extended_mission",
    "D": "decayed",
    "?": "unknown",
}

_STATUS_RE = re.compile(r"^(?P<name>.*?)\s*\[(?P<code>[^\]]*)\]\s*$")


@dataclass(frozen=True)
class ParsedConjunction:
    """A single conjunction event, normalised and ready to persist.

    Objects are stored with the lower NORAD ID as ``norad_id_1`` so the
    identity key is stable regardless of SOCRATES column ordering.
    """

    conjunction_id: str  # deterministic hash of (id_low, id_high, TCA-to-minute)
    norad_id_1: int
    name_1: str
    status_1: str
    dse_1: float
    norad_id_2: int
    name_2: str
    status_2: str
    dse_2: float
    tca_utc: str  # ISO-8601, UTC
    miss_distance_km: float
    relative_speed_km_s: float
    max_probability: float
    dilution_km: float


def _split_name_status(raw: str) -> tuple[str, str]:
    """Split ``"FFLY01 [+]"`` -> ``("FFLY01", "operational")``."""
    raw = (raw or "").strip()
    m = _STATUS_RE.match(raw)
    if not m:
        return raw, "unknown"
    name = m.group("name").strip()
    code = m.group("code").strip()
    return name, _STATUS_MAP.get(code, "unknown")


def parse_tca(raw: str) -> datetime:
    """Parse a SOCRATES TCA string (``YYYY-MM-DD HH:MM:SS[.ffffff]``) as UTC."""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised TCA format: {raw!r}")


def _make_conjunction_id(id_low: int, id_high: int, tca: datetime) -> str:
    """Stable identity for a conjunction event.

    TCA is rounded to the minute: the same physical event keeps essentially the
    same TCA across re-screenings (sub-minute drift), while distinct events
    between the same pair are far enough apart in time to differ at minute
    resolution.
    """
    key = f"{id_low}-{id_high}-{tca:%Y%m%dT%H%M}Z"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def parse_and_filter(csv_text: str, monitored_ids: set[int]) -> list[ParsedConjunction]:
    """Parse the SOCRATES CSV and keep only conjunctions touching ``monitored_ids``.

    Malformed individual rows are skipped (and counted in the log) rather than
    failing the whole batch -- a single bad row should never blind the FDS to
    every other conjunction.
    """
    reader = csv.reader(io.StringIO(csv_text))
    try:
        header = next(reader)
    except StopIteration:
        logger.error("SOCRATES CSV was empty.")
        return []

    if [h.strip() for h in header] != EXPECTED_HEADER:
        # Non-fatal: log loudly so a silent upstream format change is noticed,
        # but proceed positionally since the column order is what we rely on.
        logger.warning(
            "Unexpected SOCRATES header; proceeding positionally. Got: %s", header
        )

    results: list[ParsedConjunction] = []
    skipped = 0
    total = 0
    for row in reader:
        total += 1
        if len(row) < 11:
            skipped += 1
            continue
        try:
            id1 = int(row[0])
            id2 = int(row[3])
            if id1 not in monitored_ids and id2 not in monitored_ids:
                continue  # not our satellite -> ignore

            name1, status1 = _split_name_status(row[1])
            name2, status2 = _split_name_status(row[4])
            dse1 = float(row[2])
            dse2 = float(row[5])
            tca = parse_tca(row[6])
            miss = float(row[7])
            relspeed = float(row[8])
            prob = float(row[9])
            dilution = float(row[10])

            # Normalise pair ordering by NORAD ID (carry each object's fields).
            obj1 = (id1, name1, status1, dse1)
            obj2 = (id2, name2, status2, dse2)
            if id1 > id2:
                obj1, obj2 = obj2, obj1

            cid = _make_conjunction_id(obj1[0], obj2[0], tca)
            results.append(
                ParsedConjunction(
                    conjunction_id=cid,
                    norad_id_1=obj1[0],
                    name_1=obj1[1],
                    status_1=obj1[2],
                    dse_1=obj1[3],
                    norad_id_2=obj2[0],
                    name_2=obj2[1],
                    status_2=obj2[2],
                    dse_2=obj2[3],
                    tca_utc=tca.isoformat(),
                    miss_distance_km=miss,
                    relative_speed_km_s=relspeed,
                    max_probability=prob,
                    dilution_km=dilution,
                )
            )
        except (ValueError, IndexError) as exc:
            skipped += 1
            logger.debug("Skipping malformed row %s: %s", row, exc)

    logger.info(
        "Parsed SOCRATES feed: %d total rows, %d relevant to monitored sats, "
        "%d skipped (malformed).",
        total,
        len(results),
        skipped,
    )
    return results
