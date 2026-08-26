"""Split the cold-build JSON into one small file per state for lazy loading.
Company name is inlined as index 9 so each state file is self-contained.

Two stores, matching update_map_data.py:
    map_v1/proposals/<STATE>.json           shown on the map (grants)
    map_v1/proposals_excluded/<STATE>.json  transfers, amendments, extensions,
                                            corrigenda, splittings, surrenders

The excluded set is published rather than discarded so the headline figure is
reproducible (shown + excluded = every granted-status record) and so changing a
form's classification later never needs geometry re-fetched from the server.

Run from repo root: python pipeline/split_states.py
"""
import json, os, re

OUT = "map_v1"
comp = json.load(open(os.path.join(OUT, "company_by_pno.json"), encoding="utf-8"))

def slug(k): return re.sub(r"\s+", "_", k)

for src, sub in (("proposals.json", "proposals"),
                 ("proposals_excluded.json", "proposals_excluded")):
    path = os.path.join(OUT, src)
    if not os.path.exists(path):
        print(f"  skip {src} (not built)")
        continue
    props = json.load(open(path, encoding="utf-8"))
    d = os.path.join(OUT, sub)
    os.makedirs(d, exist_ok=True)
    n = 0
    for key, recs in props.items():
        out = [r + [comp.get(r[7], "")] for r in recs]   # append company as index 9
        json.dump(out, open(os.path.join(d, slug(key) + ".json"), "w", encoding="utf-8"),
                  ensure_ascii=False, separators=(",", ":"))
        n += 1
    print(f"wrote {n} files to {d}  ({sum(len(v) for v in props.values()):,} records)")
