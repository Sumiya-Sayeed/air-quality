"""
Download hourly NO2 for the Nordic network used in the PM2.5 studies (second-pollutant check).

Only stations that already have ERA5 meteorology (data/era5/era5_regional.csv) are kept, matched
by station name, so the NO2 series can be joined to the same meteorology as the PM2.5 series.

Writes NEW files only (nothing existing is touched):
    data/openaq_regional/regional_no2.csv   [datetime, value, units, station, latitude, longitude]
    data/openaq_regional/stations_no2.csv

China does not need a download: the Beijing files in data/beijing_air/ already contain NO2.

USAGE (run from the project root, on a machine with internet access)
    export OPENAQ_API_KEY="your_key"          # optional; falls back to the key in download_openaq.py
    python scripts/download_openaq_no2.py --dry-run     # list matching stations and their coverage
    python scripts/download_openaq_no2.py               # full download

The sensor is selected by parameter NAME ("no2"), not by numeric id, and the unit reported by
OpenAQ is stored with every row. Check the units column before analysis.
"""
from __future__ import annotations
import os, sys, time
import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from download_openaq import API_KEY as _FALLBACK_KEY, CITIES   # same key and Nordic city list
except Exception:                                                   # pragma: no cover
    _FALLBACK_KEY, CITIES = "", {}
API_KEY = os.environ.get("OPENAQ_API_KEY", _FALLBACK_KEY)

BASE = "https://api.openaq.org/v3"
PARAM_NAME = "no2"
RADIUS_M = 25000
DATE_FROM = "2019-01-01T00:00:00Z"     # same window as the PM2.5 pull
DATE_TO   = "2024-01-28T00:00:00Z"
OUT_DIR = "data/openaq_regional"
ERA5_FILE = "data/era5/era5_regional.csv"
HEADERS = {"X-API-Key": API_KEY}


def _get(path, params=None, retries=5):
    for attempt in range(retries):
        r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params or {}, timeout=60)
        if r.status_code == 429:
            wait = 12 * (attempt + 1)
            print(f"  ! rate-limited; waiting {wait}s ({attempt+1}/{retries})")
            time.sleep(wait); continue
        if r.status_code == 200:
            return r.json()
        print(f"  ! HTTP {r.status_code} on GET {path} | params {params} | body {r.text[:300]}")
        return None
    return None


def era5_station_names():
    names = pd.read_csv(ERA5_FILE, usecols=["station"])["station"].dropna().unique()
    return set(names)


def find_no2_sensors(keep_names):
    found, skipped = [], []
    for city, (lat, lon) in CITIES.items():
        j = _get("/locations", {"coordinates": f"{lat},{lon}", "radius": RADIUS_M, "limit": 100})
        for loc in (j or {}).get("results", []):
            sensor = next((s for s in loc.get("sensors", [])
                           if str((s.get("parameter") or {}).get("name", "")).lower() == PARAM_NAME), None)
            if not sensor:
                continue
            name = loc.get("name")
            c = loc.get("coordinates") or {}
            rec = {"city": city, "station": name, "lat": c.get("latitude"), "lon": c.get("longitude"),
                   "sensor_id": sensor.get("id"), "units": (sensor.get("parameter") or {}).get("units"),
                   "data_from": (loc.get("datetimeFirst") or {}).get("utc"),
                   "data_to": (loc.get("datetimeLast") or {}).get("utc")}
            (found if name in keep_names else skipped).append(rec)
        time.sleep(0.6)
    return found, skipped


def fetch_hours(sensor_id, data_from, data_to):
    """Hourly values inside DATE_FROM..DATE_TO. A sensor with no coverage in the window is skipped."""
    req_from, req_to = pd.to_datetime(DATE_FROM, utc=True), pd.to_datetime(DATE_TO, utc=True)
    start, end = req_from, req_to
    if data_from and data_to:
        start = max(req_from, pd.to_datetime(data_from, utc=True))
        end = min(req_to, pd.to_datetime(data_to, utc=True))
    if start >= end:
        print("    no coverage inside the window; skipped")
        return []
    print(f"    window: {start.date()} -> {end.date()}")
    out, cur, names = [], start, ["datetime_from", "datetime_to"]
    while cur < end:
        nxt = min(cur + pd.Timedelta(days=20), end)
        page = 1
        while True:
            params = {names[0]: cur.strftime("%Y-%m-%dT%H:%M:%SZ"), names[1]: nxt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "limit": 1000, "page": page}
            j = _get(f"/sensors/{sensor_id}/hours", params)
            if j is None and names[0] == "datetime_from":
                names = ["date_from", "date_to"]; continue
            if j is None:
                break
            res = j.get("results", [])
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
    if not API_KEY:
        sys.exit("Set OPENAQ_API_KEY (or keep scripts/download_openaq.py beside this file).")
    if not CITIES:
        sys.exit("Could not import the Nordic city list from scripts/download_openaq.py.")
    keep = era5_station_names()
    print(f"{len(keep)} stations have ERA5 meteorology; searching for NO2 sensors at those stations ...")
    sensors, skipped = find_no2_sensors(keep)
    print(f"\nNO2 sensors at stations with meteorology: {len(sensors)}   (other NO2 stations nearby, not used: {len(skipped)})")
    for s in sensors:
        print(f"  {s['city']:11s} {str(s['station'])[:28]:28s} {str(s['units']):8s} {s['data_from']} -> {s['data_to']}")
    if sensors:
        os.makedirs(OUT_DIR, exist_ok=True)
        pd.DataFrame(sensors).to_csv(os.path.join(OUT_DIR, "stations_no2.csv"), index=False)
    if dry_run or not sensors:
        if not sensors:
            print("No matching NO2 stations. Send this output back before changing anything.")
        return
    rows = []
    for i, s in enumerate(sensors, 1):
        print(f"[{i}/{len(sensors)}] {s['city']} / {s['station']} (sensor {s['sensor_id']})")
        for m in fetch_hours(s["sensor_id"], s["data_from"], s["data_to"]):
            if m["datetime"] and m["value"] is not None:
                rows.append({"datetime": m["datetime"], "value": m["value"], "units": s["units"],
                             "station": f"{s['city']}::{s['station']}", "latitude": s["lat"], "longitude": s["lon"]})
    df = pd.DataFrame(rows)
    if df.empty:
        sys.exit("No measurements returned.")
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
    df = df.dropna(subset=["datetime", "value"]).sort_values(["station", "datetime"])
    out = os.path.join(OUT_DIR, "regional_no2.csv")
    df.to_csv(out, index=False)
    print(f"\nSaved {len(df):,} rows from {df.station.nunique()} stations -> {out}")
    print("Units present:", df.units.unique().tolist(), "| span:", df.datetime.min(), "->", df.datetime.max())


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
