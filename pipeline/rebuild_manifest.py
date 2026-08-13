"""Rebuild out/manifest.json from the .geojsonl files already on disk.

OFFLINE. No network. Counts lines per file = feature count.
Preserves full provenance for any layer the existing manifest already records
(expected / flags / pulled_at / mode). Layers recovered only from disk are
marked rebuilt_from_disk with honest nulls -- no fabricated flags or expected.
"""
import json, os, sys, datetime

OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
mpath = os.path.join(OUT, "manifest.json")

# canonical (service, layer_id) per output name -- copied verbatim from
# parivesh_pull.py CLEARANCE + SENSITIVITY lists.
CANON = {
    "ec_proposals": ("KML_Edit", 0),
    "fc_proposals": ("KML_Edit", 1),
    "wl_proposals": ("KML_Edit", 2),
    "crz_proposals": ("KML_Edit", 3),
    "forest_land_bank": ("Parivesh_Proposals", 4),
    "protected_area_all": ("Protected_Area", 0),
    "national_park": ("Protected_Area", 2),
    "wildlife_sanctuary": ("Protected_Area", 3),
    "community_reserve": ("Protected_Area", 4),
    "conservation_reserve": ("Protected_Area", 5),
    "tiger_corridor": ("TigerCorridor", 0),
    "eco_sensitive_area": ("ESA", 0),
    "esz_gazette": ("ESZ", 0),
    "critically_polluted": ("CPCB_Polluted", 0),
    "severely_polluted": ("CPCB_Polluted", 1),
    "wildlife_corridor": ("Environment", 21),
    "tiger_reserve": ("Environment", 20),
}


def count_lines(path):
    n = 0
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    return n


prior = {}
if os.path.exists(mpath):
    try:
        pj = json.load(open(mpath, encoding="utf-8"))
        prior = {l["name"]: l for l in pj.get("layers", [])}
    except Exception as e:
        print(f"  (could not read existing manifest: {e})")

files = sorted(f for f in os.listdir(OUT) if f.endswith(".geojsonl"))
layers = []
for fn in files:
    name = fn[:-len(".geojsonl")]
    written = count_lines(os.path.join(OUT, fn))
    svc, lid = CANON.get(name, (None, None))
    pq = os.path.join(OUT, f"{name}.parquet")
    parquet = f"out/{name}.parquet" if os.path.exists(pq) else None

    if name in prior and prior[name].get("written") == written:
        rec = prior[name]          # provenance intact and count still matches
        rec["service"] = rec.get("service", svc)
        rec["layer_id"] = rec.get("layer_id", lid)
    else:
        rec = {
            "service": svc, "layer_id": lid, "name": name,
            "expected": None,            # unknown offline; not fabricated
            "written": written,
            "flags": None,               # not recomputed from geometry
            "file": f"out/{fn}",
            "pulled_at": prior.get(name, {}).get("pulled_at"),
            "parquet": parquet,
            "rebuilt_from_disk": True,
        }
    layers.append(rec)

order = list(CANON.keys())
layers.sort(key=lambda l: order.index(l["name"]) if l["name"] in order else 999)

manifest = {
    "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "base": "https://bharatmapsparivesh.parivesh.nic.in/parivesh_bharatmaps/rest/services/parivesh2",
    "india_box": [67.9, 6.5, 97.6, 37.3],
    "rebuilt_from_disk_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "layers": layers,
}
if os.path.exists(mpath):
    import shutil
    bak = mpath.replace(".json", ".pre_rebuild.json")
    shutil.copyfile(mpath, bak)
    print(f"  backed up old manifest -> {bak}")

json.dump(manifest, open(mpath, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

tot = sum(l["written"] for l in layers)
rebuilt = sum(1 for l in layers if l.get("rebuilt_from_disk"))
print(f"  wrote {len(layers)} layers, {tot:,} features -> {mpath}")
print(f"  {len(layers) - rebuilt} with intact provenance, {rebuilt} recovered from disk")
