import geopandas as gpd, json, re, os
from shapely.geometry import mapping

# Run from repo root. Fetch the DataMeet Admin2 shapefile into ./boundaries/ first
# (see pipeline/README or ProjectNotes Appendix A for the source URL).
SHP = "boundaries/Admin2.shp"
OUT = "map_v1"
os.makedirs(OUT, exist_ok=True)

def norm(name):
    s = str(name).upper().strip()
    s = s.replace("&", " AND ")
    s = re.sub(r"\bTHE\b", " ", s)
    s = re.sub(r"\bISLANDS?\b", " ", s)
    s = re.sub(r"[^A-Z ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def round_coords(obj, nd=3):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(obj[0]), nd), round(float(obj[1]), nd)]
        return [round_coords(x, nd) for x in obj]
    return obj

g = gpd.read_file(SHP)
g["geometry"] = g["geometry"].simplify(0.01, preserve_topology=True)
feats, keys = [], {}
for _, row in g.iterrows():
    k = norm(row["ST_NM"])
    keys[k] = row["ST_NM"]
    geom = mapping(row["geometry"])
    geom["coordinates"] = round_coords(geom["coordinates"], 3)
    feats.append({"type": "Feature",
                  "properties": {"name": row["ST_NM"], "key": k},
                  "geometry": geom})
fc = {"type": "FeatureCollection", "features": feats}
p = os.path.join(OUT, "states.geojson")
json.dump(fc, open(p, "w", encoding="utf-8"), separators=(",", ":"))
print("wrote", p, os.path.getsize(p)//1024, "KB,", len(feats), "features")
json.dump(keys, open(os.path.join(OUT, "_state_keys.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("keys:", sorted(keys.keys()))
