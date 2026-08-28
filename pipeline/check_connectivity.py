#!/usr/bin/env python3
"""
check_connectivity.py -- can this machine reach PARIVESH, and how fast?

WHY THIS EXISTS
---------------
The two PARIVESH hosts behave very differently depending on where you call them
from, and the difference is not subtle. Same layer, same day, from GitHub's
network: the EC census took 109 s in one run and 1,677 s in another, with
[Errno 110] connection timeouts in between. From a laptop in India it takes
about 40 s, every time.

So when the map stops updating there are two very different explanations --
our code broke, or the government server will not talk to GitHub today -- and
telling them apart previously meant triggering a 30-minute update and reading
the log. This does it in under a minute, and reads the same from a laptop as
from a runner, so the two can be compared directly.

It also exercises the one path a --dry-run cannot: the proponent-name endpoint
on the portal. A dry run skips every lookup, so a green dry run has never said
anything about whether that host is reachable.

Read-only. Two cheap count queries per GIS layer and a handful of single
proposal lookups; far less traffic than one update run.

Usage
-----
    python pipeline/check_connectivity.py
    python pipeline/check_connectivity.py --names 10
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parivesh_pull import BASE, build_ssl_context  # noqa: E402

PORTAL = "https://parivesh.nic.in/parivesh_api/trackYourProposal/dataOfProposalNo"
STORE = os.path.join("map_v1", "proposals")
LAYERS = [("EC", 0), ("FC", 1), ("CRZ", 3)]


def timed(fn):
    t0 = time.monotonic()
    try:
        return fn(), None, time.monotonic() - t0
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:70]}", time.monotonic() - t0


def sample_proposals(n):
    """Real proposal numbers, preferring records that still have no proponent
    name -- the ones an update would actually be trying to resolve."""
    blank, any_ = [], []
    if not os.path.isdir(STORE):
        return []
    for f in sorted(os.listdir(STORE)):
        for r in json.load(open(os.path.join(STORE, f), encoding="utf-8")):
            (blank if not r[9] else any_).append(r[7])
            if len(blank) >= n:
                return blank[:n]
    return (blank + any_)[:n]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--names", type=int, default=5,
                    help="how many proponent lookups to time")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--cafile")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    ctx = build_ssl_context(args.cafile, args.insecure)

    def get(url):
        req = urllib.request.Request(
            url, headers={"User-Agent": "parivesh-connectivity-check/1.0",
                          "Accept": "application/json"})
        with urllib.request.urlopen(req, context=ctx, timeout=args.timeout) as r:
            return json.loads(r.read())

    results = {}

    print("\nGIS server  bharatmapsparivesh.parivesh.nic.in")
    print("  (a returnCountOnly query per layer -- the cheapest call there is)")
    gis_ok, gis_t = 0, []
    for label, lid in LAYERS:
        url = (f"{BASE}/KML_Edit/MapServer/{lid}/query?"
               + urllib.parse.urlencode({"where": "1=1", "returnCountOnly": "true",
                                         "f": "json"}))
        out, err, dt = timed(lambda u=url: get(u))
        if err:
            print(f"    {label:<4} FAIL  {dt:6.1f}s  {err}")
        else:
            gis_ok += 1
            gis_t.append(dt)
            print(f"    {label:<4} ok    {dt:6.1f}s  count={out.get('count'):,}")
    results["gis"] = (gis_ok, len(LAYERS))

    print("\nPortal      parivesh.nic.in  (proponent names)")
    pnos = sample_proposals(args.names)
    if not pnos:
        print("    no local store to sample proposal numbers from -- skipped")
        results["portal"] = (0, 0)
    else:
        print(f"  (dataOfProposalNo for {len(pnos)} real proposals, "
              f"preferring ones with no name stored)")
        p_ok, p_t = 0, []
        for pno in pnos:
            url = PORTAL + "?" + urllib.parse.urlencode({"proposalNo": pno})
            out, err, dt = timed(lambda u=url: get(u))
            if err:
                print(f"    FAIL  {dt:6.1f}s  {pno}  {err}")
            else:
                rows = out.get("data") or []
                name = (rows[0].get("nameOfUserAgency") or "").strip() if rows else ""
                p_ok += 1
                p_t.append(dt)
                print(f"    ok    {dt:6.1f}s  {pno}  -> "
                      f"{name[:44] or '(no name returned)'}")
            time.sleep(0.5)
        results["portal"] = (p_ok, len(pnos))

    print("\n" + "=" * 66)
    for host, (ok, total) in results.items():
        if not total:
            continue
        verdict = ("reachable" if ok == total
                   else "PARTIAL - intermittent" if ok else "UNREACHABLE")
        print(f"  {host:<8} {ok}/{total} succeeded - {verdict}")
    if gis_t:
        print(f"  GIS median {statistics.median(gis_t):.1f}s "
              f"(a healthy call is well under 2s)")

    # Exit non-zero only when a host is completely unreachable, so the run's
    # colour carries the answer without anyone opening the log.
    dead = [h for h, (ok, total) in results.items() if total and not ok]
    if dead:
        print(f"\n  UNREACHABLE from here: {', '.join(dead)}")
        return 1
    print("\n  Both hosts answered. If the map is stale, it is not connectivity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
