import json, os, re, sys, collections
from shapely.geometry import shape
from pyproj import Geod
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import form_class   # form_type -> GRANT / ADMIN / UNKNOWN; see that file for why

# Run from repo root. ROOT = raw GeoJSONL layers (produced by parivesh_pull.py).
ROOT = "out"
OUT = "map_v1"
GEOD = Geod(ellps="WGS84")
KEYS = json.load(open(os.path.join(OUT, "_state_keys.json"), encoding="utf-8"))  # key -> display

MON = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
def yr(v):
    if not v or v in ("0",0,"NA"): return None
    s=str(v).strip()
    if s.startswith("1899"): return None
    if len(s)>=4 and s[:4].isdigit(): return int(s[:4])
    p=s.split("-")
    if len(p)==3 and p[1].upper()[:3] in MON and p[2][:4].isdigit(): return int(p[2][:4])
    return None
def norm(name):
    s=str(name).upper().strip().replace("&"," AND ")
    s=re.sub(r"\bTHE\b"," ",s); s=re.sub(r"\bISLANDS?\b"," ",s)
    s=re.sub(r"[^A-Z ]"," ",s); s=re.sub(r"\s+"," ",s).strip()
    return s

CFG = [
    ("ec_proposals", 0, {"EC Granted","Granted"}, ["granted_date"]),
    ("fc_proposals", 1, {"Stage-II Accorded","GRANTED"}, ["date_of_stage_ii_approval","date_of_stage_i_approval"]),
    ("crz_proposals",2, {"CRZ Granted"}, ["granted_date"]),
]
TNAME = {0:"EC",1:"FC",2:"CRZ"}
counts = collections.defaultdict(lambda: {"EC":collections.Counter(),"FC":collections.Counter(),"CRZ":collections.Counter()})
proposals = collections.defaultdict(list)   # key -> records shown on the map
excluded  = collections.defaultdict(list)   # key -> administrative actions
unknown_forms = collections.Counter()
unmatched = collections.Counter()
grand = collections.Counter()

for name, tidx, gset, dfields in CFG:
    n=0; kept=0
    for line in open(os.path.join(ROOT,name+".geojsonl"), encoding="utf-8"):
        line=line.strip()
        if not line: continue
        n+=1
        feat=json.loads(line); pr=feat["properties"]
        st=str(pr.get("status_clean") or pr.get("proposal_status") or pr.get("proposal_status_1"))
        if st not in gset: continue
        y=None; gdate=None
        for d in dfields:
            raw=pr.get(d); y=yr(raw)
            if y: gdate=str(raw); break
        if not y: continue
        key=norm(pr.get("stname"))
        if key not in KEYS:
            unmatched[pr.get("stname")]+=1
            continue
        try:
            geom=shape(feat["geometry"])
            c=geom.centroid; lon,lat=round(c.x,4),round(c.y,4)
            a_m2,_=GEOD.geometry_area_perimeter(geom); area_ha=round(abs(a_m2)/10000,2)
        except Exception:
            continue
        nm=(pr.get("proposal_name") or "").strip()[:120]
        cat=(pr.get("category") or pr.get("form_type") or "").strip()[:60]
        # record: [lon,lat,typeIdx,year,area_ha,name,category,proposalNo,grantDate]
        rec=[lon,lat,tidx,y,area_ha,nm,cat,pr.get("proposal_no"),gdate]
        # Status alone over-counts: transfers, amendments, extensions,
        # corrigenda, splittings and surrenders all carry a granted status but
        # are actions on a clearance that already exists -- and their date is
        # the date of that action, not of the clearance.
        bucket=form_class.classify(pr.get("form_type"))
        if bucket==form_class.UNKNOWN:
            unknown_forms[pr.get("form_type") or "(empty)"]+=1
            continue
        if bucket==form_class.ADMIN:
            excluded[key].append(rec); continue
        kept+=1; grand[TNAME[tidx]]+=1
        counts[key][TNAME[tidx]][y]+=1
        proposals[key].append(rec)
        if kept%5000==0: print(f"  {name}: kept {kept:,}/{n:,}", flush=True)
    print(f"{name}: total lines {n:,}, granted+dated+mapped {kept:,}")

# build state_counts.json
sc={}
for key,disp in KEYS.items():
    tot=0; byt={}
    for t in ("EC","FC","CRZ"):
        yrs={str(k):v for k,v in counts[key][t].items()}
        byt[t]=yrs; tot+=sum(counts[key][t].values())
    sc[key]={"name":disp,"counts":byt,"total":tot}
json.dump(sc, open(os.path.join(OUT,"state_counts.json"),"w",encoding="utf-8"), ensure_ascii=False, separators=(",",":"))
json.dump(proposals, open(os.path.join(OUT,"proposals.json"),"w",encoding="utf-8"), ensure_ascii=False, separators=(",",":"))
json.dump(excluded, open(os.path.join(OUT,"proposals_excluded.json"),"w",encoding="utf-8"), ensure_ascii=False, separators=(",",":"))

print("\n=== SUMMARY ===")
print("grand totals:", dict(grand), "sum", sum(grand.values()))
print("proposals written:", sum(len(v) for v in proposals.values()))
print("state_counts states:", len(sc))
print("unmatched stnames (dropped):", dict(unmatched))
print("excluded (administrative actions):", sum(len(v) for v in excluded.values()))
if unknown_forms:
    print("!!! UNRECOGNISED form_type -- neither shown nor excluded; add them to "
          "pipeline/form_class.py:")
    for k,v in unknown_forms.most_common(): print(f"      {v:>6}  {k}")
print("proposals.json size MB:", round(os.path.getsize(os.path.join(OUT,'proposals.json'))/1e6,2))
