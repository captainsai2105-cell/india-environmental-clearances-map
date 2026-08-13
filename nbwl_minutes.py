#!/usr/bin/env python3
"""
nbwl_minutes.py -- scrape Standing Committee of NBWL meeting minutes and extract
every wildlife-clearance proposal number with its decision.

PURPOSE
-------
The Parivesh GIS `wl_proposals` layer holds ~609 features. To know whether that
is roughly complete or a fraction of reality, we need the ground truth: the
proposals SC-NBWL actually considered. The minutes are that ground truth, and
they carry proposal numbers in the same WL/.. format the GIS uses -- so the two
are directly joinable.

SOURCE
------
    https://forestsclearance.nic.in/FAC_Report_W.aspx

That page holds two tables. The first ("online Process") has a single dead 2019
row and is ignored. The second ("Wildlife Advisory Committee (offline Process)")
has ~85 rows, newest first, with columns: Sl.No | Agenda Date | Minutes Date.
Both date cells are links to PDFs under /writereaddata/Order_and_Release/
(minutes) and /writereaddata/Order_and_Release/Agenda/ (agendas).

Only MINUTES are downloaded. Minutes list every proposal tabled, including
deferrals, so agendas add nothing.

Filenames are irregular -- numeric prefix plus free text, sometimes containing
parentheses and dots -- so hrefs are read from the page rather than constructed.

KNOWN LIMIT
-----------
As of writing, the page stops at April 2025 (83rd meeting). Proposals submitted
after that cannot be checked against minutes that do not exist yet. The script
reports how many of your flagged proposals fall past the last meeting date.

Usage
-----
    pip install truststore pdfplumber
    python nbwl_minutes.py --from 2022-09
    python nbwl_minutes.py --from 2022-09 --check results/overlay_detail_direct.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

PAGE = "https://forestsclearance.nic.in/FAC_Report_W.aspx"
HOST = "https://forestsclearance.nic.in"
CACHE = "nbwl_pdfs"
OUT = "results"
UA = "nbwl-minutes/1.0 (public-record research)"

# Proposal numbers vary in segment count:
#   WL/RJ/ROAD/429009/2023        3 segments
#   WL/TN/MIN/QRY/462271/2024     4 segments  <- note the extra /QRY/
#   WL/BR/CommPost/468578/2024    mixed case
# Non-greedy middle so the 4-segment form still matches.
PROP_RE = re.compile(r"\bWL/[A-Za-z]{2}/[A-Za-z0-9/_\-]+?/\d{4,8}/\d{4}\b")

# Decision language used in the minutes, most specific first.
DECISIONS = [
    ("approved",   re.compile(r"decided to approve", re.I)),
    ("recommended", re.compile(r"decided to recommend", re.I)),
    ("deferred",   re.compile(r"decided to defer|deferred the proposal", re.I)),
    ("rejected",   re.compile(r"decided to reject|rejected the proposal", re.I)),
    ("returned",   re.compile(r"decided to return", re.I)),
]

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def build_ssl_context(insecure=False) -> ssl.SSLContext:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import truststore  # noqa: PLC0415
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


def fetch(url: str, ctx, timeout=120) -> bytes:
    # Some ASP.NET endpoints return a stub or an error page to bare clients,
    # so present normal browser headers.
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def parse_date(text: str):
    """'22 April 2025', '25th March 2019', '12-13 August 2014' -> (y, m)."""
    t = re.sub(r"(\d)(st|nd|rd|th)", r"\1", text.strip(), flags=re.I)
    m = re.search(r"([A-Za-z]{3,9})\s+(\d{4})", t)
    if m and m.group(1)[:3].lower() in MONTHS:
        return int(m.group(2)), MONTHS[m.group(1)[:3].lower()]
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)   # 6/6/2019
    if m:
        return int(m.group(3)), int(m.group(2))
    return None


def scrape_index(ctx, debug=False) -> list[dict]:
    """
    Read minutes links off the page.

    The page is ASP.NET, so link markup varies (quote style, casing, relative
    vs absolute, possibly __doPostBack). Rather than assume a shape, match any
    .pdf href permissively, then distinguish minutes from agendas by path:
    minutes live in /writereaddata/Order_and_Release/, agendas one level
    deeper in .../Agenda/.
    """
    raw = fetch(PAGE, ctx)
    html = raw.decode("utf-8", "replace")
    with open("nbwl_page.html", "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"  fetched {len(raw):,} bytes -> saved nbwl_page.html")

    low = html.lower()
    print(f"  diagnostics: '.pdf'={low.count('.pdf')}  "
          f"'writereaddata'={low.count('writereaddata')}  "
          f"'order_and_release'={low.count('order_and_release')}  "
          f"'__dopostback'={low.count('__dopostback')}")

    # permissive: single or double quotes, any casing, absolute or relative
    links = re.findall(r"""href\s*=\s*['"]([^'"]+?\.pdf)['"]([^>]*)>(.*?)</a>""",
                       html, re.S | re.I)
    print(f"  {len(links)} PDF links in page")

    rows = []
    for href, _attrs, label in links:
        if "/agenda/" in href.lower():
            continue
        label = re.sub(r"<[^>]+>", " ", label)
        label = re.sub(r"\s+", " ", label).strip()
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = HOST + href
        else:
            url = HOST + "/" + href
        rows.append({"minutes_date_text": label,
                     "date": parse_date(label),
                     "url": url})

    seen, out = set(), []
    for r in rows:
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)

    if not out:
        print("\n  NO MINUTES LINKS FOUND. Likely causes:")
        if low.count("__dopostback"):
            print("    - links are ASP.NET postbacks, not plain hrefs")
        if len(raw) < 5000:
            print(f"    - response is only {len(raw):,} bytes; possibly an error "
                  f"page or a redirect")
        print("    Open nbwl_page.html and search for 'Order_and_Release' to see "
              "the real markup.")
        snippet = html[:1200].replace("\n", " ")
        print(f"\n  first 1200 chars:\n  {snippet}\n")
    elif debug:
        for r in out[:5]:
            print(f"    {r['minutes_date_text'][:24]:<26}{r['url']}")
    return out


def cached_pdf(url: str, ctx, delay=2.0) -> str | None:
    os.makedirs(CACHE, exist_ok=True)
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    name = re.sub(r"[^A-Za-z0-9._()-]", "_", name)[:150]
    path = os.path.join(CACHE, name)
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    # parentheses and dots in filenames need encoding on the wire
    safe = urllib.parse.quote(url, safe=":/?&=")
    try:
        data = fetch(safe, ctx)
    except Exception as e:  # noqa: BLE001
        print(f"      download failed: {e}")
        return None
    if not data.startswith(b"%PDF"):
        print("      not a PDF, skipping")
        return None
    open(path, "wb").write(data)
    time.sleep(delay)
    return path


def extract(path: str, verbose=False) -> tuple[list[dict], dict]:
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError:
        sys.exit("needs pdfplumber:  pip install pdfplumber")
    with pdfplumber.open(path) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    text = "\n".join(pages)
    blank = sum(1 for p in pages if len(p.strip()) < 40)
    stats = {"pages": len(pages), "chars": len(text), "blank_pages": blank}

    matches = list(PROP_RE.finditer(text))
    out = {}
    for i, m in enumerate(matches):
        num = m.group()
        # Bound the window at the NEXT proposal number. A fixed-size window is
        # wrong here: minutes carry 20-30 numbered conditions after each
        # decision, so the gap between proposals runs to several thousand
        # characters -- a short window misses the decision, a long one steals
        # the following proposal's.
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        window = text[m.end():end]
        decision = next((lab for lab, rx in DECISIONS if rx.search(window)), None)
        # A proposal can appear twice: once under "Action Taken Report" (a
        # re-consideration) and again as a fresh item. Prefer the occurrence
        # that actually carries a decision.
        if num not in out or (decision and not out[num]["decision"]):
            out[num] = {"proposal_no": num, "decision": decision,
                        "occurrences": out.get(num, {}).get("occurrences", 0) + 1}
        else:
            out[num]["occurrences"] += 1

    if verbose or stats["chars"] < 2000:
        print(f"      pages={stats['pages']} chars={stats['chars']:,} "
              f"blank={blank}")
    if stats["chars"] < 2000 and stats["pages"] > 2:
        print("      WARNING: little text extracted -- likely a scanned PDF; "
              "proposal numbers cannot be read from it")
    return list(out.values()), stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="frm", default="2022-09",
                    help="earliest meeting month, YYYY-MM")
    ap.add_argument("--to", dest="to", default=None, help="latest, YYYY-MM")
    ap.add_argument("--check", metavar="CSV",
                    help="overlay detail CSV to cross-check against")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--verbose", action="store_true",
                    help="print page/char counts for every PDF")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    ctx = build_ssl_context(args.insecure)
    os.makedirs(OUT, exist_ok=True)

    def key(s):
        y, m = s.split("-")
        return int(y), int(m)
    lo = key(args.frm)
    hi = key(args.to) if args.to else (9999, 12)

    print(f"reading {PAGE}")
    idx = scrape_index(ctx, debug=args.verbose)
    print(f"  {len(idx)} minutes links found")
    inrange = [r for r in idx if r["date"] and lo <= r["date"] <= hi]
    print(f"  {len(inrange)} within {args.frm}..{args.to or 'latest'}")
    if not inrange:
        sys.exit("nothing in range")
    latest = max(r["date"] for r in inrange)
    print(f"  most recent meeting on the page: {latest[0]}-{latest[1]:02d}\n")

    all_props, per_meeting = [], []
    for i, r in enumerate(inrange, 1):
        print(f"[{i}/{len(inrange)}] {r['minutes_date_text']}")
        path = cached_pdf(r["url"], ctx, args.delay)
        if not path:
            continue
        props, stats = extract(path, args.verbose)
        print(f"      {len(props)} proposal numbers "
              f"({stats['pages']} pages, {stats['chars']:,} chars)")
        for p in props:
            p["meeting"] = r["minutes_date_text"]
            p["meeting_ym"] = f"{r['date'][0]}-{r['date'][1]:02d}"
        all_props.extend(props)
        per_meeting.append({"meeting": r["minutes_date_text"],
                            "ym": f"{r['date'][0]}-{r['date'][1]:02d}",
                            "n": len(props), "file": os.path.basename(path),
                            **stats})

    uniq = {}
    for p in all_props:
        uniq.setdefault(p["proposal_no"], p)

    csv_path = os.path.join(OUT, "nbwl_proposals.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["proposal_no", "decision",
                                           "occurrences", "meeting", "meeting_ym"],
                           extrasaction="ignore")
        w.writeheader()
        for p in uniq.values():
            w.writerow(p)

    print(f"\n{'='*66}")
    print(f"{len(inrange)} meetings, {len(all_props):,} mentions, "
          f"{len(uniq):,} distinct proposals")
    print(f"{'='*66}")
    print(f"  {'meeting':<22}{'props':>7}{'pages':>7}{'chars':>10}")
    for m in per_meeting:
        warn = "  <-- low yield" if m["n"] == 0 and m.get("pages", 0) > 2 else ""
        print(f"  {m['meeting'][:20]:<22}{m['n']:>7}{m.get('pages',0):>7}"
              f"{m.get('chars',0):>10,}{warn}")
    empty = [m for m in per_meeting if m["n"] == 0]
    if empty:
        print(f"\n  {len(empty)} meeting(s) yielded no proposal numbers -- either "
              f"scanned PDFs\n  or minutes predating the WL/ numbering scheme. "
              f"Check these by hand.")
    dec = {}
    for p in uniq.values():
        dec[p["decision"] or "(unclear)"] = dec.get(p["decision"] or "(unclear)", 0) + 1
    print("\ndecisions:", dec)

    # ---- cross-check ----
    if args.check and os.path.exists(args.check):
        rows = list(csv.DictReader(open(args.check, encoding="utf-8")))
        gis_wl = {r["proposal_key"] for r in rows
                  if r["layer"] == "wl_proposals"}
        minutes = set(uniq)
        print(f"\n{'='*66}\nCOVERAGE OF THE GIS wl_proposals LAYER\n{'='*66}")
        print(f"  in minutes                  : {len(minutes):,}")
        print(f"  in GIS (overlapping subset) : {len(gis_wl):,}")
        print(f"  in both                     : {len(minutes & gis_wl):,}")
        print(f"  minutes only (missing from GIS): {len(minutes - gis_wl):,}")
        if minutes:
            print(f"  -> GIS captures {100*len(minutes & gis_wl)/len(minutes):.1f}% "
                  f"of proposals named in minutes")

        # do any FLAGGED forest proposals appear in the minutes?
        fc = [r for r in rows if r["layer"] == "fc_proposals"]
        cafs = {r["caf_no"] for r in rows if r["layer"] == "wl_proposals"}
        strict = {"National Park", "Wildlife Sanctuary", "Tiger Reserve"}
        final = {"GRANTED", "Stage-II Accorded"}
        flagged = {r["proposal_key"] for r in fc
                   if r["sensitivity_type"] in strict
                   and r["relation"] == "within"
                   and r["status"] in final
                   and r["caf_no"] not in cafs}
        # FC numbers are FP/..; minutes list WL/.. -- match on the serial
        serial = lambda s: (re.search(r"/(\d{4,8})/", s or "") or [None, None])[1]
        min_serials = {serial(p) for p in minutes}
        hit = {p for p in flagged if serial(p) in min_serials}
        print(f"\n  flagged FC proposals              : {len(flagged):,}")
        print(f"  whose serial appears in minutes   : {len(hit):,}  "
              f"(these DID reach SC-NBWL)")
        print(f"  still unexplained                 : {len(flagged)-len(hit):,}")
        print("\n  NOTE: serial matching is indicative only -- FP and WL numbers")
        print("        are separate series. Confirm any hit by hand.")

        after = [r for r in fc if r["proposal_key"] in flagged
                 and (r["dos"] or "")[-4:].isdigit()
                 and int(r["dos"][-4:]) > latest[0]]
        if after:
            print(f"\n  {len({r['proposal_key'] for r in after})} flagged proposals "
                  f"postdate the last published minutes ({latest[0]}-{latest[1]:02d})")
            print("        -- these cannot be checked and must stay open.")

    json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
               "source": PAGE, "meetings": per_meeting,
               "distinct_proposals": len(uniq)},
              open(os.path.join(OUT, "nbwl_summary.json"), "w",
                   encoding="utf-8"), indent=2)
    print(f"\nWrote {csv_path}")


if __name__ == "__main__":
    main()