"""
Restore the North China Plain regional PM2.5 data for the PEMDT spatial-coupling
study (study2). This is a SEPARATE copy of download_openaq.py, reconfigured for the
six North China Plain cities and writing to a NEW filename so it never touches the
Nordic data that the other studies depend on.

WHY this exists: the original regional_pm25.csv was pulled from OpenAQ with Chinese
cities, then the download script was later repointed at the Nordics and re-run, which
overwrote that file. This copy reconstructs the Chinese pull from the city list and
coordinates used in study2_regional_spatial.ipynb.

OUTPUT (both under data/openaq_regional/, NEW names, Nordic files untouched):
    data/openaq_regional/regional_pm25_ncp.csv   [datetime, value, station, latitude, longitude]
    data/openaq_regional/stations_ncp.csv        (station coverage listing)

Then point the networked-variant notebook at regional_pm25_ncp.csv.

USAGE
-----
1.  pip install requests pandas
2.  Put your OpenAQ key in the environment (recommended):
        export OPENAQ_API_KEY="your_key"     # macOS/Linux
        setx  OPENAQ_API_KEY "your_key"      # Windows (then open a NEW terminal)
3.  TEST the key:        python download_openaq_ncp.py --test
4.  DRY RUN (see which stations/coverage exist, no big download, REVIEW THE DATES):
        python download_openaq_ncp.py --dry-run
5.  Full pull:          python download_openaq_ncp.py

IMPORTANT: OpenAQ is a live service and Chinese-city coverage is historically limited
and may have changed since the original pull. Use --dry-run first to see the real date
coverage per station, then set DATE_FROM/DATE_TO below to a window those stations cover.
A fresh pull may not reproduce the published 0.844 -> 0.857 exactly; confirm before citing.
"""
from __future__ import annotations
import os, sys, time
import requests
import pandas as pd

# ----------------------------- configuration -----------------------------
API_KEY = os.environ.get("OPENAQ_API_KEY", "YOUR_API_KEY")
BASE = "https://api.openaq.org/v3"
PM25_PARAMETER_ID = 2          # OpenAQ parameter id for PM2.5
RADIUS_M = 25000               # search radius per city (25 km = v3 max)
MAX_STATIONS_PER_CITY = 5       # Chinese cities typically have several reference monitors

# North China Plain corridor, roughly South -> North along the prevailing transport
# path (Handan/Xingtai up to Beijing), matching study2_regional_spatial.ipynb. The
# spatial-coupling study needs this S->N ordering so transported pollution reaches the
# southern cities first (upwind) and Beijing later (downwind), giving a real time lag.
CITIES = {
    "Handan":       (36.61, 114.49),   # S
    "Xingtai":      (37.07, 114.50),
    "Shijiazhuang": (38.04, 114.51),
    "Baoding":      (38.87, 115.46),
    "Tianjin":      (39.08, 117.20),
    "Beijing":      (39.90, 116.41),   # N
}

# Date window RECOVERED from the original run's compiled bytecode
# (scripts/__pycache__/download_openaq.cpython-310.pyc), which held these exact bounds
# alongside the six Chinese cities. This is the window the original Chinese pull used, so
# it is the most faithful starting point. If --dry-run shows coverage has since changed,
# adjust. (The original also wrote to an "openaq_data/" folder; we use data/openaq_regional/.)
DATE_FROM = "2018-11-01T00:00:00Z"   # ISO-8601  (recovered from old .pyc)
DATE_TO   = "2020-03-31T00:00:00Z"   # recovered from old .pyc

LOWCOST_ONLY = "--lowcost" in sys.argv
OUT_DIR = "data/openaq_regional"                 # same folder, NEW filenames (below)
STATIONS_FILE = "stations_ncp.csv"
REGIONAL_FILE = "regional_pm25_ncp.csv"
# ------------------------------------------------------------------------
HEADERS = {"X-API-Key": API_KEY}


def _get(path, params=None, retries=5):
    """GET with rate-limit retry. Returns parsed JSON, or None on a 4xx (after
    printing the full error body so the exact problem is visible)."""
    for attempt in range(retries):
        r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params or {}, timeout=60)
        if r.status_code == 429:
            wait = 12 * (attempt + 1)
            print(f"  ! 429 rate-limited; waiting {wait}s ({attempt+1}/{retries})")
            time.sleep(wait); continue
        if r.status_code == 200:
            return r.json()
        print(f"  ! HTTP {r.status_code} on GET {path}")
        print(f"    params: {params}")
        print(f"    body:   {r.text[:500]}")
        return None
    print("  ! gave up after retries"); return None


def test_key():
    print("Testing API key against the docs' known-good endpoint /v3/locations/8118 ...")
    j = _get("/locations/8118")
    if j and j.get("results"):
        print("OK — key works. Station:", j["results"][0].get("name"))
        return True
    print("FAILED — if HTTP 401 above, the key/header is the problem; "
          "if 403, the key may be unverified; otherwise see the body above.")
    return False


def find_pm25_sensors():
    """Find PM2.5 sensors near each city. PM2.5 filtering is done client-side."""
    found = []
    for city, (lat, lon) in CITIES.items():
        j = _get("/locations", {"coordinates": f"{lat},{lon}", "radius": RADIUS_M, "limit": 100})
        if j is None:
            j = _get("/locations", {"coordinates": f"{lon},{lat}", "radius": RADIUS_M, "limit": 100})
        kept = 0
        for loc in (j or {}).get("results", []):
            is_monitor = loc.get("isMonitor")
            if LOWCOST_ONLY and is_monitor is not False:
                continue
            sensor = next((s for s in loc.get("sensors", [])
                           if (s.get("parameter") or {}).get("id") == PM25_PARAMETER_ID), None)
            if not sensor:
                continue
            c = loc.get("coordinates") or {}
            first = (loc.get("datetimeFirst") or {}).get("utc")
            last = (loc.get("datetimeLast") or {}).get("utc")
            found.append({"city": city, "station": loc.get("name"),
                          "lat": c.get("latitude"), "lon": c.get("longitude"),
                          "sensor_id": sensor.get("id"),
                          "sensor_type": "low-cost" if is_monitor is False else "reference",
                          "data_from": first, "data_to": last})
            kept += 1
            if kept >= MAX_STATIONS_PER_CITY:
                break
        print(f"{city:13s}: {kept} PM2.5 station(s)")
        time.sleep(0.6)
    return found


def fetch_hours(sensor_id, data_from=None, data_to=None):
    """Pull hourly PM2.5 for one sensor via /v3/sensors/{id}/hours, chunked + paginated.
    The window is CLAMPED to the sensor's actual coverage (data_from/data_to)."""
    out = []
    req_from, req_to = pd.to_datetime(DATE_FROM, utc=True), pd.to_datetime(DATE_TO, utc=True)
    cov_from = pd.to_datetime(data_from, utc=True) if data_from else None
    cov_to = pd.to_datetime(data_to, utc=True) if data_to else None
    if cov_from is not None and cov_to is not None:
        start, end = max(req_from, cov_from), min(req_to, cov_to)
        if start >= end:
            end = cov_to; start = max(cov_from, cov_to - pd.Timedelta(days=183))
    else:
        start, end = req_from, req_to
    print(f"    window: {start.date()} -> {end.date()}")
    cur = start
    param_names = ["datetime_from", "datetime_to"]
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=20), end)
        f_iso, t_iso = cur.strftime("%Y-%m-%dT%H:%M:%SZ"), nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
        page = 1
        while True:
            params = {param_names[0]: f_iso, param_names[1]: t_iso, "limit": 1000, "page": page}
            j = _get(f"/sensors/{sensor_id}/hours", params)
            if j is None and param_names[0] == "datetime_from":
                param_names = ["date_from", "date_to"]
                print("    (retrying with date_from/date_to param names)")
                continue
            if j is None:
                break
            res = j.get("results", [])
            if not res:
                break
            for m in res:
                per = m.get("period") or {}
                dt = (per.get("datetimeFrom") or {}).get("utc") or (per.get("datetimeTo") or {}).get("utc")
                out.append({"datetime": dt, "value": m.get("value")})
            if len(res) < 1000:
                break
            page += 1; time.sleep(1.0)
        cur = nxt; time.sleep(1.0)
    return out


def main(dry_run=False):
    if API_KEY in ("", "PASTE_YOUR_KEY_HERE"):
        sys.exit("Set OPENAQ_API_KEY or paste your key into API_KEY at the top.")
    os.makedirs(OUT_DIR, exist_ok=True)
    sensors = find_pm25_sensors()
    print(f"\nTotal PM2.5 stations found: {len(sensors)}")
    if sensors:
        pd.DataFrame(sensors).to_csv(os.path.join(OUT_DIR, STATIONS_FILE), index=False)
    if dry_run:
        print("\nStation data coverage (this is the key info):")
        for s in sensors:
            print(f"  {s['city']:13s} {str(s['station'])[:22]:22s}  {s.get('data_from')}  ->  {s.get('data_to')}")
        print("\nReview the dates above. Set DATE_FROM/DATE_TO at the top of the script to a "
              "period these stations actually cover, then run without --dry-run.")
        return
    if not sensors:
        sys.exit("No PM2.5 stations found near the Chinese cities — OpenAQ coverage there may "
                 "be gone for this period. Run `--test` to check the key, or widen DATE range / CITIES.")

    rows = []
    for i, s in enumerate(sensors, 1):
        print(f"[{i}/{len(sensors)}] {s['city']} / {s['station']} (sensor {s['sensor_id']}) ...")
        for m in fetch_hours(s["sensor_id"], s.get("data_from"), s.get("data_to")):
            if m["datetime"] and m["value"] is not None:
                rows.append({"datetime": m["datetime"], "value": m["value"],
                             "station": f"{s['city']}::{s['station']}",
                             "latitude": s["lat"], "longitude": s["lon"]})
    df = pd.DataFrame(rows)
    if df.empty:
        sys.exit("No measurements returned — widen the date range or check coverage with --dry-run.")
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    df = df.dropna(subset=["datetime", "value"]).sort_values(["station", "datetime"])
    out = os.path.join(OUT_DIR, REGIONAL_FILE)
    df.to_csv(out, index=False)
    print(f"\nSaved {len(df):,} rows from {df.station.nunique()} stations -> {out}")
    print("Date span:", df.datetime.min(), "->", df.datetime.max())
    print("Next: point the networked-variant notebook at", out)


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_key()
    else:
        main(dry_run="--dry-run" in sys.argv)
