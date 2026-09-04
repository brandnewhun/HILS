import json, time, urllib.request, urllib.parse, math

LAT0 = 37.27908611111111
LON0 = 127.10344722222221
SPACING_M = 250.0
RADIUS_M = 5000.0
N = int(2 * RADIUS_M / SPACING_M) + 1  # 41

M_PER_DEG_LAT = 111320.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(LAT0))
dlat = SPACING_M / M_PER_DEG_LAT
dlon = SPACING_M / M_PER_DEG_LON

half = (N - 1) // 2
pts = []
for i in range(N):
    lat = LAT0 + (i - half) * dlat
    for j in range(N):
        lon = LON0 + (j - half) * dlon
        pts.append([lat, lon])

print(f"N={N} total_pts={len(pts)} spacing={SPACING_M}m radius={RADIUS_M}m")

with open("elev_grid_points_5km.json", "w", encoding="utf-8") as f:
    json.dump({"N": N, "lat0": LAT0, "lon0": LON0, "dlat": dlat, "dlon": dlon, "spacingM": SPACING_M, "radiusM": RADIUS_M, "pts": pts}, f)

BATCH = 100
elevations = [None] * len(pts)
url_base = "https://api.opentopodata.org/v1/srtm30m"

for start in range(0, len(pts), BATCH):
    batch = pts[start:start+BATCH]
    loc_str = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in batch)
    url = url_base + "?locations=" + urllib.parse.quote(loc_str, safe="|,.")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.load(resp)
            if data.get("status") != "OK":
                raise RuntimeError(data)
            results = data["results"]
            for k, r in enumerate(results):
                elevations[start + k] = r["elevation"]
            print(f"batch {start}-{start+len(batch)-1} OK")
            break
        except Exception as e:
            print(f"batch {start} attempt {attempt} failed: {e}")
            time.sleep(3)
    time.sleep(1.1)

missing = sum(1 for e in elevations if e is None)
print(f"missing={missing}")

with open("elevations_srtm30m_5km.json", "w", encoding="utf-8") as f:
    json.dump({"N": N, "spacingM": SPACING_M, "radiusM": RADIUS_M, "elevations": elevations}, f)

valid = [e for e in elevations if e is not None]
print(f"min={min(valid)} max={max(valid)} mean={sum(valid)/len(valid):.1f}")
