"""Build compact geometry for the PH Hackathon Intelligence Map.

Input : faeldon/philippines-json-maps (PSGC-based, low-res, WGS84 lon/lat)
Output: geo.json with
  regions   : [name, ...]
  provinces : [[name, regionIndex], ...]
  dots      : flat [lon, lat, provinceIndex, ...]  (hex grid clipped to land)
  outlines  : [[provinceIndex, [lon,lat,lon,lat,...]], ...]  simplified rings
"""
import glob, json, math, sys
from shapely.geometry import shape, Point
from shapely.prepared import prep
from shapely import STRtree

SRC = sys.argv[1]
OUT = sys.argv[2]
STEP = float(sys.argv[3]) if len(sys.argv) > 3 else 0.05

regions, provinces, geoms = [], [], []
files = sorted(glob.glob(f"{SRC}/latest/geojson/regions/lowres/provdists-region-*.0.001.json"),
               key=lambda p: int(p.split("region-")[1].split(".")[0]))
country = json.load(open(f"{SRC}/latest/geojson/country/lowres/country.0.001.json"))
reg_name = {f["properties"]["adm1_psgc"]: f["properties"]["adm1_en"] for f in country["features"]}

for path in files:
    data = json.load(open(path))
    psgc = data["features"][0]["properties"]["adm1_psgc"]
    regions.append(reg_name.get(psgc, str(psgc)))
    ri = len(regions) - 1
    for f in data["features"]:
        g = shape(f["geometry"])
        if g.is_empty:
            continue
        if not g.is_valid:
            g = g.buffer(0)
        provinces.append([f["properties"]["adm2_en"], ri])
        geoms.append(g)

tree = STRtree(geoms)
prepared = [prep(g) for g in geoms]

# Hex-offset grid: every other row shifted half a step -> halftone look.
dots = []
minx, miny, maxx, maxy = 116.8, 4.5, 126.7, 21.2
row = 0
lat = miny
while lat <= maxy:
    dx = STEP / math.cos(math.radians(lat))  # keep dots roughly square on the ground
    lon = minx + (dx / 2 if row % 2 else 0)
    while lon <= maxx:
        p = Point(lon, lat)
        for idx in tree.query(p):
            if prepared[idx].contains(p):
                dots += [round(lon, 3), round(lat, 3), int(idx)]
                break
        lon += dx
    lat += STEP * 0.866
    row += 1

# Islands smaller than the grid step get no dots; give each one a single dot
# at a point guaranteed inside it so islands like Boracay still appear.
have = set()
for k in range(0, len(dots), 3):
    have.add((dots[k], dots[k + 1]))
grid_pts = [Point(dots[k], dots[k + 1]) for k in range(0, len(dots), 3)]
pt_tree = STRtree(grid_pts) if grid_pts else None
extra = 0
for i, g in enumerate(geoms):
    parts = [g] if g.geom_type == "Polygon" else list(g.geoms)
    for part in parts:
        if part.area < 0.0003:
            continue
        hit = [j for j in pt_tree.query(part) if part.contains(grid_pts[j])]
        if not hit:
            rp = part.representative_point()
            dots += [round(rp.x, 3), round(rp.y, 3), i]
            extra += 1
print("island dots added:", extra)

outlines = []
for i, g in enumerate(geoms):
    s = g.simplify(0.012, preserve_topology=True)
    polys = [s] if s.geom_type == "Polygon" else list(s.geoms)
    for poly in polys:
        if poly.area < 0.002:  # drop tiny islets from outline layer (dots still show them)
            continue
        ring = [c for xy in poly.exterior.coords for c in (round(xy[0], 3), round(xy[1], 3))]
        outlines.append([i, ring])

json.dump({"regions": regions, "provinces": provinces, "dots": dots, "outlines": outlines},
          open(OUT, "w"), separators=(",", ":"))
print(f"regions={len(regions)} provinces={len(provinces)} dots={len(dots)//3} outlines={len(outlines)}")
