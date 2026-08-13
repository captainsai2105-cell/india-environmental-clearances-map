# India Environmental Clearances — Interactive Map

An interactive map of every **granted** environmental (EC), forest (FC), and coastal (CRZ)
clearance in India, built from the Government of India's **PARIVESH 2.0** open data.

<!-- Add a screenshot or GIF here, e.g. map_v1/preview.png -->
<!-- ![Map preview](map_v1/preview.png) -->

**Live demo:** https://captainsai2105-cell.github.io/india-environmental-clearances-map/ · **Method & write-up:** [ProjectNotes.md](ProjectNotes.md)

---

## What it shows

- **50,185 granted clearances (2023–2026)** — EC 42,242 · FC (Stage-II) 7,517 · CRZ 426.
- **Hero:** India choropleth of granted proposals per state/UT.
- **Drill:** scroll/drag to zoom (QGIS-style); individual projects appear as you zoom in.
- **Filter:** by clearance type (All / EC / FC / CRZ) and year (2023–2026).
- **Hover a project** for its name, type, area (hectares), grant date, and proponent.

## Run locally

No build step, no dependencies — plain HTML/JS with inline SVG + Canvas:

```bash
python -m http.server 8137 --directory map_v1
# then open http://localhost:8137
```

## How it was built

Full methodology, data reconciliation, and limitations are in **[ProjectNotes.md](ProjectNotes.md)**.
In brief: pulled 67,633 project boundaries from PARIVESH 2.0's GIS server (working around a
broken TLS chain, four coordinate systems, and error-as-HTTP-200 quirks), filtered to *granted*
proposals, **derived project areas from geometry**, enriched proponent names via the metadata
API, joined to DataMeet state boundaries, and rendered a dependency-free interactive map.

## Important caveats (please read before drawing conclusions)

- **Snapshot, not live** — data as of **8 Aug 2026**; the portal updates daily.
- **Overlap with a protected area is _not_ a violation**, and **a granted clearance is _not_
  wrongdoing.** This is a transparency/accessibility tool, not an accusation.
- **Wildlife (WL) clearances are excluded** — the public data offers no clean approve/reject
  signal (all logged as "DISPOSED").
- **You cannot derive a rejection/approval _rate_** from this data — it's a geometry layer, not
  a complete application registry (see [ProjectNotes.md](ProjectNotes.md) §5.2).

## Data & attribution

- Clearance data © **Government of India, [PARIVESH 2.0](https://parivesh.nic.in)**.
- Administrative boundaries © **DataMeet / LGD / Survey of India (CC-BY-4.0)**, via
  [india-geodata](https://github.com/yashveeeeeeer/india-geodata).
- This is an **independent, personal project on public data** — not affiliated with, or
  produced for, any employer or the Government of India.

## License

Code: **MIT** (see [LICENSE](LICENSE)). Data: subject to the sources above.
