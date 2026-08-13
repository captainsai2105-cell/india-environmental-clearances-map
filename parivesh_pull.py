#!/usr/bin/env python3
"""
parivesh_pull.py -- download Parivesh clearance polygons and ecological
sensitivity layers as clean, analysis-ready files.

WHAT IT PULLS
-------------
Default (Parivesh 2.0, ~67,600 features, validated clean):
    KML_Edit/0..3          EC / FC / WL / CRZ proposals   (28-32 attributes)
    Parivesh_Proposals/4   Forest Land Bank

--sensitivity  adds the ecological baseline (~8,000 features, all small):
    Protected_Area (by designation), TigerCorridor, ESA, ESZ,
    CPCB_Polluted, Environment/21 Wildlife Corridor

--include-legacy adds Parivesh 1.0 (~388,000 features). READ THIS FIRST:
    the 1.0 archive contains known defects -- empty geometry, a -122.09
    coordinate constant, occasional sign flips. This script flags them
    (see the `flag_*` columns) rather than silently dropping them. More
    importantly, 1.0 tables are PATCH-level: one proposal can occupy
    hundreds of rows. Never quote a percentage of rows as a percentage of
    proposals. Use proposal_key for any rate you intend to publish.

OUTPUT
------
    out/<layer>.geojsonl     newline-delimited GeoJSON, written as it streams
    out/<layer>.parquet      if geopandas is installed
    out/manifest.json        counts, timestamps, flag tallies per layer

Resumable: pagination offset is checkpointed, so an interrupted run continues
with --resume instead of starting over.

TLS: the server omits its intermediate CA. `pip install truststore` fixes it;
--cafile and --insecure are fallbacks.

Usage
-----
    pip install truststore
    pip install geopandas pyarrow          # optional, for parquet
    python parivesh_pull.py
    python parivesh_pull.py --sensitivity
    python parivesh_pull.py --include-legacy --resume
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = ("https://bharatmapsparivesh.parivesh.nic.in"
        "/parivesh_bharatmaps/rest/services/parivesh2")

PAGE = 2000          # server maxRecordCount
PAGE_FLOOR = 25      # stop halving below this; if it still fails, the layer is broken
OUT = "out"

# Coordinate decimal places. 6 dp is ~0.1 m at the equator -- far finer than any
# KML a proponent submits. Default ArcGIS output can run to 15 dp, which is pure
# payload: it roughly halves response size for vertex-heavy forest polygons.
PRECISION = 6

# Some layers hold patch boundaries with huge vertex counts. FC pages at the full
# 2000 produce 70 MB+ responses that time out mid-serialisation. Start smaller.
PAGE_OVERRIDE = {
    ("KML_Edit", 1): 250,          # FC proposals -- worst offender
    ("parivesh1", 1): 250,         # legacy FC patches
    ("parivesh1", 2): 500,
    ("parivesh1", 5): 500,
}

# India incl. island territories + 12nm sea. Westernmost is mainland Gujarat
# (68.11E), NOT Lakshadweep. Southernmost is Indira Point, Nicobar (6.74N).
INDIA = (67.9, 6.5, 97.6, 37.3)

CLEARANCE = [
    ("KML_Edit", 0, "ec_proposals"),
    ("KML_Edit", 1, "fc_proposals"),
    ("KML_Edit", 2, "wl_proposals"),
    ("KML_Edit", 3, "crz_proposals"),
    ("Parivesh_Proposals", 4, "forest_land_bank"),
]

SENSITIVITY = [
    ("Protected_Area", 0, "protected_area_all"),
    ("Protected_Area", 2, "national_park"),
    ("Protected_Area", 3, "wildlife_sanctuary"),
    ("Protected_Area", 4, "community_reserve"),
    ("Protected_Area", 5, "conservation_reserve"),
    ("TigerCorridor", 0, "tiger_corridor"),
    ("ESA", 0, "eco_sensitive_area"),
    ("ESZ", 0, "esz_gazette"),
    ("CPCB_Polluted", 0, "critically_polluted"),
    ("CPCB_Polluted", 1, "severely_polluted"),
    ("Environment", 21, "wildlife_corridor"),
    ("Environment", 20, "tiger_reserve"),
]

LEGACY = [
    ("parivesh1", 0, "legacy_ec"),
    ("parivesh1", 1, "legacy_fc_form_ab"),
    ("parivesh1", 2, "legacy_fc_formc"),
    ("parivesh1", 3, "legacy_ca_mutation"),
    ("parivesh1", 4, "legacy_ca_nonforest"),
    ("parivesh1", 5, "legacy_wildlife"),
]

FORBIDDEN = ("applyedits", "addfeatures", "updatefeatures", "deletefeatures",
             "calculate", "append", "truncate", "createreplica")
UA = "parivesh-pull/1.0 (read-only public-data download)"

# 29 status spellings collapse to far fewer. Case/whitespace first, then these.
STATUS_CANON = {
    "TOR GRANTED": "ToR Granted",
    "STANDARD TOR GRANTED": "Standard ToR Granted",
    "EC GRANTED": "EC Granted",
    "EC REJECTED": "EC Rejected",
    "DELISTED_BY_SYSTEM": "Delisted by System",
    "DELISTED BY SYSTEM": "Delisted by System",
}
# Statuses that represent a final decision. Anything else is in-process and
# should be excluded from approval-rate denominators.
DECIDED = {"EC Granted", "EC Rejected", "ToR Granted", "Standard ToR Granted"}


class OversizeResponse(Exception):
    """Server truncated the response; the page is too big to serialise."""


class ArcGISError(Exception):
    """Server returned an error object with HTTP 200."""


def build_ssl_context(cafile, insecure) -> ssl.SSLContext:
    if insecure:
        print("WARNING: TLS verification disabled.", file=sys.stderr)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    try:
        import truststore  # noqa: PLC0415
        print("TLS: OS trust store (truststore)")
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        print("TLS: stock bundle. If this fails, run `pip install truststore`.",
              file=sys.stderr)
        return ssl.create_default_context()


class Client:
    def __init__(self, ctx, delay=1.0, timeout=300, retries=3):
        self.ctx, self.delay, self.timeout, self.retries = ctx, delay, timeout, retries
        self.calls = 0

    def get(self, url: str) -> dict:
        for bad in FORBIDDEN:
            if bad in url.lower():
                raise RuntimeError(f"refusing write endpoint: {url}")
        last = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": UA, "Accept": "application/json"})
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
            except json.JSONDecodeError as e:
                # Truncated body: the server gave up part-way through serialising
                # a very large response. Halving the page is the only real fix.
                raise OversizeResponse(f"truncated at char {e.pos:,}") from e
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
            wait = self.delay * (2 ** attempt)
            print(f"      retry in {wait:.0f}s ({last})")
            time.sleep(wait)
        raise RuntimeError(f"gave up: {last}")

    def count(self, svc, lid) -> int:
        d = self.get(f"{BASE}/{svc}/MapServer/{lid}/query?"
                     + urllib.parse.urlencode(
                         {"where": "1=1", "returnCountOnly": "true", "f": "json"}))
        return d.get("count", 0)

    def ids(self, svc, lid) -> tuple[str, list]:
        """All object IDs. Works even where pagination is unsupported."""
        d = self.get(f"{BASE}/{svc}/MapServer/{lid}/query?"
                     + urllib.parse.urlencode(
                         {"where": "1=1", "returnIdsOnly": "true", "f": "json"}))
        if "error" in d:
            e = d["error"]
            raise ArcGISError(f"code {e.get('code')}: {e.get('message')}")
        return d.get("objectIdFieldName", "objectid"), (d.get("objectIds") or [])

    def by_ids(self, svc, lid, id_batch, precision=PRECISION) -> dict:
        """
        Fetch an explicit set of object IDs. This is the fallback for layers
        that reject resultOffset ("Pagination is not supported"), and it also
        sidesteps maxRecordCount truncation, since the server returns exactly
        the IDs asked for.
        """
        d = self.get(f"{BASE}/{svc}/MapServer/{lid}/query?"
                     + urllib.parse.urlencode({
                         "objectIds": ",".join(str(i) for i in id_batch),
                         "outFields": "*", "returnGeometry": "true",
                         "outSR": "4326", "geometryPrecision": precision,
                         "f": "geojson"}))
        if isinstance(d, dict) and "error" in d:
            e = d["error"]
            raise ArcGISError(f"code {e.get('code')}: {e.get('message')}")
        return d

    def page(self, svc, lid, offset, size=PAGE, precision=PRECISION) -> dict:
        d = self.get(f"{BASE}/{svc}/MapServer/{lid}/query?"
                     + urllib.parse.urlencode({
                         "where": "1=1", "outFields": "*",
                         "returnGeometry": "true", "outSR": "4326",
                         "geometryPrecision": precision,
                         "resultOffset": offset, "resultRecordCount": size,
                         "f": "geojson"}))
        # ArcGIS reports failures as HTTP 200 with an `error` object. Without
        # this check an errored layer looks like "no more features" and the
        # layer is silently recorded as complete with 0 rows.
        if isinstance(d, dict) and "error" in d:
            e = d["error"]
            raise ArcGISError(f"code {e.get('code')}: {e.get('message')} "
                              f"{'; '.join(e.get('details') or [])}")
        return d


# --------------------------------------------------------------------------- #
# cleaning
# --------------------------------------------------------------------------- #

def norm_status(v):
    if not v:
        return None
    s = re.sub(r"\s+", " ", str(v).strip())
    return STATUS_CANON.get(s.upper(), s)


def bbox_of(geom):
    """Bounding box of a GeoJSON geometry, or None if empty."""
    if not geom:
        return None
    xs, ys = [], []

    def walk(c):
        if not c:
            return
        if isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for p in c:
                walk(p)

    walk(geom.get("coordinates"))
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def annotate(feat: dict, layer: str) -> dict:
    """Add provenance and quality flags. Never drops anything."""
    props = feat.get("properties") or {}
    geom = feat.get("geometry")
    bb = bbox_of(geom)

    props["_layer"] = layer
    props["_pulled_at"] = datetime.now(timezone.utc).date().isoformat()

    # flag rather than delete: you need these to quantify data quality honestly
    props["flag_empty_geometry"] = bb is None
    if bb:
        x0, y0, x1, y1 = bb
        props["flag_outside_india"] = not (
            x0 >= INDIA[0] and y0 >= INDIA[1] and x1 <= INDIA[2] and y1 <= INDIA[3])
        # a polygon spanning >20 degrees is torn, not merely misplaced
        props["flag_torn_geometry"] = (x1 - x0) > 20 or (y1 - y0) > 20
    else:
        props["flag_outside_india"] = None
        props["flag_torn_geometry"] = None

    for k in ("proposal_status", "proposal_s", "status_yet"):
        if k in props:
            props["status_clean"] = norm_status(props[k])
            props["is_decided"] = props["status_clean"] in DECIDED
            break

    # the key you should group by for ANY published rate. Rows are patches.
    for k in ("proposal_no", "proposal_n"):
        if props.get(k):
            props["proposal_key"] = props[k]
            break

    feat["properties"] = props
    return feat


# --------------------------------------------------------------------------- #
# pull
# --------------------------------------------------------------------------- #

ID_BATCH = 200          # object IDs per request; shrinks on oversize


def pull_by_ids(cli, svc, lid, name, path, ckpt, total, fh) -> dict:
    """Fallback for layers that reject resultOffset. Fetches explicit ID batches."""
    _, all_ids = cli.ids(svc, lid)
    print(f"      {len(all_ids):,} object IDs retrieved")
    start = 0
    if os.path.exists(ckpt):
        start = int(open(ckpt).read().strip() or 0)
        if start:
            print(f"      resuming at index {start:,}")

    flags = {"empty": 0, "outside": 0, "torn": 0}
    batch = ID_BATCH
    i = start
    while i < len(all_ids):
        chunk = all_ids[i:i + batch]
        try:
            d = cli.by_ids(svc, lid, chunk)
        except (OversizeResponse, ArcGISError, RuntimeError) as e:
            if batch <= 10:
                raise RuntimeError(f"failing at batch size {batch}: {e}") from e
            batch = max(10, batch // 2)
            print(f"      batch too large ({e}) -> {batch}")
            continue
        for f in d.get("features") or []:
            f = annotate(f, name)
            p = f["properties"]
            flags["empty"] += bool(p.get("flag_empty_geometry"))
            flags["outside"] += bool(p.get("flag_outside_india"))
            flags["torn"] += bool(p.get("flag_torn_geometry"))
            fh.write(json.dumps(f, ensure_ascii=False) + "\n")
        i += len(chunk)
        fh.flush()
        open(ckpt, "w").write(str(i))
        print(f"      {i:,}/{len(all_ids):,}", end="\r", flush=True)

    print(f"      {i:,}/{len(all_ids):,}  done"
          f"   [empty {flags['empty']}, outside {flags['outside']}, torn {flags['torn']}]")
    if os.path.exists(ckpt):
        os.remove(ckpt)
    return {"service": svc, "layer_id": lid, "name": name,
            "expected": total, "written": i, "flags": flags, "mode": "objectIds",
            "file": path, "pulled_at": datetime.now(timezone.utc).isoformat()}


def pull_layer(cli: Client, svc: str, lid: int, name: str, resume: bool) -> dict:
    path = os.path.join(OUT, f"{name}.geojsonl")
    ckpt = os.path.join(OUT, f"{name}.offset")

    total = cli.count(svc, lid)
    offset = 0
    if resume and os.path.exists(ckpt):
        offset = int(open(ckpt).read().strip() or 0)
        print(f"    resuming at offset {offset:,}")
    elif os.path.exists(path):
        os.remove(path)

    size = PAGE_OVERRIDE.get((svc, lid), PAGE)
    print(f"    {total:,} features  (page {size})")
    written = offset
    flags = {"empty": 0, "outside": 0, "torn": 0}

    with open(path, "a", encoding="utf-8") as fh:
        while offset < total:
            try:
                d = cli.page(svc, lid, offset, size)
            except ArcGISError as e:
                if "pagination" in str(e).lower():
                    print("      pagination unsupported -> fetching by object ID")
                    return pull_by_ids(cli, svc, lid, name, path, ckpt, total, fh)
                raise RuntimeError(
                    f"server rejected the query ({e}). Check the layer "
                    f"in a browser.") from e
            except (OversizeResponse, RuntimeError) as e:
                if size <= PAGE_FLOOR:
                    raise RuntimeError(
                        f"still failing at page size {size}: {e}") from e
                size = max(PAGE_FLOOR, size // 2)
                print(f"      response too large ({e}) -> page size {size}")
                continue
            feats = d.get("features") or []
            if not feats:
                if offset == 0:
                    raise RuntimeError(
                        "server returned 0 features at offset 0 despite "
                        f"count={total:,} -- layer did not download")
                break
            for f in feats:
                f = annotate(f, name)
                p = f["properties"]
                flags["empty"] += bool(p.get("flag_empty_geometry"))
                flags["outside"] += bool(p.get("flag_outside_india"))
                flags["torn"] += bool(p.get("flag_torn_geometry"))
                fh.write(json.dumps(f, ensure_ascii=False) + "\n")
            offset += len(feats)
            written = offset
            fh.flush()
            open(ckpt, "w").write(str(offset))
            print(f"      {offset:,}/{total:,}", end="\r", flush=True)
    print(f"      {written:,}/{total:,}  done"
          f"   [empty {flags['empty']}, outside {flags['outside']}, torn {flags['torn']}]")

    if os.path.exists(ckpt):
        os.remove(ckpt)
    return {"service": svc, "layer_id": lid, "name": name,
            "expected": total, "written": written, "flags": flags,
            "file": path, "pulled_at": datetime.now(timezone.utc).isoformat()}


def to_parquet(name: str) -> str | None:
    try:
        import geopandas as gpd  # noqa: PLC0415
    except ImportError:
        return None
    src = os.path.join(OUT, f"{name}.geojsonl")
    dst = os.path.join(OUT, f"{name}.parquet")
    try:
        gdf = gpd.read_file(src, driver="GeoJSONSeq")
        gdf.to_parquet(dst, index=False)
        return dst
    except Exception as e:  # noqa: BLE001
        print(f"      parquet failed for {name}: {e}")
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sensitivity", action="store_true",
                    help="also pull ecological baseline layers")
    ap.add_argument("--include-legacy", action="store_true",
                    help="also pull Parivesh 1.0 (patch-level, known defects)")
    ap.add_argument("--only", nargs="*", help="restrict to these output names")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--cafile")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    layers = list(CLEARANCE)
    if args.sensitivity:
        layers += SENSITIVITY
    if args.include_legacy:
        layers += LEGACY
        print("\nNOTE: legacy 1.0 layers are PATCH-level and contain known "
              "defects.\n      Group by proposal_key before quoting any rate.\n")
    if args.only:
        # --only searches EVERY catalogue, so you don't also have to remember
        # --sensitivity or --include-legacy just to re-pull one layer.
        pool = CLEARANCE + SENSITIVITY + LEGACY
        layers = [l for l in pool if l[2] in args.only]
        unknown = set(args.only) - {l[2] for l in pool}
        if unknown:
            print(f"  unknown layer names: {', '.join(sorted(unknown))}")
            print(f"  available: {', '.join(sorted(l[2] for l in pool))}")
        if not layers:
            sys.exit("nothing to do")

    os.makedirs(OUT, exist_ok=True)
    cli = Client(build_ssl_context(args.cafile, args.insecure), delay=args.delay)

    manifest = {"generated_at": datetime.now(timezone.utc).isoformat(),
                "base": BASE, "india_box": INDIA, "layers": []}
    mpath = os.path.join(OUT, "manifest.json")
    # Always merge. Replacing the manifest destroys the provenance record --
    # counts, flags and timestamps -- for layers pulled in earlier runs, which
    # is precisely what someone verifying a published dataset needs.
    if os.path.exists(mpath):
        try:
            prior = json.load(open(mpath, encoding="utf-8"))
            manifest["layers"] = prior.get("layers", [])
            manifest["first_generated_at"] = prior.get(
                "first_generated_at", prior.get("generated_at"))
            print(f"  merging into existing manifest "
                  f"({len(manifest['layers'])} layers already recorded)")
        except Exception as e:  # noqa: BLE001
            print(f"  could not read existing manifest ({e}); starting fresh")

    done = {l["name"] for l in manifest["layers"]}
    for svc, lid, name in layers:
        if name in done and args.resume and not os.path.exists(
                os.path.join(OUT, f"{name}.offset")):
            print(f"  [skip] {name}")
            continue
        print(f"  {svc}/{lid} -> {name}")
        try:
            rec = pull_layer(cli, svc, lid, name, args.resume)
        except Exception as e:  # noqa: BLE001
            print(f"    FAILED: {e}")
            continue
        rec["parquet"] = to_parquet(name)
        manifest["layers"] = [l for l in manifest["layers"] if l["name"] != name]
        manifest["layers"].append(rec)
        json.dump(manifest, open(mpath, "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)

    print(f"\n{'='*70}")
    tot = sum(l["written"] for l in manifest["layers"])
    print(f"{len(manifest['layers'])} layers, {tot:,} features -> {OUT}/")
    incomplete = []
    for l in manifest["layers"]:
        f = l["flags"]
        warn = ""
        if f["empty"] or f["outside"] or f["torn"]:
            warn = f"   flags: empty={f['empty']} outside={f['outside']} torn={f['torn']}"
        short = l["written"] < l["expected"]
        if short:
            incomplete.append(l)
            warn += f"   *** INCOMPLETE: expected {l['expected']:,}"
        print(f"  {l['name']:<24}{l['written']:>8,}{warn}")
    if incomplete:
        print(f"\n!!! {len(incomplete)} LAYER(S) INCOMPLETE -- do not analyse until fixed:")
        for l in incomplete:
            print(f"      {l['name']}: {l['written']:,} of {l['expected']:,}")
    print(f"\n{cli.calls} HTTP requests. Manifest: {mpath}")
    print("\nREMINDER: rows are patches, not proposals. "
          "Group by proposal_key for rates.")


if __name__ == "__main__":
    main()