"""Split the single proposals.json into one small file per state for lazy loading.
Company name is inlined as index 9 so each state file is self-contained.
Run from repo root: python pipeline/split_states.py  ->  map_v1/proposals/<STATE>.json
"""
import json, os, re
OUT = "map_v1"
props = json.load(open(os.path.join(OUT, "proposals.json"), encoding="utf-8"))
comp = json.load(open(os.path.join(OUT, "company_by_pno.json"), encoding="utf-8"))
d = os.path.join(OUT, "proposals"); os.makedirs(d, exist_ok=True)
def slug(k): return re.sub(r"\s+", "_", k)
n = 0
for key, recs in props.items():
    out = [r + [comp.get(r[7], "")] for r in recs]   # append company as index 9
    json.dump(out, open(os.path.join(d, slug(key)+".json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    n += 1
print(f"wrote {n} per-state files to {d}")
