# Pipeline

Reproduces the map's data files from source. Run all commands **from the repo root**.
Requires Python 3.13 with `shapely`, `pyproj`, `geopandas`, `fiona`, and `truststore`.

Full context, endpoints, and query parameters are in [`../ProjectNotes.md`](../ProjectNotes.md)
(Appendices A & D).

## Order of operations

1. **Ingest the GIS layers** → `out/*.geojsonl`
   Use the repo's ingestion scripts (`../parivesh_pull.py`) to pull the PARIVESH 2.0
   `KML_Edit` layers (EC/FC/WL/CRZ) + Forest Land Bank. `truststore` is required for the
   server's incomplete TLS chain.

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

7. **`python pipeline/split_states.py`** → `map_v1/proposals/<STATE>.json`
   Splits `proposals.json` into one small file per state and **inlines the company name as
   index 9**. The map lazy-loads these per state (fast first paint); required for the live
   site. `company_by_pno.json` is no longer fetched by the map at runtime.

Then serve the map: `python -m http.server 8137 --directory map_v1`.
