# Dashboard Guide — how to read every section

A plain-English walkthrough of the Conjunction Tracker dashboard, written so it
can be explained to anyone in a minute. Open it at <http://localhost:8501>.

## The one-line summary

> The service watches our Pixxel satellites, automatically pulls a list of objects
> that will fly close to them from a free space-tracking service (CelesTrak
> SOCRATES), works out how dangerous each close approach is, and shows it so a
> flight-dynamics analyst instantly knows what needs action.

## Two numbers behind everything

Every close approach ("conjunction") is judged on two independent measurements:

| Term | What it means | Example |
|------|----------------|---------|
| **Miss distance** | How close the two objects come, in km. Smaller = more dangerous. | `0.205` km = 205 metres |
| **Pc (collision probability)** | Chance of an actual collision, in scientific notation. More negative exponent = safer. | `1.1e-04` = 0.00011 ≈ 1-in-9,000 |

These come straight from SOCRATES, which propagates each object's orbit to the
moment of closest approach and reports the geometry.

---

## 🚦 Action board — "what needs attention"

The verdict-first view. The four boxes at the top are the headline:

| Box | Meaning |
|-----|---------|
| 🔴 **ACTION** | Count of **RED** events — real threats; request a CDM / plan a maneuver |
| 🟡 **MONITOR** | Count of **YELLOW** events — credible; keep watching |
| 🟢 **CLEAR** | Count of **GREEN** events — no concern |
| **Next decision by (UTC)** | The soonest deadline by which a decision must be made |

> Why is CLEAR often 0? The list only contains approaches SOCRATES flagged as
> close enough to screen — so everything shown is at least YELLOW. Truly safe
> passes never appear.

### How RED / YELLOW / GREEN is decided

An event takes the **worse** of two checks (conservative, the right bias for an
early-warning tool):

| | RED if | YELLOW if | GREEN if |
|---|--------|-----------|----------|
| **Pc** | ≥ 1e-4 | ≥ 1e-7 | below |
| **Miss** | < 1 km | < 5 km | ≥ 5 km |

### Reading one row

Columns: **risk · verdict · trend · satellite · threat · Pc · miss(km) · TCA(h) · decision-by · confidence · why**

Example row:
`🔴 ACTION · 🆕 new · FFLY02 · LEMUR-2-WILSON · 1.1e-04 · 0.205 · 7.4 · 2026-06-22 12:52 UTC · low · Pc 1.1e-04 (red); miss 0.205 km (red); stale data`

- **FFLY02** (our satellite) has a close approach with **LEMUR-2-WILSON** (the threat).
- **Pc 1.1e-04** ≥ 1e-4 → red on probability; **miss 0.205 km** < 1 km → red on distance → **RED**.
- **TCA(h) = 7.4** → closest approach is in 7.4 hours.
- **decision-by 2026-06-22 12:52 UTC** = TCA minus the 24 h planning lead — the latest sensible time to decide on a maneuver.
- **confidence = low** because the orbit data (TLE) is stale → *get a fresh CDM before acting*.

### The `trend` column (across repeated screenings)

SOCRATES re-checks ~3×/day, so each event builds a history:

| Badge | Meaning |
|-------|---------|
| 🔺 worsening | getting closer / probability rising |
| 🔻 improving | opening up / probability falling |
| ➡️ stable | little change |
| 🆕 new | only seen once so far — no trend yet |

> Operational point: **a worsening YELLOW can deserve attention before a stable RED.**

---

## ⏱️ Conjunctions over time

A timeline of the threats.

- **X-axis = TCA (time of closest approach)** — *when* each event happens.
- **Y-axis = miss distance (km)**, **flipped** so closer (scarier) events sit higher.
- **Colour = which satellite**; **dot size = relative speed**.

> Why do the dates span the next several days? SOCRATES only looks **~7 days
> ahead**, so all upcoming TCAs fall within the coming week. The range shifts
> forward each day — it's data, not a fixed window.

---

## ⚠️ Risk matrix

The two danger measures plotted against each other.

- **X-axis = miss distance (km)** — geometric closeness.
- **Y-axis = Pc (collision probability)**, on a **log scale** (Pc spans tiny values).
- **Bottom-left corner = close AND high-probability = most dangerous.**

It exists because distance and probability don't always agree — this shows which
events are bad on *both* axes at once.

## 📊 By satellite

A simple bar chart: **how many conjunctions each satellite has**. Tells you at a
glance which Firefly is most exposed right now.

---

## 🌍 Conjunction locations (sub-satellite point at TCA)

A world map showing **where over the Earth** the satellite is at the moment of
each close approach.

**How it is computed:** SOCRATES gives the *time* of closest approach but not a
ground position. So the dashboard downloads the satellite's **TLE** (orbital
parameters) from CelesTrak, uses **SGP4** (the standard orbit model, via
`skyfield`) to fly the orbit to the TCA, and converts that to a
**latitude/longitude** — the point on Earth directly beneath the satellite.

**Why it's useful:** geographic context for each event. If TLEs can't be
downloaded, the map hides itself and the rest of the dashboard still works.

---

## 🔬 Prediction evolution (drill-down)

Pick **one** event; see **how its prediction changed across screenings**.

- **X-axis = screening time** (each time SOCRATES re-ran).
- **Y-axis = miss distance** for that event at each screening.

This is the `trend` as a chart — is the event getting more or less dangerous as
TCA approaches? You select one event at a time because each has its own history;
the dropdown lists **all** conjunctions, not just one satellite.

---

## 📋 Conjunction list

The full table of every conjunction.

**Why isn't TCA in time order?** The table is sorted by **miss distance (closest
first)**, not by time — because the analyst's first question is *"what is the most
dangerous one?"*, not *"what is next?"*. Row order = danger, not clock.

**Why does it show future dates if we must store past data?** Both are stored.
The dashboard **defaults to the "upcoming" time window** (future TCAs) because
that is the operationally important view. Past events are kept too — switch the
**Time window** selector in the sidebar to **historical** or **all** to see them.
(The API exposes the same via `?time_filter=historical|all`.)

Each row shows: TCA (UTC) · satellite · threat · is-debris · miss distance ·
relative speed · Pc · hours-to-TCA · screening count.

---

## Upcoming vs historical (the data-retention point)

| Time window | Meaning |
|-------------|---------|
| **upcoming** | TCA is in the future — the default operational view |
| **historical** | TCA is in the past — the archive (assignment requires this) |
| **all** | both combined |

"Upcoming vs historical" is *derived* from TCA versus now, never a stored flag,
so it is always correct without a background job flipping statuses. Past events
remain in the database as the historical archive even after SOCRATES drops them
from its 7-day window.
