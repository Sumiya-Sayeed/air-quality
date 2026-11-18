"""
Download ERA5 meteorology (hourly) for the Nordic station network and attach it to
each station, so the twin can use weather-driven dynamics (multimodal fusion).

WHY: the OpenAQ regional pull is PM2.5-only. ERA5 (Copernicus, free) provides
global hourly meteorology everywhere, so this fills the missing modality:
wind (u/v), temperature, dewpoint, surface pressure, boundary-layer height.

OUTPUT: data/era5/era5_regional.csv  with columns
    datetime, station, latitude, longitude, u10, v10, wind_speed, t2m, d2m, sp, blh

--------------------------------------------------------------------------------
ONE-TIME SETUP (verified against the current CDS docs)
--------------------------------------------------------------------------------
1. Create a free account at https://cds.climate.copernicus.eu and log in.
2. Put your credentials in  ~/.cdsapirc :
       url: https://cds.climate.copernicus.eu/api
       key: <YOUR-PERSONAL-ACCESS-TOKEN>
3. Install the client + readers:
       pip install "cdsapi>=0.7.7" xarray netCDF4 pandas numpy
4. On the dataset page, ACCEPT the licence once (manual, required):
       https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels
5. Run:
       python scripts/download_era5.py
   Requests queue on ECMWF servers and can take minutes-to-hours per month —
   this is normal. Files are cached in data/era5/raw/ so re-runs skip finished months.

NOTE: I cannot run this here (needs your CDS token + network). If a request errors,
paste the message and I'll adjust the variable names / request format.
"""
from __future__ import annotations
import os, sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
STATIONS_CSV = ROOT / "data" / "openaq_regional" / "stations.csv"
OUT_DIR = ROOT / "data" / "era5"
RAW_DIR = OUT_DIR / "raw"

DATASET = "reanalysis-era5-single-levels"
VARIABLES = [
    "10m_u_component_of_wind", "10m_v_component_of_wind",
    "2m_temperature", "2m_dewpoint_temperature",
    "surface_pressure", "boundary_layer_height",
]
SHORT = {"u10": "u10", "v10": "v10", "t2m": "t2m", "d2m": "d2m", "sp": "sp", "blh": "blh"}
# Match the Nordic PM2.5 window (Jan 2019 – Jan 2024; continuous pre-migration record).
MONTHS = pd.period_range("2019-01", "2024-01", freq="M")   # 61 months


def bbox_from_stations(df, pad=0.75):
    """CDS 'area' = [North, West, South, East] with a small pad around the stations."""
    n = df["lat"].max() + pad; s = df["lat"].min() - pad
    w = df["lon"].min() - pad; e = df["lon"].max() + pad
    return [round(n, 2), round(w, 2), round(s, 2), round(e, 2)]


def download_month(client, period, area):
    out = RAW_DIR / f"era5_{period}.nc"
    if out.exists() and out.stat().st_size > 0:
        return out
    request = {
        "product_type": ["reanalysis"],
        "variable": VARIABLES,
        "year": [str(period.year)],
        "month": [f"{period.month:02d}"],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": area,
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    print(f"  requesting {period} ...")
    client.retrieve(DATASET, request, str(out))
    return out


def extract_stations(nc_path, stations):
    """Sample the ERA5 grid at each station's nearest grid point -> tidy rows."""
    import xarray as xr
    ds = xr.open_dataset(nc_path)
    tname = "valid_time" if "valid_time" in ds.coords else ("time" if "time" in ds.coords else None)
    rows = []
    for _, s in stations.iterrows():
        sel = ds.sel(latitude=s["lat"], longitude=s["lon"], method="nearest")
        df = sel[[v for v in SHORT if v in ds]].to_dataframe().reset_index()
        df = df.rename(columns={tname: "datetime"})
        df["station"] = s["station"]; df["latitude"] = s["lat"]; df["longitude"] = s["lon"]
        rows.append(df)
    ds.close()
    return pd.concat(rows, ignore_index=True)


def main():
    if not STATIONS_CSV.exists():
        sys.exit(f"Missing {STATIONS_CSV}. Run scripts/download_openaq.py first (it writes stations.csv).")
    try:
        import cdsapi  # noqa
    except ImportError:
        sys.exit('Install the client:  pip install "cdsapi>=0.7.7" xarray netCDF4')
    stations = pd.read_csv(STATIONS_CSV).dropna(subset=["lat", "lon"])
    stations = stations.drop_duplicates("station")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    area = bbox_from_stations(stations)
    print(f"{len(stations)} stations | ERA5 area [N,W,S,E] = {area} | {len(MONTHS)} months")

    import cdsapi
    client = cdsapi.Client()
    frames = []
    for period in MONTHS:
        nc = download_month(client, period, area)
        frames.append(extract_stations(nc, stations))
    met = pd.concat(frames, ignore_index=True)
    met["datetime"] = pd.to_datetime(met["datetime"], utc=True)
    if {"u10", "v10"}.issubset(met.columns):
        met["wind_speed"] = np.hypot(met["u10"], met["v10"])
    met = met.sort_values(["station", "datetime"])
    out = OUT_DIR / "era5_regional.csv"
    met.to_csv(out, index=False)
    print(f"\nSaved {len(met):,} station-hours -> {out}")
    print("Merge with PM2.5 on (station, datetime) to get the multimodal regional panel.")


if __name__ == "__main__":
    main()
