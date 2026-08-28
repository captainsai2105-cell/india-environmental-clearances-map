# Pipeline

Reproduces the map's data files from source. Run all commands **from the repo root**.
Requires Python 3.13 with `shapely`, `pyproj`, `geopandas`, `fiona`, and `truststore`.
(The weekly refresh needs only the first two plus `truststore` — see below.)

**TLS:** the GIS host serves an incomplete chain. `truststore` covers Windows and macOS;
on Linux it defers to the OS bundle and does *not* help, so the missing root ships in
`certs/isrg-root-yr.pem` and is added to the system trust store automatically. Nothing to
configure — but never reach for `--insecure`.

Full context, endpoints, and query parameters are in [`../ProjectNotes.md`](../ProjectNotes.md)
(Appendices A & D).

## Order of operations

1. **Ingest the GIS layers** → `out/*.geojsonl`
   Use the repo's ingestion scripts (`../parivesh_pull.py`) to pull the PARIVESH 2.0
   `KML_Edit` layers (EC/FC/WL/CRZ) + Forest Land Bank. See the TLS note above.

2. **Fetch state boundaries** → `boundaries/Admin2.*`
   Download the DataMeet Admin2 shapefile (states, EPSG:4326, CC-BY-4.0) from
   `https://github.com/yashveeeeeeer/india-geodata` →
   `data/administrative/states/datameet/` into a local `boundaries/` folder.

3. **`python pipeline/prep_boundaries.py`** → `map_v1/states.geojson`
   Simplifies boundaries and bakes a normalised join key.

4. **`python pipeline/prep_proposals.py`** → `map_v1/state_counts.json`, `map_v1/proposals.json`
   Filters to *granted* (per clearance type), derives centroid + area (ha) from geometry,
   and aggregates per-state counts.

5. **`python pipeline/enrich_companies.py`** → `map_v1/company_by_pno.json`
   Bulk `advanceSearchData` pull (per type × state); maps `proposalNo` → proponent name.
   Slow and checkpointed — safe to resume.

6. **(optional) `python pipeline/rebuild_manifest.py out`** → rebuilds `out/manifest.json`
   offline from whatever layers are on disk.

7. **`python pipeline/split_states.py`** → `map_v1/proposals/<STATE>.json` and
   `map_v1/proposals_excluded/<STATE>.json`
   Splits the cold-build JSON into one small file per state and **inlines the company name
   as index 9**. The map lazy-loads the first set per state (fast first paint); required
   for the live site. `company_by_pno.json` is no longer fetched by the map at runtime.

Then serve the map: `python -m http.server 8137 --directory map_v1`.

## What counts as a clearance

Filtering on status alone over-counts. `pipeline/form_class.py` classifies every
`form_type` as **GRANT**, **ADMIN** or **UNKNOWN**, and both the cold build and the weekly
updater import it — one table, so the two paths cannot drift.

ADMIN records — transfers, amendments, validity extensions, corrigenda, splittings and
surrenders — are actions on a clearance that already exists. They are **published**, under
`map_v1/proposals_excluded/`, not deleted: shown + excluded reconciles to every
granted-status record, so the headline figure can be checked, and re-classifying a form
later is a local edit that needs no geometry re-fetched.

UNKNOWN is a sentinel, not a category: a `form_type` absent from the table is neither
counted nor silently dropped. The updater reports it and aborts past a threshold. Read
`form_class.py` before changing any of it — it records which parts are the ministry's own
categories and which are our judgment.


## Keeping it current

Steps 1–7 are the **cold build**. Day to day the map is refreshed incrementally by

```
python pipeline/update_map_data.py --dry-run   # report the diff
python pipeline/update_map_data.py             # apply it
```

which needs only `shapely`, `pyproj` and `truststore` — no `out/`, no geopandas.
`.github/workflows/update-data.yml` runs it weekly (Sun 20:30 UTC) and redeploys.

It works by **censusing each layer's attributes** (no geometry — 2000 rows in
~1.4 s, against a geometry pull that runs to hundreds of MB) and diffing that
against `map_v1/proposals/<STATE>.json`, then fetching geometry only for the
proposals that are actually new. A date watermark was the first design and does
not work: every `KML_Edit` field is a string, FC's Stage-II date is
`DD-MON-YYYY` and does not sort, and `sync_date` records when the KML was
pushed rather than when the decision changed — 98.8% of FC Stage-II approvals
carry a `sync_date` predating their own approval.

Because it is a diff and not a watermark, every run is a full reconciliation:
additions, withdrawals and amendments are all picked up, and a skipped week
costs nothing. It writes `map_v1/data_meta.json`, from which the map takes its
"data as of" date and a cache-busting version for the per-state files.

`map_v1/proposals.json` and `map_v1/company_by_pno.json` are inputs to the cold
build only. They are gitignored: the map and the updater both read the per-state
files, where the proponent name is already inlined at index 9.

To top up the raw `out/` layers the same way (append rows new since the last
pull, rather than re-downloading 951 MB):

```
python parivesh_pull.py --incremental
```

## When something looks wrong

```
python pipeline/check_connectivity.py
```

Times both PARIVESH hosts — a count query per GIS layer, and real `dataOfProposalNo`
lookups for records that currently have no proponent name. Under a minute, read-only.

Run it **first** whenever the map looks stale. The two explanations — our code broke, or
the government server will not answer today — are indistinguishable from the outside, and
this separates them without triggering a 30-minute update. It reads the same locally as in
CI, so the two can be compared directly; that matters, because the same GIS census has
taken 109 s and 1,677 s from GitHub on the same day, against ~40 s locally every time.

Also available as a manual GitHub Actions run (`check-connectivity.yml`). Exits non-zero
only if a host is entirely unreachable, so the run's colour carries the answer.
