"""Path B enrichment: pull advanceSearchData per clearance type x state census
code, extract proposalNo -> nameOfUserAgency. Checkpoints after every call so it
is resumable and safe to run in the background. WL (type 3) excluded from v1."""
import os, ssl, json, time, urllib.request

OUT = "map_v1"   # run from repo root
os.makedirs(OUT, exist_ok=True)
CH = os.path.join(OUT, "company_by_pno.json")
PROG = os.path.join(OUT, "enrich_progress.json")
try: ctx = __import__("truststore").SSLContext(ssl.PROTOCOL_TLS_CLIENT)
except Exception: ctx = ssl.create_default_context()

company = json.load(open(CH, encoding="utf-8")) if os.path.exists(CH) else {}
done = set(tuple(x) for x in (json.load(open(PROG)) if os.path.exists(PROG) else []))
TYPES = {1: "EC", 2: "FC", 4: "CRZ"}          # WL(3) excluded
STATES = list(range(1, 39))                    # 2011 census codes (skip empties)
base = "https://parivesh.nic.in/parivesh_api/trackYourProposal/advanceSearchData"

def save():
    json.dump(company, open(CH, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(sorted(list(done)), open(PROG, "w"))

added_total = 0
for t, tname in TYPES.items():
    for s in STATES:
        if (t, s) in done:
            continue
        url = f"{base}?majorClearanceType={t}&state={s}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            t0 = time.time()
            with urllib.request.urlopen(req, context=ctx, timeout=150) as r:
                j = json.loads(r.read())
            arr = j if isinstance(j, list) else (j.get("data") or j.get("content") or j.get("result") or [])
            n = 0
            for rec in arr:
                pno = rec.get("proposalNo")
                comp = (rec.get("nameOfUserAgency") or "").strip()
                if pno and comp:
                    company[pno] = comp; n += 1
            added_total += n
            done.add((t, s)); save()
            print(f"[{tname} state={s}] {len(arr)} recs, +{n} names ({time.time()-t0:.0f}s) | total {len(company):,}", flush=True)
        except Exception as e:
            print(f"[{tname} state={s}] ERROR {e}", flush=True)
        time.sleep(1.0)
print(f"DONE. {len(company):,} proposalNo->company mappings -> {CH}")
