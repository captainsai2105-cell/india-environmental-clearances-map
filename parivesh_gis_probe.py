#!/usr/bin/env python3
"""
parivesh_gis_probe.py — read-only inventory of the Parivesh bharatmaps ArcGIS server.

Walks the service directory, enumerates layers, dumps field schemas and feature
counts, and flags layers that carry a joinable proposal identifier or a usable
India-bounds extent.

STRICTLY READ-ONLY. Issues GET requests to service/layer metadata and /query only.
It never touches applyEdits / addFeatures / updateFeatures / deleteFeatures, and it
skips FeatureServer endpoints entirely by default. Do not remove those guards: this
is government infrastructure, and write probing is not research.

TLS note
--------
This server sends an INCOMPLETE certificate chain (it omits the intermediate CA).
Browsers hide this by fetching the missing intermediate themselves; Python's
OpenSSL will not, so you get CERTIFICATE_VERIFY_FAILED / "unable to get local
issuer certificate". Fixes, best first:

  1. pip install truststore    -> verification via the OS trust store, which does
                                  chase missing intermediates. Used automatically
                                  if installed. This keeps verification ON.
  2. --cafile chain.pem        -> supply the intermediate yourself. In Chrome:
                                  padlock > Connection secure > Certificate is
                                  valid > Details, export the issuer (middle)
                                  cert as Base-64 .cer, and point --cafile at it.
  3. --insecure                -> last resort, verification off. Fine-ish for
                                  reading public map layers, but never reuse the
                                  habit anywhere credentials are involved.

Usage
-----
    pip install truststore          # recommended one-time fix
    python parivesh_gis_probe.py
    python parivesh_gis_probe.py --only Parivesh_Proposals parivesh1
    python parivesh_gis_probe.py --cafile parivesh_chain.pem
    python parivesh_gis_probe.py --insecure --no-counts --delay 2.0

Outputs
-------
    parivesh_gis_inventory.json   full machine-readable inventory
    parivesh_gis_inventory.csv    one row per layer, for eyeballing
    (plus a human-readable summary on stdout)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = ("https://bharatmapsparivesh.parivesh.nic.in"
        "/parivesh_bharatmaps/rest/services")
FOLDER = "parivesh2"

# India bounding box, generously padded. Used only to judge whether a layer's
# reported extent is sane -- not to filter features here.
INDIA_BBOX = (67.0, 6.0, 98.5, 37.5)  # xmin, ymin, xmax, ymax

# Field names that would let you join geometry back to the parivesh_api metadata.
JOIN_HINTS = re.compile(
    r"(?i)propos|proponent|caf|clearance|single.?window|sw_?no|file.?no|"
    r"proj(ect)?.?(id|no|name)|application|pk_?id|unique.?id"
)

# Endpoint fragments that must never be requested. Belt and braces.
FORBIDDEN = ("applyedits", "addfeatures", "updatefeatures", "deletefeatures",
             "calculate", "append", "truncate", "createreplica")

UA = "parivesh-gis-probe/1.0 (read-only public-data inventory)"


def build_ssl_context(cafile: str | None, insecure: bool) -> ssl.SSLContext:
    """
    The server omits its intermediate CA, so a stock context fails. Prefer the OS
    trust store (which chases missing intermediates), then an explicit chain file,
    then -- only if asked -- no verification at all.
    """
    if insecure:
        print("WARNING: TLS verification disabled (--insecure).", file=sys.stderr)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    if cafile:
        ctx = ssl.create_default_context(cafile=cafile)
        print(f"TLS: verifying against {cafile}")
        return ctx

    try:
        import truststore  # noqa: PLC0415 - optional dependency, probed at runtime
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        print("TLS: verifying via OS trust store (truststore)")
        return ctx
    except ImportError:
        print("TLS: stock certifi bundle. If this fails with "
              "'unable to get local issuer certificate', run:\n"
              "       pip install truststore\n"
              "     (or pass --cafile / --insecure -- see the docstring)",
              file=sys.stderr)
        return ssl.create_default_context()


class Probe:
    def __init__(self, delay: float = 1.0, timeout: int = 60,
                 want_counts: bool = True, include_featureservers: bool = False,
                 ssl_context: ssl.SSLContext | None = None):
        self.delay = delay
        self.timeout = timeout
        self.want_counts = want_counts
        self.include_featureservers = include_featureservers
        self.ssl_context = ssl_context
        self.calls = 0

    # ---------- transport ----------

    def get(self, url: str) -> dict | None:
        low = url.lower()
        for bad in FORBIDDEN:
            if bad in low:
                raise RuntimeError(f"refusing to request write endpoint: {url}")

        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self.ssl_context) as r:
                body = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return {"_error": f"HTTP {e.code}"}
        except urllib.error.URLError as e:
            reason = str(getattr(e, "reason", e))
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                return {"_error": "TLS chain incomplete -- see TLS note in docstring; "
                                  "try `pip install truststore`"}
            return {"_error": f"URLError: {reason}"}
        except Exception as e:  # noqa: BLE001 - report and continue
            return {"_error": f"{type(e).__name__}: {e}"}
        finally:
            self.calls += 1
            time.sleep(self.delay)

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            snippet = body.strip()[:120].replace("\n", " ")
            return {"_error": f"non-JSON response: {snippet}"}

    # ---------- traversal ----------

    def list_services(self) -> list[dict]:
        d = self.get(f"{BASE}/{FOLDER}?f=json") or {}
        if "_error" in d:
            sys.exit(f"could not list services: {d['_error']}")
        return d.get("services", [])

    def layer_count(self, svc: str, lid: int) -> int | str:
        """Feature count via returnCountOnly. Cheap and polite."""
        q = urllib.parse.urlencode({
            "where": "1=1", "returnCountOnly": "true", "f": "json",
        })
        d = self.get(f"{BASE}/{svc}/MapServer/{lid}/query?{q}") or {}
        if "_error" in d:
            return d["_error"]
        return d.get("count", "n/a")

    def probe_layer(self, svc: str, lid: int, lname: str) -> dict:
        meta = self.get(f"{BASE}/{svc}/MapServer/{lid}?f=json") or {}
        if "_error" in meta:
            return {"service": svc, "layer_id": lid, "layer_name": lname,
                    "error": meta["_error"]}

        fields = [
            {"name": f.get("name"), "alias": f.get("alias"), "type":
             (f.get("type") or "").replace("esriFieldType", "")}
            for f in meta.get("fields") or []
            if (f.get("type") or "") != "esriFieldTypeGeometry"
        ]
        join_fields = [f["name"] for f in fields if JOIN_HINTS.search(f["name"] or "")]

        ext = meta.get("extent") or {}
        sr = (ext.get("spatialReference") or meta.get("spatialReference") or {})
        wkid = sr.get("latestWkid") or sr.get("wkid")

        extent_ok = None
        if all(k in ext for k in ("xmin", "ymin", "xmax", "ymax")) and wkid == 4326:
            x0, y0, x1, y1 = INDIA_BBOX
            extent_ok = (ext["xmin"] >= x0 and ext["ymin"] >= y0
                         and ext["xmax"] <= x1 and ext["ymax"] <= y1)

        row = {
            "service": svc,
            "layer_id": lid,
            "layer_name": meta.get("name") or lname,
            "geometry": (meta.get("geometryType") or "").replace("esriGeometry", ""),
            "wkid": wkid,
            "max_record_count": meta.get("maxRecordCount"),
            "supports_pagination": bool(
                (meta.get("advancedQueryCapabilities") or {}).get("supportsPagination")
            ),
            "supports_statistics": bool(meta.get("supportsStatistics")),
            "capabilities": meta.get("capabilities"),
            "query_formats": meta.get("supportedQueryFormats"),
            "n_fields": len(fields),
            "join_candidates": join_fields,
            "extent": {k: ext.get(k) for k in ("xmin", "ymin", "xmax", "ymax")},
            "extent_within_india": extent_ok,
            "fields": fields,
        }
        if self.want_counts:
            row["feature_count"] = self.layer_count(svc, lid)
        return row

    def probe_service(self, svc: str) -> list[dict]:
        root = self.get(f"{BASE}/{svc}/MapServer?f=json") or {}
        if "_error" in root:
            return [{"service": svc, "error": root["_error"]}]
        rows = []
        # IDs are NOT sequential on this server (parivesh1 runs 0,1,2,5,4,3),
        # so always enumerate from the service JSON.
        for lyr in root.get("layers") or []:
            if lyr.get("type") != "Feature Layer":
                continue
            rows.append(self.probe_layer(svc, lyr["id"], lyr.get("name", "")))
        return rows


# ---------- reporting ----------

def write_outputs(rows: list[dict]) -> None:
    with open("parivesh_gis_inventory.json", "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)

    cols = ["service", "layer_id", "layer_name", "geometry", "wkid",
            "feature_count", "n_fields", "join_candidates",
            "extent_within_india", "capabilities", "error"]
    with open("parivesh_gis_inventory.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["join_candidates"] = "|".join(r.get("join_candidates") or [])
            w.writerow(r)


def summarise(rows: list[dict]) -> None:
    ok = [r for r in rows if "error" not in r]
    bad = [r for r in rows if "error" in r]

    print(f"\n{'='*78}\nPROBED {len(rows)} layers "
          f"({len(ok)} readable, {len(bad)} errored)\n{'='*78}")

    joinable = [r for r in ok if r["join_candidates"]]
    print(f"\n--- LAYERS WITH A JOINABLE IDENTIFIER ({len(joinable)}) ---")
    for r in sorted(joinable, key=lambda x: (x["service"], x["layer_id"])):
        cnt = r.get("feature_count", "?")
        print(f"  {r['service']}/{r['layer_id']:<2} {r['layer_name'][:34]:<34} "
              f"n={cnt:<9} -> {', '.join(r['join_candidates'])}")

    suspect = [r for r in ok if r["extent_within_india"] is False]
    if suspect:
        print(f"\n--- EXTENTS OUTSIDE INDIA (dirty geometry present) ({len(suspect)}) ---")
        for r in suspect:
            e = r["extent"]
            print(f"  {r['service']}/{r['layer_id']:<2} {r['layer_name'][:30]:<30} "
                  f"x[{e['xmin']:.1f},{e['xmax']:.1f}] y[{e['ymin']:.1f},{e['ymax']:.1f}]")

    mixed = sorted({r["wkid"] for r in ok if r["wkid"]})
    print(f"\n--- PROJECTIONS IN USE: {mixed} "
          f"(pass &outSR=4326 on every query) ---")

    if bad:
        print(f"\n--- ERRORS ({len(bad)}) ---")
        for r in bad:
            label = f"{r.get('service')}/{r.get('layer_id', '-')}"
            print(f"  {label:<44} {r['error']}")

    print("\nWrote parivesh_gis_inventory.json and .csv")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", metavar="NAME",
                    help="probe only these services (substring match), "
                         "e.g. --only Parivesh_Proposals parivesh1")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between requests (default 1.0; be polite)")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--no-counts", action="store_true",
                    help="skip feature counts (roughly halves request volume)")
    ap.add_argument("--cafile", metavar="PEM",
                    help="CA/intermediate bundle to verify against")
    ap.add_argument("--insecure", action="store_true",
                    help="disable TLS verification (last resort)")
    args = ap.parse_args()

    ctx = build_ssl_context(args.cafile, args.insecure)
    p = Probe(delay=args.delay, timeout=args.timeout,
              want_counts=not args.no_counts, ssl_context=ctx)

    services = [s["name"] for s in p.list_services()
                if s.get("type") == "MapServer"]
    if args.only:
        want = [w.lower() for w in args.only]
        services = [s for s in services if any(w in s.lower() for w in want)]

    print(f"{len(services)} MapServer services to probe "
          f"(FeatureServers deliberately skipped)")

    rows: list[dict] = []
    for i, svc in enumerate(services, 1):
        print(f"[{i}/{len(services)}] {svc}")
        rows.extend(p.probe_service(svc))

    write_outputs(rows)
    summarise(rows)
    print(f"Total HTTP requests: {p.calls}")


if __name__ == "__main__":
    main()