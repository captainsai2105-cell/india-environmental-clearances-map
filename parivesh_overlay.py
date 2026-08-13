#!/usr/bin/env python3
"""
parivesh_overlay.py -- spatial overlay of clearance proposals against
ecological sensitivity layers.

Answers, at PROPOSAL level (not patch level):
  * how many clearances fall inside CPCB critically/severely polluted areas
  * how many intersect protected areas, by designation type
  * how many intersect tiger corridors, eco-sensitive areas, ESZs
  * the same within a buffer distance (default 10 km)

WHY IT STREAMS
--------------
fc_proposals.geojsonl is ~535 MB and ec_proposals ~389 MB. Loading either into
GeoPandas needs several GB. This script instead builds a spatial index over the
small sensitivity layers (~6,500 polygons total) and streams the big files one
feature at a time, so peak memory stays in the low hundreds of MB.

PROPOSALS, NOT ROWS
-------------------
Parivesh tables are patch-level: one proposal can occupy many rows. Every
headline figure here is deduplicated on proposal_key (falling back to caf_no).
Row counts are reported alongside, but the proposal count is the real number.

PREREQUISITES
-------------
    pip install shapely pandas
    python parivesh_pull.py --sensitivity      # if not already done

Usage
-----
    python parivesh_overlay.py
    python parivesh_overlay.py --buffer-km 5 --layers ec_proposals fc_proposals
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import os
import sys

try:
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    from shapely.prepared import prep
except ImportError:
    sys.exit("needs shapely 2.x:  pip install shapely")

OUT = "out"
RESULTS = "results"

# sensitivity layer file stem -> human label
SENSITIVITY = {
    "critically_polluted":   "CPCB Critically Polluted Area",
    "severely_polluted":     "CPCB Severely Polluted Area",
    "national_park":         "National Park",
    "wildlife_sanctuary":    "Wildlife Sanctuary",
    "community_reserve":     "Community Reserve",
    "conservation_reserve":  "Conservation Reserve",
    "tiger_corridor":        "Tiger Corridor",
    "tiger_reserve":         "Tiger Reserve",
    "wildlife_corridor":     "Wildlife Corridor",
    "eco_sensitive_area":    "Eco-Sensitive Area",
    "esz_gazette":           "ESZ (gazette notified)",
}

CLEARANCE = ["ec_proposals", "fc_proposals", "wl_proposals",
             "crz_proposals", "forest_land_bank"]

# name field candidates inside sensitivity layers
NAME_FIELDS = ["name", "Nam", "NAME", "pa_name", "Name", "sanctuary", "State"]

# Status lives under different names per layer, and the base `proposal_status`
# is often blank on FC -- which carries proposal_status_1 / _2 for Stage-I and
# Stage-II of forest clearance. Resolved here rather than at pull time so this
# works against files already on disk.
STATUS_FIELDS = ["proposal_status", "proposal_status_1", "proposal_status_2",
                 "proposal_s", "status_yet"]
DATE_FIELDS = ["dos", "granted_date", "sync_date", "date_of_is", "created_da"]

STATUS_CANON = {
    "TOR GRANTED": "ToR Granted",
    "STANDARD TOR GRANTED": "Standard ToR Granted",
    "EC GRANTED": "EC Granted",
    "EC REJECTED": "EC Rejected",
    "DELISTED_BY_SYSTEM": "Delisted by System",
    "DELISTED BY SYSTEM": "Delisted by System",
}


def resolve_status(props: dict) -> tuple[str | None, str | None]:
    """Return (normalised status, which field it came from)."""
    import re as _re
    for k in STATUS_FIELDS:
        v = props.get(k)
        if v not in (None, "", "NULL", "null"):
            s = _re.sub(r"\s+", " ", str(v).strip())
            return STATUS_CANON.get(s.upper(), s), k
    # fall back to whatever the pull stage computed
    return props.get("status_clean"), "status_clean"


def resolve_date(props: dict) -> tuple[str | None, str | None]:
    for k in DATE_FIELDS:
        v = props.get(k)
        # 1899-12-31 is a null-date sentinel used throughout this dataset
        if v not in (None, "", "NULL", "null") and not str(v).startswith("1899"):
            return str(v), k
    return None, None


def km_to_deg(km: float, lat: float = 22.0) -> float:
    """
    Crude degrees-per-km. Adequate for a screening buffer, NOT for area maths.
    Longitude degrees shrink with latitude; 22N is roughly central India.
    If you need accurate distances, reproject to an equal-distance CRS.
    """
    return km / (111.32 * math.cos(math.radians(lat)))


def read_geojsonl(path: str):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def pick_name(props: dict) -> str:
    for f in NAME_FIELDS:
        if props.get(f):
            return str(props[f])
    return "(unnamed)"


def load_sensitivity(buffer_deg: float):
    """Build one STRtree over all sensitivity polygons, with metadata alongside."""
    geoms, meta = [], []
    for stem, label in SENSITIVITY.items():
        path = os.path.join(OUT, f"{stem}.geojsonl")
        if not os.path.exists(path):
            print(f"  [skip] {stem} not downloaded")
            continue
        n = 0
        for feat in read_geojsonl(path):
            g = feat.get("geometry")
            if not g or not g.get("coordinates"):
                continue
            try:
                geom = shape(g)
                if not geom.is_valid:
                    geom = geom.buffer(0)          # fix self-intersections
                if geom.is_empty:
                    continue
            except Exception:  # noqa: BLE001
                continue
            geoms.append(geom.buffer(buffer_deg) if buffer_deg else geom)
            meta.append({"layer": stem, "label": label,
                         "name": pick_name(feat.get("properties") or {})})
            n += 1
        print(f"  {label:<32}{n:>6}")
    if not geoms:
        sys.exit("no sensitivity layers found -- run: "
                 "python parivesh_pull.py --sensitivity")
    print(f"  {'TOTAL':<32}{len(geoms):>6}\n")
    return STRtree(geoms), geoms, meta


def overlay(layer: str, tree, geoms, meta, prepared) -> dict:
    path = os.path.join(OUT, f"{layer}.geojsonl")
    if not os.path.exists(path):
        print(f"  [skip] {layer} not found")
        return {}

    rows_total = rows_hit = 0
    proposals_all: set[str] = set()
    proposals_hit: set[str] = set()
    cafs_all: set[str] = set()
    cafs_hit: set[str] = set()
    by_label: dict[str, set] = collections.defaultdict(set)
    by_relation: dict[tuple, set] = collections.defaultdict(set)
    by_zone: dict[tuple, set] = collections.defaultdict(set)
    by_state: dict[str, set] = collections.defaultdict(set)
    detail = []

    for feat in read_geojsonl(path):
        rows_total += 1
        if rows_total % 5000 == 0:
            print(f"      {rows_total:,} rows", end="\r", flush=True)

        props = feat.get("properties") or {}
        if props.get("flag_empty_geometry"):
            continue
        # torn polygons span thousands of km and would hit everything
        if props.get("flag_torn_geometry"):
            continue

        key = (props.get("proposal_key") or props.get("caf_no")
               or props.get("proposal_no") or f"_row{rows_total}")
        proposals_all.add(key)
        caf = props.get("caf_no")
        if caf:
            cafs_all.add(caf)

        g = feat.get("geometry")
        if not g or not g.get("coordinates"):
            continue
        try:
            geom = shape(g)
            if not geom.is_valid:
                geom = geom.buffer(0)
        except Exception:  # noqa: BLE001
            continue
        if geom.is_empty:
            continue

        hits = tree.query(geom)
        matched = False
        for idx in hits:
            if not prepared[idx].intersects(geom):
                continue
            matched = True
            m = meta[idx]

            # An "overlap" that only touches a boundary is legally and
            # practically different from a project sitting inside a protected
            # area. Report them separately rather than lumping both under
            # "intersects", which overstates encroachment.
            if prepared[idx].contains(geom):
                relation = "within"
            else:
                try:
                    frac = geom.intersection(geoms[idx]).area / geom.area \
                        if geom.area > 0 else 0.0
                except Exception:  # noqa: BLE001
                    frac = 0.0
                relation = "mostly_inside" if frac >= 0.5 else "partial"

            by_label[m["label"]].add(key)
            by_relation[(m["label"], relation)].add(key)
            by_zone[(m["label"], m["name"])].add(key)
            status, status_src = resolve_status(props)
            date, date_src = resolve_date(props)
            detail.append({
                "proposal_key": key,
                "caf_no": props.get("caf_no"),
                "sw_no": props.get("sw_no"),
                "state": props.get("stname") or props.get("state_name"),
                "district": props.get("dtname"),
                "category": props.get("category"),
                "status": status,
                "status_field": status_src,
                "date": date,
                "date_field": date_src,
                "dos": props.get("dos"),
                "granted_date": props.get("granted_date"),
                "project_area": props.get("project_area"),
                "sensitivity_type": m["label"],
                "sensitivity_name": m["name"],
                "relation": relation,
            })
        if matched:
            rows_hit += 1
            proposals_hit.add(key)
            if props.get("caf_no"):
                cafs_hit.add(props["caf_no"])
            st = props.get("stname") or props.get("state_name") or "(unknown)"
            by_state[str(st).strip().upper()].add(key)

    print(f"      {rows_total:,} rows           ")
    return {
        "layer": layer,
        "rows_total": rows_total,
        "rows_hit": rows_hit,
        "proposals_total": len(proposals_all),
        "proposals_hit": len(proposals_hit),
        "cafs_total": len(cafs_all),
        "cafs_hit": len(cafs_hit),
        "by_label": {k: len(v) for k, v in by_label.items()},
        "by_relation": {f"{a} :: {b}": len(v) for (a, b), v in by_relation.items()},
        "by_zone": {f"{a} :: {b}": len(v) for (a, b), v in by_zone.items()},
        "by_state": {k: len(v) for k, v in by_state.items()},
        "detail": detail,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--buffer-km", type=float, default=0.0,
                    help="buffer around sensitivity zones (0 = direct overlap)")
    ap.add_argument("--layers", nargs="*", default=CLEARANCE)
    args = ap.parse_args()

    os.makedirs(RESULTS, exist_ok=True)
    buf = km_to_deg(args.buffer_km) if args.buffer_km else 0.0
    label = f"{args.buffer_km:g}km" if args.buffer_km else "direct"

    print(f"\nSensitivity layers (buffer: {label})")
    tree, geoms, meta = load_sensitivity(buf)
    prepared = [prep(g) for g in geoms]

    results = []
    for layer in args.layers:
        print(f"  {layer}")
        r = overlay(layer, tree, geoms, meta, prepared)
        if r:
            results.append(r)

    # ---- write outputs ----
    det_path = os.path.join(RESULTS, f"overlay_detail_{label}.csv")
    with open(det_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["layer", "proposal_key", "caf_no",
                                           "sw_no", "state", "district",
                                           "category", "status", "status_field",
                                           "date", "date_field", "dos",
                                           "granted_date", "project_area",
                                           "sensitivity_type", "sensitivity_name",
                                           "relation"])
        w.writeheader()
        for r in results:
            for d in r["detail"]:
                w.writerow({"layer": r["layer"], **d})

    sum_path = os.path.join(RESULTS, f"overlay_summary_{label}.json")
    json.dump([{k: v for k, v in r.items() if k != "detail"} for r in results],
              open(sum_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    # ---- console report ----
    print(f"\n{'='*74}\nOVERLAY SUMMARY  (buffer: {label})\n{'='*74}")
    for r in results:
        pt, ph = r["proposals_total"], r["proposals_hit"]
        pct = 100 * ph / pt if pt else 0
        print(f"\n{r['layer']}")
        print(f"  rows      : {r['rows_hit']:,} of {r['rows_total']:,}")
        print(f"  PROPOSALS : {ph:,} of {pt:,}  ({pct:.2f}%)   <- the real figure")
        if r["by_label"]:
            print("  by sensitivity type  (within / mostly / partial):")
            for k, v in sorted(r["by_label"].items(), key=lambda x: -x[1]):
                w_ = r["by_relation"].get(f"{k} :: within", 0)
                m_ = r["by_relation"].get(f"{k} :: mostly_inside", 0)
                p_ = r["by_relation"].get(f"{k} :: partial", 0)
                print(f"      {v:>6}  {k:<32} {w_:>5} / {m_:>5} / {p_:>5}")
        if r.get("cafs_total"):
            print(f"  distinct CAFs: {r['cafs_hit']:,} of {r['cafs_total']:,}")
        if r["by_state"]:
            print("  top states:")
            for k, v in sorted(r["by_state"].items(), key=lambda x: -x[1])[:8]:
                print(f"      {v:>6}  {k}")

    print(f"\nWrote {det_path}\n      {sum_path}")
    print("\nNOTE: buffer distances use a flat degree approximation and are a "
          "screening tool.\n      Reproject to an equal-distance CRS before "
          "publishing any distance figure.")


if __name__ == "__main__":
    main()