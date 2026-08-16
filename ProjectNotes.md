# Mapping India's Environmental Clearances — Project Notes

**A reproducible pipeline and interactive map of granted environmental, forest, and
coastal clearances from the Government of India's PARIVESH 2.0 portal.**

*Status: v1 built and verified. Document compiled 10 Aug 2026. Data snapshot: 8 Aug 2026.*

---

## Abstract

India's PARIVESH portal publishes every environmental (EC), forest (FC), wildlife (WL),
and coastal (CRZ) clearance, but the data is fragmented across an undocumented GIS
server, a metadata REST API, and thousands of committee-minutes PDFs — and is
effectively unexplorable by the public. This project builds an end-to-end, reproducible
pipeline that ingests **67,633 project boundaries** from PARIVESH 2.0, isolates the
**50,185 granted** proposals (EC + FC + CRZ), derives their areas and locations,
enriches them with proponent names, and renders them as a **self-contained interactive
map of India** (state choropleth → zoom/pan → per-project detail). The larger
contribution is methodological: a documented account of how noisy, semantically
ambiguous government data is, and a verification discipline for producing figures that
survive scrutiny. All headline counts reconcile exactly against the source layers.

---

## 1. Introduction

### 1.1 Goal
Produce a free, public, interactive map of every environmental-clearance project in
India, using open government data — and, in the process, establish exactly what that
data can and cannot support.

### 1.2 Scope of v1 (locked)
- **PARIVESH 2.0 only** (coverage begins ~Sept 2022; granted records span **2023–2026**).
  Legacy / PARIVESH 1.0 layers are out of scope for v1 — they are patch-level (one
  proposal can be hundreds of rows) and carry geometry defects.
- **Granted proposals only**, defined per clearance type (§3.2).
- **EC, FC, CRZ included; WL excluded** — wildlife outcomes are not recoverable from any
  public field (§5.4).

### 1.3 What changed vs. the original framing
The project began as a "can this data even be retrieved?" check and expanded into a
multi-day audit of the government's geospatial infrastructure. That audit is complete,
the dataset is validated, **and — unlike the earlier project state — the interactive
map now exists** (v1). This document supersedes the "map not yet built" framing.

---

## 2. Data Sources

| Source | What | Access | License |
|---|---|---|---|
| **PARIVESH 2.0 GIS server** (ArcGIS REST) | 67,633 project boundaries + sensitivity layers | Unauthenticated REST | Govt. of India (attribute) |
| **PARIVESH metadata API** | proposal metadata, proponent, status; committee-minutes indices | Unauthenticated REST | Govt. of India (attribute) |
| **PARIVESH committee-minutes** | EC/FC/WL agenda & MoM listings (2.0 and legacy) | Unauthenticated REST | Govt. of India (attribute) |
| **india-geodata (DataMeet Admin2)** | 36 state/UT boundaries | GitHub, EPSG:4326 | **CC0-1.0 / CC-BY-4.0** (permissive) |
| **PARIVESH Wildlife Technical FAQ** | WLC status vocabulary | Portal PDF | Govt. of India |

Exact endpoints and query parameters are in **Appendix A**.

**Coverage & freshness:** the 2.0 GIS layers grow daily; every figure here is a snapshot
as of the **8 Aug 2026** pull. The map is labelled accordingly.

---

## 3. Methodology

### 3.1 Ingestion (GIS)
Pulled the five 2.0 proposal layers from the ArcGIS `KML_Edit` MapServer (`outSR=4326`),
streaming to newline-delimited GeoJSON. Notable obstacles solved:
- **Broken TLS chain** — the server omits its intermediate CA; browsers compensate via
  AIA-chasing, OpenSSL-based tools fail with `CERTIFICATE_VERIFY_FAILED`. Fixed with
  Python `truststore`.
- **Four coordinate systems** across services (3857 / 4326 / 32643 / 3395) — normalised
  by always requesting `outSR=4326`.
- **ArcGIS returns errors as HTTP 200 with an `error` key** — must be checked, or a failed
  layer silently records as "complete, zero rows."
- **Geometry-heavy FC layer** times out at `maxRecordCount=2000` — mitigated with
  `geometryPrecision` and ID-batch fallbacks.

### 3.2 Definition of "granted" (per clearance type)
Filtered on the **`status_clean` text field** (never the `is_*_granted` booleans, which
are unreliable — see §6):

| Type | "Granted" = | Count | Grant-date field |
|---|---|---|---|
| EC  | "EC Granted", "Granted" | 42,242 | `granted_date` (ISO) |
| FC  | "Stage-II Accorded", "GRANTED" (**Stage-I excluded**) | 7,517 | `date_of_stage_ii_approval` (DD-MON-YYYY) |
| CRZ | "CRZ Granted" | 426 | `granted_date` (ISO) |
| WL  | *excluded* | — | all 609 are "DISPOSED" |

FC Stage-I is excluded because it is *in-principle / conditional*, not a completed
clearance. **v1 granted universe ≈ 50,185.**

### 3.3 Derived attributes
- **Area (hectares):** the `project_area` field is empty for EC/CRZ (populated only for
  FC), so area is **derived from geometry** — polygon area on the WGS84 ellipsoid via
  `pyproj.Geod`, converted to hectares.
- **Location:** each proposal reduced to its **centroid** (lon/lat) for point rendering;
  full polygon retained at source.
- **Grant date** taken per the §3.2 table (formats differ by type).

### 3.4 Proponent enrichment ("Path B")
The proponent/company name is **absent from the GIS layer**. It lives in the metadata
API (`nameOfUserAgency`). Rather than ~50k per-proposal calls, we used the bulk
`advanceSearchData?majorClearanceType={1,2,4}&state=<census code>` endpoint (~144 calls,
one per type × state), yielding **146,904 `proposalNo` → company mappings**, joined to
the proposals by proposal number.

### 3.5 Boundary preparation
DataMeet `Admin2` shapefile (36 states/UTs, EPSG:4326) → simplified geometry, normalised
join key, exported as a compact GeoJSON (~301 KB). Proposals joined to states by
**normalised state name** (the boundary file carries no LGD code); only one record
("Dummy State") failed to join.

### 3.6 Visualization
A **single self-contained HTML file, no external libraries**:
- **Base map:** inline SVG of state polygons under a custom **Mercator projection**
  (longitude and latitude kept in the same angular units — an early bug produced a
  flattened map when they weren't).
- **Choropleth:** per-state granted counts, sequential red ramp with quantile breaks,
  recomputed per filter.
- **Points:** granted proposals drawn on an overlaid **Canvas** (handles tens of
  thousands of dots), coloured by clearance type.
- **Interaction:** scroll-to-zoom + drag-to-pan over a shared transform (QGIS-style);
  **dots reveal when zoomed past a threshold**; click-a-state and a searchable dropdown
  are zoom shortcuts; hover a dot for project name, type, derived area, grant date, and
  company.
- **Tiny-UT markers:** UTs whose land area is < 0.06 % of India (Lakshadweep, Chandigarh,
  Delhi, Puducherry, DNH&DD) render as ringed markers anchored to their **largest
  enclave's centroid** (a bbox centroid put Puducherry inland across its 25 scattered
  parts).
- Served locally via `python -m http.server`; data fetched as sibling JSON files.

### 3.7 Output schema & how a point is placed

The map reads four derived files (all committed to git under `map_v1/`); the raw
`out/` layers are **not** committed (see the raw-vs-displayed table below).

**`proposals.json`** is keyed by state; each proposal is a compact *positional array*
(field names omitted to keep 50k rows small):

```
{ "GUJARAT": [ [70.227, 21.0494, 0, 2025, 1.0, "Shri Madhubhai Hirabhai Velani",
               "Mining of minerals", "SIA/GJ/MIN/501094/2024",
               "2025-02-02T19:31:52.392"], ... ], ... }
```

| Index | Field | Example |
|---|---|---|
| 0 | **longitude** | 70.227 |
| 1 | **latitude** | 21.0494 |
| 2 | type (0=EC, 1=FC, 2=CRZ) | 0 |
| 3 | year granted | 2025 |
| 4 | area (hectares) | 1.0 |
| 5 | project name | "Shri Madhubhai Hirabhai Velani" |
| 6 | category | "Mining of minerals" |
| 7 | proposal number | "SIA/GJ/MIN/501094/2024" |
| 8 | grant date | "2025-02-02T19:31:52.392" |

The **company name is not in the row** — it is joined at display time from
`company_by_pno.json` using index 7 (the proposal number).

**How a dot is placed on the map.** Each clearance is really a *polygon* (the project
boundary). `prep_proposals.py` reduces it to its **centroid** via `shapely`. Because
the source geometry was fetched as **EPSG:4326**, that centroid is already a
`(longitude, latitude)` pair — stored as indices 0 and 1. At render time the map's
**Mercator projection** converts `(lon, lat)` → screen `(x, y)`. Coordinates are
rounded to 4 dp (~11 m). Full chain:

```
polygon (raw layer) → centroid (lon,lat) via shapely → [lon, lat, …] in proposals.json
                    → Mercator projection → pixel on screen
```

**Raw (on disk, gitignored) vs. displayed (in the map):**

| Layer | In raw `out/` | In the map |
|---|---|---|
| EC / FC / CRZ (granted) | ✅ | ✅ |
| WL (wildlife) | ✅ | ❌ — all "DISPOSED", no grant signal (§5.4) |
| Forest Land Bank | ✅ | ❌ — not a project clearance |
| 12 sensitivity layers (PAs, ESZ, tiger reserves…) | ✅ | ❌ — context only (used in overlay analysis, §5.3) |

Raw `out/` = **17 GeoJSONL layers (~1.5 GB)**. The map uses only granted EC/FC/CRZ,
carried into `proposals.json` (~9.4 MB) + `company_by_pno.json` (~7.7 MB) +
`states.geojson` (~0.3 MB) + `state_counts.json`.

---

## 4. What We Built (Results)

An interactive, filterable map of **50,185 granted clearances** across 36 states/UTs:
- **Hero:** India choropleth, granted count per state (default view: 2026 · all types =
  12,555; all years · all types = 50,185).
- **Filters:** clearance type (All / EC / FC / CRZ) and year (2023–2026 + All, default 2026).
- **Drill:** zoom/pan reveals individual projects; hover shows full attributes.
- **Data files:** `states.geojson`, `state_counts.json`, `proposals.json` (grouped by
  state, ~50k records), `company_by_pno.json` (146,904 mappings).

---

## 4A. v1.1 — interaction & performance updates (17 Aug 2026)

Shipped after the first build; all live.

**Interactions**
- **Focus mode:** searching or clicking a state isolates it — the state keeps its colour
  and gains a **dark boundary**, all others fade, and only its projects show.
- **Click/tap a project** → the point gets a highlight ring and a detail card appears
  **beside it (never on top)**. Click empty space to dismiss.
- **No auto-reset:** zooming/scrolling out no longer snaps back to India — reset is only
  via the **Reset** button.
- Desktop gotcha fixed: the pan handler's `setPointerCapture` swallows the `click` event,
  so all point/state selection is done on **`pointerup`** (works on mouse and touch).

**Mobile**
- Responsive layout — **filters on top, map below, footer at bottom** (search visible on
  load); **pinch-to-zoom** (phones have no scroll-wheel); search via tap-a-suggestion or a
  **Go** button; and the **reset-on-scroll bug fixed** (height-only resizes from the mobile
  address bar are ignored).

**Performance — per-state lazy loading**
- `proposals.json` is split into one small file per state: **`map_v1/proposals/<STATE>.json`**,
  with the **company name inlined as index 9**. Initial load fetches only `states.geojson`
  + `state_counts.json` (~0.3 MB) → instant choropleth; a state's projects load on demand
  when opened/zoomed. The old ~17 MB upfront load (proposals + company) is gone. New
  pipeline step: `pipeline/split_states.py`.

**Default view:** All clearance types + All years (2023–2026) = **50,184** mapped.

---

## 5. Verified Findings

### 5.1 PARIVESH 2.0 geometry is clean (the positive headline)
**67,633 project boundaries, zero falling outside India.** The mandatory KML validation
introduced with 2.0 works. Spatial control confirms the pipeline: **wildlife proposals
overlap protected areas 574/609 = 94.25 %**, which is what the law predicts.

### 5.2 Boundary count vs granted count — full reconciliation
The gap between 67,633 boundaries and 50,185 granted is **not "rejected."** Computed from
all five layers:

| Category | Count | Layer | Note |
|---|---|---|---|
| **GRANTED (v1)** | **50,185** | EC/FC/CRZ | EC 42,242 · FC 7,517 · CRZ 426 |
| ToR stage | 10,990 | EC | scope approved, EC **not yet decided** |
| In process (pending) | 2,592 | mostly FC | levies / DFO / EDS states |
| Forest Land Bank | 2,548 | — | **not a clearance** — afforestation land inventory |
| Wildlife | 609 | WL | excluded (§5.4) |
| Referred to SEIAA | 457 | EC | handed to state authority |
| Stage-I (in-principle) | 247 | FC | conditional, not final |
| **Rejected** | **2** | EC | |
| Delisted / Returned | 3 | EC | |
| **Total** | **67,633** | | ✓ |

**Rejections number in the single digits (2 EC).** 63 % of the remainder is EC proposals
parked at the Terms-of-Reference stage. **A rejection/approval *rate* cannot be computed
from these layers** — they are geometry layers, not a complete application registry;
proposals rejected at screening may never receive geometry and thus never appear.

### 5.3 Spatial overlap with sensitivity zones (validated)
Share of proposals overlapping any protected/sensitive zone: **EC 3.20 %** (1,721/53,798),
**FC 13.74 %, CRZ 9.18 %.** *Overlap is not wrongdoing* — Eco-Sensitive Zones regulate
rather than prohibit development (Sanjay Gandhi National Park sits inside Mumbai).

### 5.4 Wildlife outcomes are not publicly recoverable
The WLC approve/reject distinction is real — PARIVESH's Wildlife Technical FAQ lists 19
statuses, with **"Permit Letter Issued" (approved)** vs **"Rejection Letter Issued"
(rejected)**. But those are *internal workflow* statuses. **Every public surface collapses
them to "DISPOSED"**: the GIS layer (609/609) and the metadata API `dataOfProposalNo`
(10/10 tested, `certificateUrl=null`). The only public route to the outcome is parsing
the NBWL/SBWL minutes decision text. Hence WL is a v2 (minutes-parsing) task, not a
quick field join.

### 5.5 Committee-minutes (MoM) counts, by committee
Derived directly from the public agenda/MoM services (see Appendix A). Counts are raw
array lengths (reliable); the rendered tables are not (§6).

| Service | PARIVESH 2.0 (MoM) | PARIVESH 1.0 |
|---|---|---|
| EC | EAC 587 · SEIAA 3,938 · SEAC 5,958 · SCEIA 1 · SAEIA 0 | Center 1,458 (State 13,502, index) |
| FC (Minutes) | FAC 40 · REC 189 · PSC 2,877 | AC 184 · REC 1,870 · Campa 31 |
| WL (MoM) | SCNBWL 17 · **SBWL 337** | 85 |

### 5.6 Infrastructure / transparency observations
- **Broken TLS chain** makes the GIS server fail in `requests`, `geopandas`, QGIS, R's
  `sf`, and `curl` while working in browsers.
- **Rendered MoM tables mislead:** selecting a non-default committee fires its query, then
  a default query re-fires and repaints the DOM — so the table and pagination show the
  wrong committee. SBWL rendered as 17 rows when the API array is 337. *Trust the API
  response length, never the rendered table.*
- **Wildlife minutes index publicly stops at the 83rd meeting** while other committees run
  to 2026, even though later meetings occurred.

---

## 6. Methodological Lessons (Where Interpretation Failed)

Counts held up consistently; **interpretations were what failed.** Recurring discipline:
- **Verify system behaviour before trusting a metric's semantics.** Almost every error
  came from trusting a query's or field's apparent meaning without checking.
- **Rows are not proposals** (legacy tables are patch-level; one proposal → many rows).
- **Complements are unreliable** — `total − granted` is not a clean category (§5.2).
- **Trust API array lengths, not rendered tables or pagination controls** (§5.6).
- **Do not trust `is_*_granted` / `is_decided` booleans** — e.g. `is_wlc_granted` is truthy
  for all 609 WL records; use the `status_clean` text.
- **Overlap ≠ violation; granted ≠ wrongdoing** — framing discipline for publication.
- **Test a join key before trusting a zero** (EC×WL CAF namespaces are disjoint; FC×WL share 188).

*(This project was built with heavy use of an AI coding agent; the verification loop above
is precisely what kept AI-generated output trustworthy. AI confidence was repeatedly
uncorrelated with correctness — e.g. it reported a committee as 17 records when the API
returned 337, and rendered a flattened "India" that looked complete but was a projection
bug. Both were caught by a human asking "are we sure?", not by better prompting.)*

---

## 7. Limitations

- **Snapshot, not live** — data frozen at 8 Aug 2026; 2.0 grows daily.
- **Granted-only** — the map currently does not show any other approval status (esp. 10,990 EC ToR-stage);
  no rejection rate is derivable (§5.2).
- **WL excluded** — no public outcome field (§5.4).
- **Project-name quality is mixed** — `proposal_name` is inconsistently a firm, a project,
  or a person; there is no clean proponent field in the geojson (enriched separately).
- **Single marker per multipart UT** — cannot represent all Puducherry enclaves at once.
- **Whole-dataset load** — the v1 map fetches all ~17 MB of JSON up front; fine for a
  prototype, not for scale (see §8).

---

## 8. Future Directions

1. **Publish it** — static hosting (Cloudflare/GitHub Pages), open-source the pipeline,
   attribute sources, and report the infrastructure defects to the ministry first.
2. **PMTiles** — convert proposals to vector tiles so the browser loads only what's in
   view; prerequisite for public scale and for auto-updating growing data.
3. **Auto-update** — a scheduled (weekly, not daily) job doing an **incremental** GIS pull
   (records with `granted_date` after last run) + incremental enrichment; keep large data
   out of git history; be a polite scraper.
4. **WL v2** — parse NBWL/SBWL minutes for the Recommended/Not-Recommended decision, giving
   a rare *granted-and-rejected* view.
5. **Legacy history** — ingest and clean the 1.0 layers to extend coverage back to 2006
   (heavy; rows≠proposals; geometry defects).
6. **Proximity analysis** — buffer overlays at 2 km / 10 km (a recent case concerned a mine
   1.6 km from a marine park — proximity, not containment).
7. **In-flight backlog view** — surface the ToR-stage / pending pipeline as its own layer.

---

## 9. Related Work

- **IndiaSpend — "Environment Clearances in India: 2014–2020"** — the closest prior art:
  an interactive EC map with state/year/type/protected-area filters. Differs from this
  work in era (pre-2.0), scope (EC-only), and depth (no project-level geometry / proponent
  enrichment on 2.0 data).
- **Land Conflict Watch; EJAtlas + Kalpavriksh** — map land/conservation *conflicts*, a
  different focus.
- **GitHub: `vjjan91/Environmental-Clearances`, `pratikunterwegs/forest-clearance-india`**
  — analysis code (static figures), not interactive maps.
- **PARIVESH DSS/GIS module; data.gov.in datasets** — official but gated / raw.

---

## Appendix A — Endpoints & Queries

**GIS (ArcGIS REST), base:**
`https://bharatmapsparivesh.parivesh.nic.in/parivesh_bharatmaps/rest/services/parivesh2/`
```
KML_Edit/MapServer/0   EC proposals   (53,801)
KML_Edit/MapServer/1   FC proposals   (10,248)
KML_Edit/MapServer/2   WL proposals   (609)
KML_Edit/MapServer/3   CRZ proposals  (427)
Parivesh_Proposals/MapServer/4   Forest Land Bank (2,548)
# query params: where=1=1 &outSR=4326 &outFields=* &f=json
#               &returnGeometry=true &geometryPrecision=6
#               fallback: returnIdsOnly=true then objectIds=<batch>
```

**Metadata API, base:** `https://parivesh.nic.in/parivesh_api/`
```
trackYourProposal/advanceSearchData?majorClearanceType={1=EC,2=FC,3=WL,4=CRZ}&state=<2011 census code>
    # returns ~26 fields incl. nameOfUserAgency, proposalNo, proposalStatus, certificateUrl, dates
    # slow / no pagination — slice by state
trackYourProposal/dataOfProposalNo?proposalNo=<PROPOSAL_NO>       # single-proposal lookup (fast)
kyc/getstates                                                    # state master (filter wants code, not id)
```

**Committee minutes — PARIVESH 2.0:**
```
agendamom/getAgendaMomDocumentByCommitteeV2?committee={EAC|SEIAA|SEAC|SCEIA|SAEIA|FAC|REC|PSC}&ref_type={AGENDA|MOM}&workgroupId={1=EC,2=FC}   (POST)
ua/wlcTrackYourProposal/getWlcAgendaMomPublic?committee={AGENDA|MOM}&ref_type={NBWL|SBWL}   (POST; WL — params read swapped)
# reliable count = response array length
```

**Committee minutes — PARIVESH 1.0:**
```
proponentApplicant/getParivesh1AgendaMom?committee=<X>&workgroup=<Y>&type=Mom&authority={Center|State}
```

**Boundaries:**
```
https://github.com/yashveeeeeeer/india-geodata  ->  data/administrative/states/datameet/Admin2.shp
    # 36 states/UTs, EPSG:4326, license CC0-1.0 / CC-BY-4.0 (per metadata.json)
```

## Appendix B — Reference field notes

- **State code:** proposals carry `state_lgd`; boundaries carry `ST_NM` (join by normalised name).
- **Status field to trust:** `status_clean` (text). Ignore `is_*_granted`, `is_decided`.
- **Dates:** EC/CRZ/WL `granted_date` (ISO); FC `date_of_stage_ii_approval` (DD-MON-YYYY);
  `1899-12-31` is a null-date sentinel.
- **WLC status vocabulary (internal):** 19 statuses; terminal = "Permit Letter Issued"
  (approved) / "Rejection Letter Issued" (rejected). Public surfaces show only "DISPOSED".

## Appendix C — Tech Stack

| Layer | Tools |
|---|---|
| Ingestion / networking | Python 3.13, `urllib`, **`truststore`** (TLS chain), `json` |
| Geoprocessing | `shapely` (geometry/centroids), **`pyproj.Geod`** (ellipsoidal area), `geopandas` + `fiona` (shapefile) |
| Frontend | Single self-contained HTML; **vanilla JS**; inline **SVG** (states) + **Canvas** (points); custom Mercator projection + zoom/pan transform; **no external libraries** |
| Serving | `python -m http.server` (static) |
| Boundaries | DataMeet `Admin2` shapefile, simplified |
| Output data | GeoJSON + JSON (states, per-state counts, proposals, company map) |

## Appendix D — Pipeline scripts

| Script | Purpose |
|---|---|
| `parivesh_gis_probe.py` | Walk ArcGIS services; dump schemas, counts, join candidates |
| `parivesh_audit.py` | Count-based anomaly audit (atomic, resumable) |
| `parivesh_pull.py` | Paginated GeoJSON download with quality flags + ID-batch fallback |
| `parivesh_overlay.py` | Streaming spatial overlay vs sensitivity layers |
| `nbwl_minutes.py` | Scrape SC-NBWL minutes; extract `WL/` numbers + decisions |
| `prep_boundaries.py` | Simplify DataMeet states → `states.geojson` |
| `prep_proposals.py` | Filter granted; derive centroid + area; per-state counts + proposals |
| `enrich_companies.py` | Path-B proponent enrichment (checkpointed) |
| `rebuild_manifest.py` | Rebuild the data manifest offline from on-disk layers |
| `map_v1/index.html` | The interactive map |

---

## Appendix E — Websites referenced

- PARIVESH portal — https://parivesh.nic.in
- PARIVESH (NIC) — https://www.nic.gov.in/project/parivesh/
- Wildlife Technical FAQ — https://cpc.parivesh.nic.in/writereaddata/ENV/Wildlife_Technical_FAQ.pdf
- Wildlife Clearance Procedure (coal.gov.in) — https://coal.gov.in/sites/default/files/2024-11/WildlifeClearanceProcedure.pdf
- Standing Committee of NBWL — https://www.conservationindia.org/resources/mandate-of-the-standing-committee-of-the-nbwl
- india-geodata (boundaries) — https://github.com/yashveeeeeeer/india-geodata
- OGD dataset (EC Granted, Parivesh 2.0) — https://www.data.gov.in/catalog/environmental-clearance-granted-parivesh-20
- IndiaSpend EC map (related work) — https://environmentclearance.indiaspend.com/
- Land Conflict Watch — https://www.landconflictwatch.org/
- Kalpavriksh (PA conflicts map) — https://kalpavriksh.org/our-work/conservation-livelihoods/protected-areas-governances/mapping-conflicts-in-protected-areas/
- vjjan91/Environmental-Clearances — https://github.com/vjjan91/Environmental-Clearances
- pratikunterwegs/forest-clearance-india — https://github.com/pratikunterwegs/forest-clearance-india

---

*Data © Government of India (PARIVESH 2.0). Administrative boundaries © DataMeet / LGD /
Survey of India (CC-BY-4.0). Figures are a snapshot as of 8 Aug 2026; the live portal
updates daily. v1 covers granted EC/FC/CRZ proposals only.*
