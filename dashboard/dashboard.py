"""Bonus visualization dashboard (Streamlit).

Queries the Conjunction Tracker REST API and visualises upcoming/historical
conjunctions by **time** and **severity**, and -- the headline bonus feature --
maps each conjunction's **location**: the sub-satellite ground point of the
monitored Pixxel satellite at the Time of Closest Approach (TCA).

SOCRATES gives us TCA and the close-approach geometry but not a ground
coordinate, so we derive location by propagating the satellite's orbit (TLE via
CelesTrak GP API) to the TCA using SGP4 (skyfield). If TLEs cannot be fetched
(e.g. offline), the map section degrades gracefully and the rest still works.

Run:  streamlit run dashboard.py     (set API_BASE_URL, default http://localhost:8000)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
GP_URL = "https://celestrak.org/NORAD/elements/gp.php"


def fmt_utc(iso_utc) -> str:
    """Render a UTC ISO timestamp compactly, e.g. '2026-06-22 12:52 UTC'.

    UTC is the operational standard for spaceflight (SOCRATES, CDMs, TLE epochs
    are all UTC), so the dashboard displays UTC throughout. Returns '—' for
    missing values.
    """
    if not isinstance(iso_utc, str) or not iso_utc:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_utc)
    except ValueError:
        return iso_utc[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC"

st.set_page_config(page_title="Pixxel FDS · Conjunction Tracker", layout="wide")


# --------------------------------------------------------------------------- #
#  API access helpers
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=60)
def api_get(path: str, params: dict | None = None) -> dict | list:
    resp = requests.get(f"{API_BASE_URL}{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fmt_choice(df: pd.DataFrame, cid: str) -> str:
    """Label for the drill-down selectbox: 'SAT vs THREAT @ TCA'."""
    row = df.loc[df["id"] == cid].iloc[0]
    return f"{row['satellite']} vs {row['threat']} @ {row['tca_utc'][:16]}"


def conjunctions_df(time_filter: str) -> pd.DataFrame:
    data = api_get("/conjunctions", {"time_filter": time_filter, "limit": 1000})
    rows = []
    for c in data.get("results", []):
        risk = c.get("risk") or {}
        trend = c.get("trend") or {}
        rows.append(
            {
                "id": c["id"],
                "trend_direction": trend.get("direction", "new"),
                "trend_note": trend.get("note", ""),
                "tca_utc": c["tca_utc"],
                "tca": datetime.fromisoformat(c["tca_utc"]),
                "satellite": c["satellite"]["name"],
                "satellite_id": c["satellite"]["norad_id"],
                "threat": c["threat"]["name"],
                "threat_id": c["threat"]["norad_id"],
                "threat_is_debris": c["threat"]["is_debris"],
                "miss_distance_km": c["miss_distance_km"],
                "relative_speed_km_s": c["relative_speed_km_s"],
                "max_probability": c["max_probability"],
                "hours_to_tca": c["hours_to_tca"],
                "screening_count": c["screening_count"],
                "risk_level": risk.get("level", "green"),
                "risk_label": risk.get("label", "CLEAR"),
                "risk_score": risk.get("score", 0.0),
                "risk_drivers": "; ".join(risk.get("drivers", [])),
                "decision_by_utc": risk.get("decision_by_utc"),
                "confidence": risk.get("confidence", "high"),
                "data_age_days": risk.get("data_age_days"),
            }
        )
    return pd.DataFrame(rows)


_RISK_EMOJI = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
_TREND_BADGE = {
    "worsening": "🔺 worsening",
    "improving": "🔻 improving",
    "stable": "➡️ stable",
    "new": "🆕 new",
}


def render_triage(df: pd.DataFrame) -> None:
    """The Flight Dynamics action board: verdict first, chart-reading second."""
    st.subheader("🚦 Action board — what needs attention", anchor="action-board")
    n_red = int((df["risk_level"] == "red").sum())
    n_yellow = int((df["risk_level"] == "yellow").sum())
    n_green = int((df["risk_level"] == "green").sum())

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("🔴 ACTION", n_red)
    k2.metric("🟡 MONITOR", n_yellow)
    k3.metric("🟢 CLEAR", n_green)
    deadlines = df.loc[df["decision_by_utc"].notna(), "decision_by_utc"]
    k4.metric(
        "Next decision by (UTC)",
        fmt_utc(deadlines.min()) if not deadlines.empty else "—",
    )

    actionable = df[df["risk_level"].isin(["red", "yellow"])].copy()
    if actionable.empty:
        st.success("No RED or YELLOW conjunctions in this window. Fleet is clear. ✅")
        return

    actionable = actionable.sort_values("risk_score", ascending=False)
    actionable["⚠"] = actionable["risk_level"].map(_RISK_EMOJI)
    actionable["trend"] = actionable["trend_direction"].map(_TREND_BADGE)
    actionable["Pc"] = actionable["max_probability"].map(
        lambda p: f"{p:.1e}" if p else "—"
    )
    actionable["miss (km)"] = actionable["miss_distance_km"].round(3)
    actionable["TCA (h)"] = actionable["hours_to_tca"].round(1)
    actionable["decision by"] = actionable["decision_by_utc"].map(fmt_utc)
    actionable["conf."] = actionable["confidence"]
    view = actionable[
        ["⚠", "risk_label", "trend", "satellite", "threat", "Pc", "miss (km)",
         "TCA (h)", "decision by", "conf.", "risk_drivers"]
    ].rename(columns={"risk_label": "verdict", "risk_drivers": "why"})
    st.dataframe(view, use_container_width=True, hide_index=True)
    st.caption(
        "**trend** = how the event has moved across SOCRATES screenings: "
        "🔺 worsening (closing in) · 🔻 improving (opening up) · ➡️ stable · 🆕 new. "
        "A worsening YELLOW deserves attention before a stable RED."
    )
    if (actionable["confidence"] == "low").any():
        st.caption(
            "⚠️ Rows marked low confidence rely on stale orbit data (old TLE) — "
            "pull a fresh CDM before acting on the probability."
        )


# --------------------------------------------------------------------------- #
#  Orbit propagation for the location map
# --------------------------------------------------------------------------- #
_TLE_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".tle_cache")


def _tle_cache_path(norad_id: int) -> str:
    return os.path.join(_TLE_CACHE_DIR, f"{norad_id}.tle")


def _read_cached_tle(norad_id: int) -> tuple[str, str] | None:
    """Last-known-good TLE persisted to disk, so a CelesTrak outage or
    rate-limit cannot break the location map once we have fetched at least once.
    """
    path = _tle_cache_path(norad_id)
    if not os.path.exists(path):
        return None
    try:
        lines = [ln.strip() for ln in open(path, encoding="utf-8") if ln.strip()]
        if len(lines) >= 2 and lines[0].startswith("1 ") and lines[1].startswith("2 "):
            return lines[0], lines[1]
    except OSError:
        return None
    return None


def _write_cached_tle(norad_id: int, l1: str, l2: str) -> None:
    try:
        os.makedirs(_TLE_CACHE_DIR, exist_ok=True)
        with open(_tle_cache_path(norad_id), "w", encoding="utf-8") as fh:
            fh.write(f"{l1}\n{l2}\n")
    except OSError:
        pass


@st.cache_data(ttl=3600)
def fetch_tle(norad_id: int) -> tuple[str, str] | None:
    """TLE for a satellite, preferring a live CelesTrak GP fetch but falling
    back to the on-disk cache when CelesTrak is unreachable/rate-limited.
    """
    try:
        resp = requests.get(
            GP_URL,
            params={"CATNR": norad_id, "FORMAT": "TLE"},
            headers={"User-Agent": "Pixxel-FDS-Dashboard/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        lines = [ln.strip() for ln in resp.text.splitlines() if ln.strip()]
        # GP TLE = name line + 2 element lines.
        for i in range(len(lines) - 2):
            if lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
                _write_cached_tle(norad_id, lines[i + 1], lines[i + 2])
                return lines[i + 1], lines[i + 2]
    except requests.RequestException:
        pass  # fall through to cache
    return _read_cached_tle(norad_id)


@st.cache_resource
def _timescale():
    # builtin=True avoids any network download of time data -> robust offline.
    from skyfield.api import load

    return load.timescale(builtin=True)


def subsatellite_point(norad_id: int, tca: datetime) -> tuple[float, float] | None:
    """Return (lat, lon) of the satellite's ground point at TCA, or None."""
    tle = fetch_tle(norad_id)
    if tle is None:
        return None
    try:
        from skyfield.api import EarthSatellite, wgs84

        ts = _timescale()
        sat = EarthSatellite(tle[0], tle[1], str(norad_id), ts)
        if tca.tzinfo is None:
            tca = tca.replace(tzinfo=timezone.utc)
        t = ts.from_datetime(tca)
        geo = wgs84.geographic_position_of(sat.at(t))
        return float(geo.latitude.degrees), float(geo.longitude.degrees)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Section navbar
# --------------------------------------------------------------------------- #
_NAV_SECTIONS = [
    ("action-board", "🚦 Action"),
    ("over-time", "⏱️ Time"),
    ("risk-matrix", "⚠️ Risk"),
    ("by-satellite", "📊 By satellite"),
    ("map", "🌍 Map"),
    ("drill-down", "🔬 Drill-down"),
    ("list", "📋 List"),
]


def render_navbar() -> None:
    """A sticky row of in-page jump links to each dashboard section.

    Streamlit auto-creates a scroll anchor for every section header (we set the
    slugs explicitly via `anchor=`), so a plain `#slug` link scroll-jumps to it.
    """
    links = " ".join(
        f"<a class='nav-link' href='#{slug}'>{label}</a>"
        for slug, label in _NAV_SECTIONS
    )
    st.markdown(
        """
        <style>
        .section-nav {
            position: sticky; top: 0; z-index: 999;
            display: flex; flex-wrap: nowrap; align-items: center;
            justify-content: space-between; gap: .5rem;
            width: 100%; overflow-x: auto;
            padding: .6rem 1rem; margin-bottom: .75rem;
            background: var(--background-color, #0e1117);
            border: 1px solid rgba(128,128,128,.25); border-radius: 10px;
            box-shadow: 0 2px 6px rgba(0,0,0,.25);
        }
        .section-nav .nav-link {
            flex: 1 1 0; text-align: center; white-space: nowrap;
            text-decoration: none; font-size: .92rem; font-weight: 600;
            padding: .35rem .6rem; border-radius: 8px;
            color: inherit; border: 1px solid rgba(128,128,128,.35);
            transition: all .12s ease-in-out;
        }
        .section-nav .nav-link:hover {
            border-color: #ff4b4b; color: #ff4b4b;
            background: rgba(255,75,75,.08);
        }
        </style>
        """
        + f"<div class='section-nav'>{links}</div>",
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
#  UI
# --------------------------------------------------------------------------- #
st.title("🛰️ Pixxel FDS · Conjunction Tracker")
st.caption(f"Connected to API: `{API_BASE_URL}`")

with st.sidebar:
    st.header("Controls")
    time_filter = st.radio("Time window", ["upcoming", "historical", "all"], index=0)
    st.divider()
    if st.button("🔄 Trigger server refresh"):
        try:
            r = requests.post(f"{API_BASE_URL}/refresh", timeout=120).json()
            st.success(f"Refresh: {r['status']} — {r.get('detail','')}")
            api_get.clear()
        except requests.RequestException as e:
            st.error(f"Refresh failed: {e}")
    st.caption("Server fetches on its own schedule; this is a manual nudge.")

# Connection guard.
try:
    health = api_get("/health")
except requests.RequestException as e:
    st.error(f"Cannot reach the API at {API_BASE_URL}. Is it running?\n\n{e}")
    st.stop()

df = conjunctions_df(time_filter)

# --- KPIs ---
c1, c2, c3, c4 = st.columns(4)
c1.metric("Conjunctions", len(df))
c2.metric("Closest miss (km)", f"{df['miss_distance_km'].min():.3f}" if not df.empty else "—")
c3.metric(
    "Highest probability",
    f"{df['max_probability'].max():.2e}" if not df.empty else "—",
)
c4.metric("Monitored satellites", len(health["monitored_satellite_ids"]))

if df.empty:
    st.info("No conjunctions for this filter yet. Try 'all', or trigger a refresh.")
    st.stop()

# --- Section navbar (sticky jump links) -------------------------------------
render_navbar()

# --- Action board (verdict first) -------------------------------------------
render_triage(df)
st.divider()

# --- Time + severity: the core "time in a plot" view ---
st.subheader("⏱️ Conjunctions over time", anchor="over-time")
fig_time = px.scatter(
    df,
    x="tca",
    y="miss_distance_km",
    color="satellite",
    size=df["relative_speed_km_s"].clip(lower=0.1),
    hover_data=["threat", "max_probability", "relative_speed_km_s", "screening_count"],
    labels={"tca": "Time of Closest Approach (UTC)", "miss_distance_km": "Miss distance (km)"},
)
fig_time.update_yaxes(autorange="reversed")  # closer (smaller) = higher = scarier
st.plotly_chart(fig_time, use_container_width=True)

# --- Risk matrix ---
col_a, col_b = st.columns(2)
with col_a:
    st.subheader("⚠️ Risk matrix", anchor="risk-matrix")
    fig_risk = px.scatter(
        df,
        x="miss_distance_km",
        y="max_probability",
        color="satellite",
        symbol="threat_is_debris",
        hover_data=["threat", "tca_utc"],
        log_y=True,
        labels={"miss_distance_km": "Miss distance (km)", "max_probability": "Max probability"},
    )
    st.plotly_chart(fig_risk, use_container_width=True)
with col_b:
    st.subheader("📊 By satellite", anchor="by-satellite")
    by_sat = df.groupby("satellite").size().reset_index(name="conjunctions")
    st.plotly_chart(
        px.bar(by_sat, x="satellite", y="conjunctions", color="satellite"),
        use_container_width=True,
    )

# --- Location map (bonus headline) ---
st.subheader("🌍 Conjunction locations (sub-satellite point at TCA)", anchor="map")
st.caption(
    "Ground position of the Pixxel satellite at the moment of closest approach, "
    "computed by propagating its TLE (CelesTrak GP) to TCA with SGP4."
)
with st.spinner("Propagating orbits to TCA…"):
    geo_rows = []
    for _, row in df.iterrows():
        pt = subsatellite_point(int(row["satellite_id"]), row["tca"])
        if pt is not None:
            geo_rows.append(
                {**row.to_dict(), "lat": pt[0], "lon": pt[1]}
            )
if geo_rows:
    gdf = pd.DataFrame(geo_rows)
    fig_map = px.scatter_geo(
        gdf,
        lat="lat",
        lon="lon",
        color="satellite",
        size=(1.0 / gdf["miss_distance_km"].clip(lower=0.05)),
        hover_name="threat",
        hover_data={"miss_distance_km": True, "tca_utc": True, "lat": ":.2f", "lon": ":.2f"},
        projection="natural earth",
    )
    st.plotly_chart(fig_map, use_container_width=True)
else:
    st.warning(
        "Could not compute locations (TLE source unreachable or skyfield "
        "unavailable). Time and severity views above are unaffected."
    )

# --- Prediction-evolution drill-down ---
st.subheader("🔬 Prediction evolution (drill-down)", anchor="drill-down")
st.caption("How the miss distance / probability for one event changed across screenings.")
sel = st.selectbox(
    "Select a conjunction",
    df["id"],
    format_func=lambda cid: _fmt_choice(df, cid),
)
detail = api_get(f"/conjunctions/{sel}")
obs = pd.DataFrame(detail["observations"])
if len(obs) >= 1:
    obs["observed_at"] = pd.to_datetime(obs["observed_at_utc"])
    fig_evo = px.line(
        obs.sort_values("observed_at"),
        x="observed_at",
        y="miss_distance_km",
        markers=True,
        labels={"observed_at": "Screening time (UTC)", "miss_distance_km": "Miss distance (km)"},
    )
    st.plotly_chart(fig_evo, use_container_width=True)
    st.caption(f"Screenings recorded: {len(obs)}")

# --- Raw table ---
st.subheader("📋 Conjunction list", anchor="list")
# Display-only formatting: sort by miss distance (numeric), then prettify the
# TCA timestamp on a copy. The underlying data / API stay full-precision ISO-8601.
conj_list = (
    df.sort_values("miss_distance_km")[
        ["tca_utc", "satellite", "threat", "threat_is_debris",
         "miss_distance_km", "relative_speed_km_s", "max_probability",
         "hours_to_tca", "screening_count"]
    ].copy()
)
conj_list["tca_utc"] = conj_list["tca_utc"].map(fmt_utc)
conj_list = conj_list.rename(columns={"tca_utc": "tca (UTC)"})
st.dataframe(conj_list, use_container_width=True, hide_index=True)
