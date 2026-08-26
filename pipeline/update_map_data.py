#!/usr/bin/env python3
"""
update_map_data.py -- refresh the map's data files from PARIVESH, incrementally.

WHY A CENSUS AND NOT A DATE WATERMARK
-------------------------------------
The obvious design is "fetch everything granted since the last run". It does not
work here:

  * every field in KML_Edit is esriFieldTypeString, so there are no date
    literals -- only lexicographic comparison, which needs ISO text;
  * FC's date_of_stage_ii_approval is DD-MON-YYYY, which does not sort;
  * sync_date is when the KML geometry was last pushed to the GIS mirror, NOT
    when the attributes changed. Measured against the 8 Aug 2026 snapshot: of
    678 FC rows accorded Stage-II in Jun-Aug 2026, 670 (98.8%) carry a
    sync_date PRECEDING the approval -- the KML lands at Stage-I, months
    earlier. A sync_date watermark would miss essentially every new FC grant.
    EC fares better (35/3836 = 0.9% missed) but still drifts, permanently.

So instead: census every layer's ATTRIBUTES on each run (no geometry -- 2000 EC
rows come back in ~420 KB and ~1.4 s, versus a geometry pull that runs to
hundreds of MB and times out), diff against what is already in the repo, and
fetch geometry only for genuinely new proposals.

That makes every run a full reconciliation. New grants, ToR->Granted flips,
back-dated entries and withdrawals are all picked up, and a missed run costs
nothing because there is no watermark to fall behind.

TWO STORES
----------
Status alone is not enough to decide what counts as a clearance -- see
form_class.py. Records are routed by form_type:

    map_v1/proposals/<STATE>.json           GRANT -- shown on the map
    map_v1/proposals_excluded/<STATE>.json  ADMIN -- transfers, amendments,
                                            extensions, corrigenda, splittings,
                                            surrenders. Published so the
                                            headline figure is reproducible and
                                            the classification is auditable.

Both use the same positional record, company inlined at index 9 --

    [lon, lat, typeIdx(0EC/1FC/2CRZ), year, area_ha, name, category,
     proposalNo, grantDate, company]

state_counts.json and data_meta.json are derived from the GRANT store on every
run. Because both stores are kept, changing a form's classification later is a
local edit -- no geometry has to be re-fetched from the government server.

Usage
-----
    python pipeline/update_map_data.py --dry-run
    python pipeline/update_map_data.py
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from parivesh_pull import (  # noqa: E402
    ArcGISError, Client, OversizeResponse, build_ssl_context, norm_status)

import form_class                    # noqa: E402
from pyproj import Geod              # noqa: E402
from shapely.geometry import shape   # noqa: E402

MAP = "map_v1"
STORES = {form_class.GRANT: os.path.join(MAP, "proposals"),
          form_class.ADMIN: os.path.join(MAP, "proposals_excluded")}
GEOD = Geod(ellps="WGS84")

# The proponent-name endpoint lives on the portal, not the GIS server.
PORTAL = "https://parivesh.nic.in/parivesh_api/trackYourProposal/dataOfProposalNo"

TNAME = {0: "EC", 1: "FC", 2: "CRZ"}

# Granted-status sets and date-field precedence are lifted verbatim from
# prep_proposals.py. They MUST stay in step: this script tops up the same store
# that a full rebuild regenerates, and a divergence would show as phantom
# additions and withdrawals on every subsequent run.
LAYERS = [
    {"svc": "KML_Edit", "lid": 0, "tidx": 0,
     "granted": {"EC Granted", "Granted"},
     "dates": ["granted_date"],
     "fields": ["objectid", "proposal_no", "proposal_status", "granted_date",
                "stname", "proposal_name", "category", "form_type"]},
    {"svc": "KML_Edit", "lid": 1, "tidx": 1,
     "granted": {"Stage-II Accorded", "GRANTED"},
     "dates": ["date_of_stage_ii_approval", "date_of_stage_i_approval"],
     "fields": ["objectid", "proposal_no", "proposal_status", "proposal_status_1",
                "date_of_stage_ii_approval", "date_of_stage_i_approval",
                "stname", "proposal_name", "category", "form_type"]},
    {"svc": "KML_Edit", "lid": 3, "tidx": 2,
     "granted": {"CRZ Granted"},
     "dates": ["granted_date"],
     "fields": ["objectid", "proposal_no", "proposal_status", "granted_date",
                "stname", "proposal_name", "category", "form_type"]},
]

# FC polygons are vertex-heavy enough to time out in large batches.
GEOM_BATCH = {1: 50}
DEFAULT_BATCH = 200

MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def yr(v):
    """Year out of either date dialect. 1899 is the null sentinel."""
    if not v or v in ("0", 0, "NA"):
        return None
    s = str(v).strip()
    if s.startswith("1899"):
        return None
    if len(s) >= 4 and s[:4].isdigit():
        return int(s[:4])
    p = s.split("-")
    if len(p) == 3 and p[1].upper()[:3] in MON and p[2][:4].isdigit():
        return int(p[2][:4])
    return None


def norm(name):
    """State name -> join key. Matches prep_boundaries/prep_proposals exactly."""
    s = str(name).upper().strip().replace("&", " AND ")
    s = re.sub(r"\bTHE\b", " ", s)
    s = re.sub(r"\bISLANDS?\b", " ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def status_of(a):
    """Same precedence prep_proposals uses: FC carries a null proposal_status
    and keeps the real one in proposal_status_1."""
    raw = a.get("proposal_status")
    return norm_status(raw) or raw or a.get("proposal_status_1")


def slug(k):
    return re.sub(r"\s+", "_", k)


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #

def load_store():
    """{bucket: {state: [record]}} and {proposal_no: (bucket, state, pos)}."""
    store = {b: {} for b in STORES}
    index = {}
    for bucket, d in STORES.items():
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if not f.endswith(".json"):
                continue
            key = f[:-5].replace("_", " ")
            recs = json.load(open(os.path.join(d, f), encoding="utf-8"))
            store[bucket][key] = recs
            for i, r in enumerate(recs):
                if r[7] in index:
                    print(f"  WARNING: duplicate proposal_no {r[7]}")
                index[r[7]] = (bucket, key, i)
    return store, index


def write_store(store, dirty):
    for bucket, key in sorted(dirty):
        d = STORES[bucket]
        os.makedirs(d, exist_ok=True)
        json.dump(store[bucket][key], open(os.path.join(d, slug(key) + ".json"),
                                           "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))


def rebuild_counts(store, display):
    """Derived from the GRANT store only -- what the map actually shows."""
    sc = {}
    for key, disp in display.items():
        byt = {t: collections.Counter() for t in ("EC", "FC", "CRZ")}
        for r in store[form_class.GRANT].get(key, []):
            byt[TNAME[r[2]]][r[3]] += 1
        sc[key] = {"name": disp,
                   "counts": {t: {str(y): n for y, n in sorted(byt[t].items())}
                              for t in ("EC", "FC", "CRZ")},
                   "total": sum(sum(byt[t].values()) for t in byt)}
    return sc


# --------------------------------------------------------------------------- #
# upstream
# --------------------------------------------------------------------------- #

def census_target(cli, cfg, keys, unmatched, unknown_forms):
    """Every currently-granted, dated, mappable proposal in one layer,
    each tagged with the bucket its form_type puts it in."""
    out = {}
    seen = 0
    for a in cli.census(cfg["svc"], cfg["lid"], cfg["fields"]):
        seen += 1
        if status_of(a) not in cfg["granted"]:
            continue
        year = gdate = None
        for d in cfg["dates"]:
            raw = a.get(d)
            year = yr(raw)
            if year:
                gdate = str(raw)
                break
        if not year:
            continue
        key = norm(a.get("stname"))
        if key not in keys:
            unmatched[a.get("stname")] += 1
            continue
        pno = a.get("proposal_no")
        if not pno:
            continue
        ft = a.get("form_type")
        bucket = form_class.classify(ft)
        if bucket == form_class.UNKNOWN:
            # Never guess. Counted here, acted on by the caller's guard.
            unknown_forms[ft or "(empty)"] += 1
            continue
        out[pno] = {
            "oid": a["objectid"], "tidx": cfg["tidx"], "year": year,
            "gdate": gdate, "state": key, "bucket": bucket,
            "form_cat": form_class.category(ft),
            "name": (a.get("proposal_name") or "").strip()[:120],
            "cat": (a.get("category") or a.get("form_type") or "").strip()[:60],
        }
    return out, seen


def fetch_geometry(cli, cfg, oids):
    """objectid -> (lon, lat, area_ha): centroid and geodesic area."""
    got = {}
    batch = GEOM_BATCH.get(cfg["lid"], DEFAULT_BATCH)
    i, oids = 0, sorted(oids)
    while i < len(oids):
        chunk = oids[i:i + batch]
        try:
            d = cli.by_ids(cfg["svc"], cfg["lid"], chunk)
        except (OversizeResponse, ArcGISError, RuntimeError) as e:
            if batch <= 10:
                raise RuntimeError(f"failing at batch size {batch}: {e}") from e
            batch = max(10, batch // 2)
            print(f"      batch too large ({e}) -> {batch}")
            continue
        for f in d.get("features") or []:
            oid = (f.get("properties") or {}).get("objectid")
            try:
                geom = shape(f["geometry"])
                c = geom.centroid
                a_m2, _ = GEOD.geometry_area_perimeter(geom)
            except Exception:  # noqa: BLE001 -- empty or degenerate geometry
                continue
            got[oid] = (round(c.x, 4), round(c.y, 4), round(abs(a_m2) / 10000, 2))
        i += len(chunk)
        print(f"      geometry {i:,}/{len(oids):,}", end="\r", flush=True)
    if oids:
        print(f"      geometry {len(got):,}/{len(oids):,} resolved" + " " * 12)
    return got


class Enricher:
    """Proponent names for new proposals, from the portal (~0.25 s each locally).
    The bulk advanceSearchData pull is 13 MB per state; for a few hundred new
    proposals a week this endpoint is cheaper by orders of magnitude.

    TIME-BOXED, AND FAILS OPEN. The names are a nice-to-have; the geometry and
    the counts are the product. An earlier version let this phase run unbounded
    with a 60 s per-call timeout, and when the portal turned out to be slow from
    a GitHub runner it consumed the entire 45-minute job and the run was killed
    having written nothing. So: a short per-call timeout, a budget for the whole
    phase, and a circuit breaker that gives up after a run of consecutive
    failures rather than proving the point 200 times.

    Whatever is missed is stored as "" and retried on the next run, which is the
    same path that already handles a lookup returning no name.
    """

    def __init__(self, ctx, delay=1.0, timeout=15, budget=600.0, max_fails=8):
        self.ctx, self.delay, self.timeout = ctx, delay, timeout
        self.budget, self.max_fails = budget, max_fails
        # Started on FIRST USE, not here. The Enricher is constructed before the
        # census; when a slow census ran 1,704s, a budget clocked from
        # construction was already spent and every lookup was skipped without a
        # single call being attempted. The budget is meant to bound the
        # enrichment phase, not the whole run.
        self.t0 = None
        self.fails = self.ok = self.skipped = 0
        self.off = None                      # reason enrichment stopped, if it did

    def _disable(self, why):
        self.off = why
        print(f"      proponent lookups disabled: {why}. "
              f"{self.ok} resolved, remainder left blank and retried next run")

    def name(self, pno) -> str:
        if self.off:
            self.skipped += 1
            return ""
        if self.t0 is None:
            self.t0 = time.monotonic()
        if time.monotonic() - self.t0 > self.budget:
            self._disable(f"budget of {self.budget:.0f}s spent")
            self.skipped += 1
            return ""
        url = PORTAL + "?" + urllib.parse.urlencode({"proposalNo": pno})
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Accept": "application/json"})
            with urllib.request.urlopen(req, context=self.ctx,
                                        timeout=self.timeout) as r:
                j = json.loads(r.read())
            rows = j.get("data") or []
            self.fails = 0
            self.ok += 1
            return (rows[0].get("nameOfUserAgency") or "").strip() if rows else ""
        except Exception as e:  # noqa: BLE001 -- retried on the next run
            self.fails += 1
            print(f"      company lookup failed for {pno}: "
                  f"{type(e).__name__}: {str(e)[:60]}")
            if self.fails >= self.max_fails:
                self._disable(f"{self.fails} consecutive failures")
            return ""
        finally:
            time.sleep(self.delay)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="report the diff, write nothing")
    ap.add_argument("--max-new", type=int, default=5000,
                    help="abort above this many additions; a bigger jump means "
                         "an upstream schema change, not news")
    ap.add_argument("--max-unmatched", type=int, default=100,
                    help="abort if this many rows carry an unrecognised stname")
    ap.add_argument("--max-unknown-forms", type=int, default=25,
                    help="abort if this many rows carry a form_type that is not "
                         "in form_class.py; they are never counted either way")
    ap.add_argument("--company-retries", type=int, default=200,
                    help="records whose proponent lookup failed earlier to retry")
    ap.add_argument("--company-timeout", type=float, default=15.0,
                    help="per-lookup timeout; short on purpose, these are optional")
    ap.add_argument("--company-budget", type=float, default=600.0,
                    help="seconds for the whole proponent phase before it gives "
                         "up and leaves the rest blank for the next run")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--cafile")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    counts_path = os.path.join(MAP, "state_counts.json")
    display = {k: v["name"] for k, v in
               json.load(open(counts_path, encoding="utf-8")).items()}
    store, index = load_store()
    stored_total = sum(len(v) for b in store.values() for v in b.values())
    print(f"store: {len(index):,} records "
          f"(grant {sum(len(v) for v in store[form_class.GRANT].values()):,} / "
          f"excluded {sum(len(v) for v in store[form_class.ADMIN].values()):,})")

    ctx = build_ssl_context(args.cafile, args.insecure)
    cli = Client(ctx, delay=args.delay)
    names = Enricher(ctx, delay=args.delay, timeout=args.company_timeout,
                     budget=args.company_budget)

    # ---- census -----------------------------------------------------------
    target, upstream = {}, {}
    unmatched, unknown_forms = collections.Counter(), collections.Counter()
    owner = {}                       # proposal_no -> layer config
    for cfg in LAYERS:
        label = TNAME[cfg["tidx"]]
        expected = cli.count(cfg["svc"], cfg["lid"])
        t0 = time.time()
        got, seen = census_target(cli, cfg, display, unmatched, unknown_forms)
        ng = sum(1 for r in got.values() if r["bucket"] == form_class.GRANT)
        print(f"  {label}: censused {seen:,}/{expected:,} rows -> {len(got):,} "
              f"classified ({ng:,} grant / {len(got) - ng:,} excluded) "
              f"({time.time() - t0:.0f}s)")
        # A census cut short by a transient error looks exactly like a mass
        # withdrawal upstream. Refuse to interpret it as one.
        if seen < expected * 0.99:
            print(f"ABORT: {label} census returned {seen:,} of {expected:,} rows")
            return 2
        if not ng:
            print(f"ABORT: {label} census produced no granted proposals")
            return 2
        upstream[label] = {"layer_rows": expected, "granted_mapped": ng,
                           "excluded": len(got) - ng}
        for pno, rec in got.items():
            if pno in target:
                print(f"  WARNING: {pno} present in two layers; keeping first")
                continue
            target[pno] = rec
            owner[pno] = cfg

    if unmatched:
        n = sum(unmatched.values())
        print(f"  unmatched stnames ({n} rows): {dict(unmatched)}")
        if n > args.max_unmatched:
            print(f"ABORT: {n} rows with unrecognised state names (> "
                  f"{args.max_unmatched}) -- upstream naming likely changed")
            return 2

    # An unrecognised form_type is neither counted nor silently dropped: the
    # whole point of the UNKNOWN bucket is that someone has to look at it.
    if unknown_forms:
        n = sum(unknown_forms.values())
        print(f"  UNKNOWN form_type ({n} rows) -- not classified:")
        for k, v in unknown_forms.most_common():
            print(f"      {v:>6,}  {k}")
        if n > args.max_unknown_forms:
            print(f"ABORT: {n} rows carry a form_type absent from form_class.py "
                  f"(> {args.max_unknown_forms}). Decide whether each is a grant "
                  f"or an administrative action and add it to the table.")
            return 2

    # ---- diff -------------------------------------------------------------
    new = sorted(set(target) - set(index))
    gone = sorted(set(index) - set(target))
    patch, moved = [], []
    for pno in set(index) & set(target):
        bucket, key, i = index[pno]
        r, t = store[bucket][key][i], target[pno]
        if bucket != t["bucket"] or key != t["state"]:
            moved.append(pno)
        elif (r[2] != t["tidx"] or r[3] != t["year"] or r[5] != t["name"]
              or r[6] != t["cat"] or r[8] != t["gdate"]):
            patch.append(pno)
    print(f"diff: +{len(new):,} new, -{len(gone):,} withdrawn, "
          f"~{len(patch):,} amended, {len(moved):,} reclassified/moved")

    if len(new) > args.max_new:
        print(f"ABORT: {len(new):,} additions exceeds --max-new {args.max_new:,}")
        return 2
    if len(target) < stored_total * 0.98:
        print(f"ABORT: upstream classified total {len(target):,} is more than 2% "
              f"below the stored {stored_total:,}")
        return 2

    # ---- geometry, only for genuine additions -----------------------------
    # Reclassification never refetches: the centroid and area are already held.
    geom = {}
    for cfg in LAYERS:
        oids = {target[p]["oid"] for p in new if owner[p] is cfg}
        if oids:
            print(f"  {TNAME[cfg['tidx']]}: fetching {len(oids):,} geometries")
            geom[cfg["lid"]] = fetch_geometry(cli, cfg, oids)

    # ---- plan, then apply -------------------------------------------------
    # Planned against the original indices, applied afterwards, so appending
    # never shifts a position that is still to be read.
    dirty, removals, appends = set(), [], []
    added = skipped = 0

    for pno in new:
        t = target[pno]
        g = geom.get(owner[pno]["lid"], {}).get(t["oid"])
        if not g:
            skipped += 1     # empty or degenerate geometry: as prep_proposals does
            continue
        lon, lat, area = g
        company = "" if args.dry_run else names.name(pno)
        appends.append((t["bucket"], t["state"],
                        [lon, lat, t["tidx"], t["year"], area, t["name"],
                         t["cat"], pno, t["gdate"], company]))
        added += 1

    for pno in moved:
        bucket, key, i = index[pno]
        t = target[pno]
        r = store[bucket][key][i]
        removals.append((bucket, key, i))
        appends.append((t["bucket"], t["state"],
                        [r[0], r[1], t["tidx"], t["year"], r[4], t["name"],
                         t["cat"], pno, t["gdate"], r[9]]))

    for pno in patch:                       # in place: same bucket, same state
        bucket, key, i = index[pno]
        t = target[pno]
        r = store[bucket][key][i]
        r[2], r[3], r[5], r[6], r[8] = (t["tidx"], t["year"], t["name"],
                                        t["cat"], t["gdate"])
        dirty.add((bucket, key))

    for pno in gone:
        removals.append(index[pno])

    for bucket, key, i in removals:
        store[bucket][key][i] = None
        dirty.add((bucket, key))
    for bucket, key, rec in appends:
        # Appended, never inserted: keeping the existing order stable keeps each
        # state file's git delta proportional to what actually changed.
        store[bucket].setdefault(key, []).append(rec)
        dirty.add((bucket, key))
    for bucket, key in dirty:
        store[bucket][key] = [r for r in store[bucket][key] if r is not None]

    # ---- retry proponent names that failed on an earlier run ---------------
    retried = 0
    if not args.dry_run and args.company_retries:
        for bucket in store:
            for key, recs in store[bucket].items():
                if retried >= args.company_retries:
                    break
                for r in recs:
                    if retried >= args.company_retries:
                        break
                    if not r[9]:
                        if names.off:          # circuit tripped; stop scanning
                            break
                        r[9] = names.name(r[7])
                        if r[9]:
                            dirty.add((bucket, key))
                        retried += 1

    grant = store[form_class.GRANT]
    total = sum(len(v) for v in grant.values())
    excl = sum(len(v) for v in store[form_class.ADMIN].values())
    by_type = collections.Counter(r[2] for v in grant.values() for r in v)
    # Counted from what actually landed in the excluded store, not from the
    # census: a new record whose geometry fails to resolve is skipped, and the
    # breakdown must still sum to the total the footnote prints.
    by_cat = collections.Counter(
        target[r[7]]["form_cat"]
        for v in store[form_class.ADMIN].values() for r in v if r[7] in target)
    missing_company = sum(1 for b in store.values() for v in b.values()
                          for r in v if not r[9])
    if not args.dry_run:
        print(f"proponents: {names.ok:,} resolved, {names.skipped:,} skipped"
              + (f" ({names.off})" if names.off else ""))
    print(f"result: {total:,} shown "
          f"(EC {by_type[0]:,} / FC {by_type[1]:,} / CRZ {by_type[2]:,}) "
          f"+ {excl:,} excluded, {len(dirty)} files touched, "
          f"{skipped} skipped for geometry, {missing_company} without a proponent")

    if args.dry_run:
        print("dry run -- nothing written")
    else:
        write_store(store, dirty)
        json.dump(rebuild_counts(store, display),
                  open(counts_path, "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        now = datetime.now(timezone.utc)
        # The timestamp stays UTC, but the date shown on the map is IST: the
        # cron fires at 20:30 UTC, which is already the next day in India, and
        # a map of Indian clearances that reports yesterday reads as stale.
        ist = now.astimezone(timezone(timedelta(hours=5, minutes=30)))
        json.dump({
            "generated_at": now.isoformat(timespec="seconds"),
            "snapshot_label": f"{ist.day} {ist:%b %Y}",
            "totals": {"EC": by_type[0], "FC": by_type[1], "CRZ": by_type[2],
                       "all": total},
            # The footnote on the map is rendered from these numbers rather than
            # typed into the HTML, so it cannot go stale between runs.
            "excluded": {"total": excl, "by_category": dict(by_cat.most_common())},
            "upstream": upstream,
            "last_change": {"added": added, "withdrawn": len(gone),
                            "amended": len(patch), "reclassified": len(moved)},
            "source": "PARIVESH 2.0 KML_Edit (EC/FC/CRZ), granted only",
        }, open(os.path.join(MAP, "data_meta.json"), "w", encoding="utf-8"),
            ensure_ascii=False, indent=1)
        print(f"wrote {len(dirty)} state files, state_counts.json, data_meta.json")

    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        changed = bool(added or gone or patch or moved)
        with open(gh, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")
            fh.write(f"summary=+{added} new, -{len(gone)} withdrawn, "
                     f"~{len(patch)} amended ({total} shown)\n")
    print(f"{cli.calls} GIS requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
