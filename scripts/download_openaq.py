"""
Download regional hourly PM2.5 from OpenAQ v3 for the PEMDT spatial-coupling study.
(Hardened + self-diagnosing version.)

WHY this design: spatial coupling needs stations FAR APART along a prevailing-wind
corridor so pollution transports between them with a time lag. Target here is
Scandinavia (Denmark -> southern Sweden -> Norway/Finland), where long-range
transported PM from continental Europe arrives via the south first.

USAGE
-----
1.  pip install requests pandas
2.  Put your key in the environment (recommended) OR paste it in API_KEY below:
        export OPENAQ_API_KEY="your_key"     # macOS/Linux
        setx  OPENAQ_API_KEY "your_key"      # Windows (then open a NEW terminal)
3.  TEST the key first (hits a known-good endpoint from the docs):
        python download_openaq.py --test
4.  DRY RUN to see what stations exist (no big download):
        python download_openaq.py --dry-run
5.  Full pull:
        python download_openaq.py
    Output -> data/openaq_regional/regional_pm25.csv  [datetime, value, station, latitude, longitude]

    For the sensor-reliability study, pull LOW-COST/community sensors instead:
        python download_openaq.py --lowcost
    Output -> data/openaq_lowcost/  (only stations where isMonitor == false)

Verified against the OpenAQ v3 docs: hourly data = /v3/sensors/{id}/hours ;
datetimes are ISO-8601 (e.g. 2023-11-01T00:00:00Z). If a call still fails it now
prints the FULL error body so the exact problem is visible.
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
MAX_STATIONS_PER_CITY = 5       # Nordic cities have fewer stations than Chinese ones
# Scandinavia has much cleaner air than China, so pollution "episodes" are milder and
# rarer -> we need a LONG multi-year window to accumulate enough events (exceedances of
# WHO/EU guidelines, not extreme smog). OpenAQ's European feed (from EEA) generally has
# good hourly coverage from ~2016 onward. Start wide; the dry-run prints each station's
# real coverage so we can trim. Episode threshold is set in the notebooks, not here.
# Dry-run (11 Nordic cities, 48 PM2.5 stations) showed a clean continuous SE+NO+FI
# network from ~2019 to a provider migration on 2024-01-29 (older Swedish station IDs
# all END then; new IDs START then). So we take the unbroken 5-year window before the
# break. Denmark/Aarhus are excluded automatically (their OpenAQ feed only starts 2023+).
# Stations outside this window yield nothing and are dropped (fetch clamps to coverage).
DATE_FROM = "2019-01-01T00:00:00Z"   # ISO-8601
DATE_TO   = "2024-01-28T00:00:00Z"
# --lowcost keeps only low-cost / community sensors (isMonitor == false) and writes to a
# separate folder — for the sensor-reliability (robustness) study. Default = reference monitors.
LOWCOST_ONLY = "--lowcost" in sys.argv
OUT_DIR = "data/openaq_lowcost" if LOWCOST_ONLY else "data/openaq_regional"
# Scandinavia / Nordics. Spread roughly S->N (Copenhagen/Malmo up to Trondheim) so
# that long-range transported pollution from continental Europe enters via Denmark /
# southern Sweden first -> a real upwind-to-downwind time lag for the spatial-coupling
# study. Dense southern cluster (Copenhagen-Malmo-Goteborg) + capitals.
CITIES = {
    "Copenhagen": (55.68, 12.57),   # DK
    "Aarhus":     (56.16, 10.20),   # DK
    "Malmo":      (55.61, 13.00),   # SE (south)
    "Gothenburg": (57.71, 11.97),   # SE (west)
    "Stockholm":  (59.33, 18.06),   # SE (capital)
    "Uppsala":    (59.86, 17.64),   # SE
    "Oslo":       (59.91, 10.75),   # NO
    "Bergen":     (60.39,  5.32),   # NO (west coast)
    "Trondheim":  (63.43, 10.39),   # NO (north)
    "Helsinki":   (60.17, 24.94),   # FI
    "Turku":      (60.45, 22.27),   # FI
}
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
        # non-retryable client/server error: show everything
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
    """Find PM2.5 sensors near each city. Locations query kept minimal
    (coordinates+radius only); PM2.5 filtering is done client-side."""
    found = []
    for city, (lat, lon) in CITIES.items():
        # NOTE: v3 geospatial radius search needs coordinates as "lon,lat"? docs use
        # "coordinates" with latitude,longitude. We send lat,lon; if 422, try swap.
        j = _get("/locations", {"coordinates": f"{lat},{lon}", "radius": RADIUS_M, "limit": 100})
        if j is None:
            j = _get("/locations", {"coordinates": f"{lon},{lat}", "radius": RADIUS_M, "limit": 100})
        kept = 0
        for loc in (j or {}).get("results", []):
            is_monitor = loc.get("isMonitor")   # True = reference/government monitor; False = low-cost
            if LOWCOST_ONLY and is_monitor is not False:
                continue                        # keep only community/low-cost sensors
            sensor = next((s for s in loc.get("sensors", [])
                           if (s.get("parameter") or {}).get("id") == PM25_PARAMETER_ID), None)
            if not sensor:
                continue
            c = loc.get("coordinates") or {}
            # capture each station's available data coverage (so we pull a real window)
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
        print(f"{city:9s}: {kept} {'low-cost ' if LOWCOST_ONLY else ''}PM2.5 station(s)")
        time.sleep(0.6)
    return found


def fetch_hours(sensor_id, data_from=None, data_to=None):
    """Pull hourly PM2.5 for one sensor via /v3/sensors/{id}/hours, chunked + paginated.
    The window is CLAMPED to the sensor's actual coverage (data_from/data_to); if the
    requested winter doesn't overlap, we fall back to the sensor's most recent 6 months.
    Tries datetime_from/datetime_to; falls back to date_from/date_to on rejection."""
    out = []
    req_from, req_to = pd.to_datetime(DATE_FROM, utc=True), pd.to_datetime(DATE_TO, utc=True)
    cov_from = pd.to_datetime(data_from, utc=True) if data_from else None
    cov_to = pd.to_datetime(data_to, utc=True) if data_to else None
    if cov_from is not None and cov_to is not None:
        start, end = max(req_from, cov_from), min(req_to, cov_to)
        if start >= end:                       # no overlap -> last 6 months of coverage
            end = cov_to; start = max(cov_from, cov_to - pd.Timedelta(days=183))
    else:
        start, end = req_from, req_to
    print(f"    window: {start.date()} -> {end.date()}")
    cur = start
    param_names = ["datetime_from", "datetime_to"]   # may flip to date_from/date_to on first 422
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=20), end)
        f_iso, t_iso = cur.strftime("%Y-%m-%dT%H:%M:%SZ"), nxt.strftime("%Y-%m-%dT%H:%M:%SZ")
        page = 1
        while True:
            params = {param_names[0]: f_iso, param_names[1]: t_iso, "limit": 1000, "page": page}
            j = _get(f"/sensors/{sensor_id}/hours", params)
            if j is None and param_names[0] == "datetime_from":
                param_names = ["date_from", "date_to"]            # fallback naming
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
        pd.DataFrame(sensors).to_csv(os.path.join(OUT_DIR, "stations.csv"), index=False)
    if dry_run:
        print("\nStation data coverage (this is the key info):")
        for s in sensors:
            print(f"  {s['city']:13s} {str(s['station'])[:22]:22s}  {s.get('data_from')}  ->  {s.get('data_to')}")
        print("\nReview the dates above. Set DATE_FROM/DATE_TO at the top of the script to a "
              "period these stations actually cover, then run without --dry-run.")
        return
    if not sensors:
        sys.exit("No PM2.5 stations found — run `--test` to check the key, or adjust CITIES.")

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
        sys.exit("No measurements returned — widen the date range or check coverage.")
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    df = df.dropna(subset=["datetime", "value"]).sort_values(["station", "datetime"])
    out = os.path.join(OUT_DIR, "regional_pm25.csv")
    df.to_csv(out, index=False)
    print(f"\nSaved {len(df):,} rows from {df.station.nunique()} stations -> {out}")
    print("Date span:", df.datetime.min(), "->", df.datetime.max())


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_key()
    else:
        main(dry_run="--dry-run" in sys.argv)
