#!/usr/bin/env python3
"""
parivesh_audit.py -- count-based geometry audit across ALL Parivesh map layers,
accumulating every layer's result into one JSON report.

WHY COUNTS, NOT EXTENTS
-----------------------
A layer's reported extent is set by its single most extreme feature. One bad
record stretches the bounding box exactly as much as a million would. Extents
detect that outliers exist; they never measure how many. Every number this
script produces comes from returnCountOnly / groupBy statistics -- i.e. counted,
not inferred.

WHAT IT DOES, per layer:
  1. total feature count
  2. count intersecting India  -> difference = features outside
  3. count in each anomaly box (null island, North America, southern ocean,
     far north, transposed-India)
  4. for any non-zero box, a groupBy breakdown on the layer's state-like field
  5. distinct proposal count where a proposal-id field exists (patches != proposals)

Layer list and field names are derived from parivesh_gis_inventory.json (produced
by parivesh_gis_probe.py) so nothing is hardcoded. Falls back to walking the
service directory live with --discover.

Results append to ONE report file after every layer, so an interrupted run loses
nothing and --resume picks up where it stopped.

Read-only: GET to /query only, with the same write-endpoint guards.

TLS: the server omits its intermediate CA.  `pip install truststore` is the fix;
--cafile and --insecure are the fallbacks.

Usage
-----
    pip install truststore
    python parivesh_audit.py                          # every layer
    python parivesh_audit.py --only KML_Edit parivesh1 # just the proposal layers
    python parivesh_audit.py --resume                 # continue an interrupted run
    python parivesh_audit.py --skip-larger-than 500000 --delay 1.5
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = ("https://bharatmapsparivesh.parivesh.nic.in"
        "/parivesh_bharatmaps/rest/services/parivesh2")

# India incl. island territories + 12nm territorial sea.
# Westernmost point is mainland Gujarat (68.11E), NOT Lakshadweep (71.7E).
# Southernmost is Indira Point, Nicobar (6.74N). Easternmost Arunachal (97.42E).
INDIA = (67.9, 6.5, 97.6, 37.3)

# Diagnostic envelopes, each isolating one failure mode. All in EPSG:4326;
# the server reprojects them into each layer's native CRS via inSR.
BOXES = {
    "null_island":      (-1.0, -1.0, 1.0, 1.0),
    "north_america":    (-130.0, 20.0, -100.0, 50.0),
    "southern_ocean":   (60.0, -25.0, 80.0, 5.0),
    "far_north":        (60.0, 38.0, 100.0, 90.0),
    "transposed_india": (6.7, 68.0, 37.1, 97.5),   # lat/lon axis-order swap
    "africa_europe":    (10.0, 0.0, 60.0, 60.0),
}

# Candidate field names, most specific first. Layers are inconsistent:
# state_name / stname / state / project_st all appear, and EC4SHP stores
# "TN" where other layers store "Tamil Nadu".
STATE_FIELDS = ["state_name", "stname", "state", "project_st", "statename",
                "st_name", "state_ut"]
PROPOSAL_FIELDS = ["proposal_no", "proposal_n", "proposal_i", "proposal_1"]
DISTRICT_FIELDS = ["dtname", "district_n", "district", "dist_name"]

FORBIDDEN = ("applyedits", "addfeatures", "updatefeatures", "deletefeatures",
             "calculate", "append", "truncate", "createreplica")
UA = "parivesh-audit/1.0 (read-only data-quality audit)"


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #

def build_ssl_context(cafile: str | None, insecure: bool) -> ssl.SSLContext:
    if insecure:
        print("WARNING: TLS verification disabled.", file=sys.stderr)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if cafile:
        print(f"TLS: verifying against {cafile}")
        return ssl.create_default_context(cafile=cafile)
    try:
        import truststore  # noqa: PLC0415
        print("TLS: OS trust store (truststore)")
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        print("TLS: stock bundle. If this fails with 'unable to get local issuer "
              "certificate', run `pip install truststore`.", file=sys.stderr)
        return ssl.create_default_context()


class Client:
    def __init__(self, ctx, delay=1.0, timeout=120, retries=2):
        self.ctx, self.delay, self.timeout, self.retries = ctx, delay, timeout, retries
        self.calls = 0

    def _get(self, url: str) -> dict:
        for bad in FORBIDDEN:
            if bad in url.lower():
                raise RuntimeError(f"refusing write endpoint: {url}")
        last = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout,
                                            context=self.ctx) as r:
                    self.calls += 1
                    time.sleep(self.delay)
                    return json.loads(r.read().decode("utf-8", "replace"))
            except urllib.error.URLError as e:
                reason = str(getattr(e, "reason", e))
                if "CERTIFICATE_VERIFY_FAILED" in reason:
                    sys.exit("TLS chain incomplete -- run `pip install truststore`.")
                last = reason
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            self.calls += 1
            time.sleep(self.delay * (attempt + 2))   # simple backoff
        return {"_error": last or "unknown"}

    def query(self, svc: str, lid: int, params: dict) -> dict:
        url = f"{BASE}/{svc}/MapServer/{lid}/query?{urllib.parse.urlencode(params)}"
        return self._get(url)

    def count(self, svc: str, lid: int, box=None, where="1=1"):
        p = {"where": where, "returnCountOnly": "true", "f": "json"}
        if box:
            p |= {"geometry": ",".join(str(v) for v in box),
                  "geometryType": "esriGeometryEnvelope",
                  "spatialRel": "esriSpatialRelIntersects",
                  "inSR": "4326"}
        d = self.query(svc, lid, p)
        return d.get("_error") or d.get("count")

    def group(self, svc: str, lid: int, field: str, stat_field: str, box=None):
        stats = json.dumps([{"statisticType": "count",
                             "onStatisticField": stat_field,
                             "outStatisticFieldName": "n"}], separators=(",", ":"))
        p = {"where": "1=1", "groupByFieldsForStatistics": field,
             "outStatistics": stats, "f": "json"}
        if box:
            p |= {"geometry": ",".join(str(v) for v in box),
                  "geometryType": "esriGeometryEnvelope",
                  "spatialRel": "esriSpatialRelIntersects",
                  "inSR": "4326"}
        d = self.query(svc, lid, p)
        if "_error" in d:
            return {"_error": d["_error"]}
        out = {}
        for f in d.get("features", []):
            a = f.get("attributes", {})
            out[str(a.get(field))] = a.get("n")
        return out

    def distinct(self, svc: str, lid: int, field: str, box=None):
        stats = json.dumps([{"statisticType": "countDistinct",
                             "onStatisticField": field,
                             "outStatisticFieldName": "d"}], separators=(",", ":"))
        p = {"where": "1=1", "outStatistics": stats, "f": "json"}
        if box:
            p |= {"geometry": ",".join(str(v) for v in box),
                  "geometryType": "esriGeometryEnvelope",
                  "spatialRel": "esriSpatialRelIntersects",
                  "inSR": "4326"}
        d = self.query(svc, lid, p)
        if "_error" in d:
            return None
        feats = d.get("features") or []
        return feats[0].get("attributes", {}).get("d") if feats else None


# --------------------------------------------------------------------------- #
# layer discovery
# --------------------------------------------------------------------------- #

def pick(fields: list[str], candidates: list[str]) -> str | None:
    low = {f.lower(): f for f in fields}
    for c in candidates:
        if c in low:
            return low[c]
    return None


def layers_from_inventory(path: str) -> list[dict]:
    rows = json.load(open(path, encoding="utf-8"))
    out = []
    for r in rows:
        if r.get("error"):
            continue
        names = [f["name"] for f in r.get("fields", [])]
        out.append({
            "service": r["service"].split("/")[-1],
            "layer_id": r["layer_id"],
            "layer_name": r.get("layer_name"),
            "wkid": r.get("wkid"),
            "known_count": r.get("feature_count"),
            "state_field": pick(names, STATE_FIELDS),
            "proposal_field": pick(names, PROPOSAL_FIELDS),
            "district_field": pick(names, DISTRICT_FIELDS),
            "stat_field": pick(names, PROPOSAL_FIELDS) or (names[0] if names else None),
        })
    return out


def layers_from_live(cli: Client) -> list[dict]:
    d = cli._get(f"{BASE}?f=json")
    if "_error" in d:
        sys.exit(f"could not list services: {d['_error']}")
    out = []
    for s in d.get("services", []):
        if s.get("type") != "MapServer":
            continue
        svc = s["name"].split("/")[-1]
        root = cli._get(f"{BASE}/{svc}/MapServer?f=json")
        for lyr in root.get("layers") or []:
            if lyr.get("type") != "Feature Layer":
                continue
            meta = cli._get(f"{BASE}/{svc}/MapServer/{lyr['id']}?f=json")
            names = [f["name"] for f in meta.get("fields", [])
                     if f.get("type") != "esriFieldTypeGeometry"]
            out.append({
                "service": svc, "layer_id": lyr["id"],
                "layer_name": meta.get("name") or lyr.get("name"),
                "wkid": (meta.get("spatialReference") or {}).get("latestWkid"),
                "known_count": None,
                "state_field": pick(names, STATE_FIELDS),
                "proposal_field": pick(names, PROPOSAL_FIELDS),
                "district_field": pick(names, DISTRICT_FIELDS),
                "stat_field": pick(names, PROPOSAL_FIELDS) or (names[0] if names else None),
            })
    return out


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #

def audit_layer(cli: Client, L: dict) -> dict:
    svc, lid = L["service"], L["layer_id"]
    key = f"{svc}/{lid}"
    res = dict(L) | {"key": key, "audited_at": datetime.now(timezone.utc).isoformat(),
                     "boxes": {}, "outside_india": None, "pct_outside": None}

    total = cli.count(svc, lid)
    res["total"] = total
    if not isinstance(total, int):
        res["error"] = str(total)
        print(f"    ERROR {total}")
        return res
    if total == 0:
        print("    empty layer")
        return res

    inside = cli.count(svc, lid, INDIA)
    res["inside_india"] = inside
    if isinstance(inside, int):
        res["outside_india"] = total - inside
        res["pct_outside"] = round(100 * (total - inside) / total, 4)
        print(f"    total={total:,}  inside={inside:,}  "
              f"outside={total - inside:,} ({res['pct_outside']}%)")

    for mode, box in BOXES.items():
        n = cli.count(svc, lid, box)
        if not isinstance(n, int) or n == 0:
            continue
        entry = {"count": n, "pct_of_layer": round(100 * n / total, 4)}
        print(f"      {mode}: {n:,} ({entry['pct_of_layer']}%)")
        if L.get("state_field") and L.get("stat_field"):
            g = cli.group(svc, lid, L["state_field"], L["stat_field"], box)
            entry["by_state"] = g
            if "_error" not in g:
                for st, c in sorted(g.items(), key=lambda x: -(x[1] or 0))[:6]:
                    print(f"         {c:>7}  {st}")
        if L.get("proposal_field"):
            entry["distinct_proposals"] = cli.distinct(
                svc, lid, L["proposal_field"], box)
        res["boxes"][mode] = entry

    # denominator: total per state, so prevalence can be a rate not a raw count
    if res["boxes"] and L.get("state_field") and L.get("stat_field"):
        res["state_totals"] = cli.group(svc, lid, L["state_field"], L["stat_field"])
    if L.get("proposal_field"):
        res["distinct_proposals_total"] = cli.distinct(svc, lid, L["proposal_field"])

    return res


def save(report: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)   # atomic; a crash mid-write can't corrupt the report


def write_csv(report: dict, path: str) -> None:
    cols = ["key", "layer_name", "wkid", "total", "inside_india", "outside_india",
            "pct_outside", "worst_box", "worst_box_count", "worst_state",
            "worst_state_count", "state_field", "error"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in report["layers"].values():
            row = dict(r)
            boxes = r.get("boxes") or {}
            if boxes:
                wb = max(boxes.items(), key=lambda x: x[1]["count"])
                row["worst_box"], row["worst_box_count"] = wb[0], wb[1]["count"]
                bs = wb[1].get("by_state") or {}
                bs = {k: v for k, v in bs.items() if isinstance(v, int)}
                if bs:
                    ws = max(bs.items(), key=lambda x: x[1])
                    row["worst_state"], row["worst_state_count"] = ws
            w.writerow(row)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--inventory", default="parivesh_gis_inventory.json",
                    help="output of parivesh_gis_probe.py")
    ap.add_argument("--discover", action="store_true",
                    help="walk the service directory live instead")
    ap.add_argument("--out", default="parivesh_audit_report.json")
    ap.add_argument("--only", nargs="*", metavar="NAME",
                    help="substring filter on service or layer name")
    ap.add_argument("--resume", action="store_true",
                    help="skip layers already present in --out")
    ap.add_argument("--skip-larger-than", type=int, default=None,
                    help="skip layers with more features than this")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--cafile")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    cli = Client(build_ssl_context(args.cafile, args.insecure),
                 delay=args.delay, timeout=args.timeout)

    if args.discover or not os.path.exists(args.inventory):
        print("discovering layers live...")
        layers = layers_from_live(cli)
    else:
        print(f"reading layers from {args.inventory}")
        layers = layers_from_inventory(args.inventory)

    if args.only:
        want = [w.lower() for w in args.only]
        layers = [L for L in layers
                  if any(w in f"{L['service']}/{L['layer_name']}".lower() for w in want)]
    if args.skip_larger_than:
        layers = [L for L in layers
                  if not isinstance(L.get("known_count"), int)
                  or L["known_count"] <= args.skip_larger_than]

    report = {"generated_at": datetime.now(timezone.utc).isoformat(),
              "base": BASE, "india_box": INDIA, "boxes": BOXES, "layers": {}}
    if args.resume and os.path.exists(args.out):
        report = json.load(open(args.out, encoding="utf-8"))
        report.setdefault("layers", {})
        print(f"resuming: {len(report['layers'])} layers already done")

    todo = [L for L in layers if f"{L['service']}/{L['layer_id']}" not in report["layers"]]
    print(f"{len(todo)} layers to audit (of {len(layers)})\n")

    try:
        for i, L in enumerate(todo, 1):
            key = f"{L['service']}/{L['layer_id']}"
            print(f"[{i}/{len(todo)}] {key}  {L['layer_name']}")
            report["layers"][key] = audit_layer(cli, L)
            save(report, args.out)          # persist after EVERY layer
    except KeyboardInterrupt:
        print("\ninterrupted -- partial report saved, rerun with --resume")

    save(report, args.out)
    csv_path = os.path.splitext(args.out)[0] + ".csv"
    write_csv(report, csv_path)

    done = report["layers"].values()
    flagged = [r for r in done if r.get("boxes")]
    print(f"\n{'='*72}\nAudited {len(done)} layers. {len(flagged)} have anomalies.\n{'='*72}")
    for r in sorted(flagged, key=lambda x: -(x.get("outside_india") or 0))[:25]:
        print(f"  {r['key']:<26}{r.get('outside_india', '?'):>8} outside "
              f"({r.get('pct_outside')}%)  modes: {', '.join(r['boxes'])}")
    print(f"\nWrote {args.out} and {csv_path}   ({cli.calls} HTTP requests)")


if __name__ == "__main__":
    main()
