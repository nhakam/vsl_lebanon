"""
vsl_local.py  --  Variable Speed Limit helper for a single road location.

Local-machine version of python/read_from_API_colab.py.
Same idea (live weather + live traffic  ->  rule-based recommended speed) but it
runs from any terminal instead of Google Colab + Google Drive, and it works on a
single location you pass as latitude / longitude.

APIs
----
  * Traffic : Google Maps Platform - Routes API  (Compute Route Matrix)
              https://developers.google.com/maps/documentation/routes
              Billed per matrix element (1 origin x 1 destination = 1 element),
              with a monthly free allotment that is plenty for a prototype.
              Needs a Google Cloud API key with the "Routes API" enabled.

  * Weather : Google Maps Platform - Weather API (active), so everything runs
              off the one Google key. Needs the "Weather API" enabled.
              A free / no-key Open-Meteo (https://open-meteo.com) implementation
              is kept COMMENTED OUT in this file (see the "WEATHER: Open-Meteo"
              block) as a fallback - un-comment it and swap the call in main().

How congestion is measured (same meaning as the original script)
--------------------------------------------------------------
Google returns, for the driven segment:
    duration        travel time WITH current traffic
    staticDuration  travel time WITHOUT traffic (free flow)
    distanceMeters  segment length
        congestion_ratio = duration / staticDuration - 1
        current_speed    = distanceMeters / duration        (m/s -> km/h)
        free_flow_speed  = distanceMeters / staticDuration   (m/s -> km/h)

You give the point whose speed you care about (--lat/--lon). Traffic needs a
short segment, so either pass the other end (--dest-lat/--dest-lon) or let the
script create one automatically ~1.5 km away along --bearing (default: north).
Google snaps both ends to the nearest road.

Usage
-----
  python python/vsl_local.py --lat 34.364735 --lon 35.782127 --name Balamand
  python python/vsl_local.py --lat 34.364735 --lon 35.782127 \
                             --dest-lat 34.393990 --dest-lon 35.896412
  python python/vsl_local.py --lat 34.39399 --lon 35.896412 --base-speed 90
  python python/vsl_local.py            (no args -> it prompts for lat/lon)

The Google key is read from the GOOGLE_MAPS_API_KEY environment variable, or from
--key, or from the GOOGLE_MAPS_API_KEY constant below.
"""

import argparse
import csv
import math
import os
import sys
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("The 'requests' package is missing. Install it with:\n"
             "    python -m pip install requests")

# ==== CONFIG ==================================================================

# Paste your Google Maps Platform key here, or leave "" and use the
# GOOGLE_MAPS_API_KEY environment variable / --key instead.
GOOGLE_MAPS_API_KEY = ""

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vsl_dataset.csv")

DEFAULT_BASE_SPEED = 80        # km/h legal / design max for the road
AUTO_SEGMENT_KM = 1.5         # length of the auto-generated probe segment
DEFAULT_BEARING_DEG = 0       # 0 = north, 90 = east, 180 = south, 270 = west
HTTP_TIMEOUT = 20            # seconds

ROUTE_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
GOOGLE_WEATHER_URL = "https://weather.googleapis.com/v1/currentConditions:lookup"


# ==== GEOMETRY: build a probe segment from one point =========================

def offset_point(lat, lon, distance_km, bearing_deg):
    """Return (lat2, lon2) distance_km away from (lat, lon) along a compass bearing."""
    R = 6371.0088
    ang = distance_km / R
    br = math.radians(bearing_deg)
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(ang) +
                     math.cos(lat1) * math.sin(ang) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(ang) * math.cos(lat1),
                             math.cos(ang) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


# ==== TRAFFIC: Google Routes API - Compute Route Matrix =====================

def _seconds(v):
    # Google durations look like "123s" or "123.4s".
    if v is None:
        return None
    return float(str(v).rstrip("s"))


def get_traffic(origin, destination, api_key, optimal=False):
    o_lat, o_lon = origin
    d_lat, d_lon = destination

    body = {
        "origins": [{
            "waypoint": {"location": {"latLng": {
                "latitude": o_lat, "longitude": o_lon}}}
        }],
        "destinations": [{
            "waypoint": {"location": {"latLng": {
                "latitude": d_lat, "longitude": d_lon}}}
        }],
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL" if optimal else "TRAFFIC_AWARE",
        "units": "METRIC",
    }
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": ("originIndex,destinationIndex,condition,"
                             "distanceMeters,duration,staticDuration"),
    }

    resp = requests.post(ROUTE_MATRIX_URL, json=body, headers=headers,
                         timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        hint = ""
        low = resp.text.lower()
        if "api key not valid" in low or "api_key_invalid" in low:
            hint = ("\n-> The key string is wrong. Copy it again from "
                    "Google Cloud Console > APIs & Services > Credentials.")
        elif "permission_denied" in low or "has not been used" in low or "is disabled" in low:
            hint = ("\n-> Enable the 'Routes API' for this project: "
                    "https://console.cloud.google.com/apis/library/routes.googleapis.com")
        elif "referer" in low or "restrict" in low:
            hint = ("\n-> The key has HTTP-referrer/IP restrictions that block a "
                    "server call. Use an unrestricted key or an 'IP addresses' "
                    "restriction that includes this machine.")
        elif "billing" in low:
            hint = ("\n-> Enable billing for the project (required even inside "
                    "the free monthly allotment).")
        sys.exit(f"Google Routes API returned HTTP {resp.status_code}.{hint}\n\n"
                 f"Full response:\n{resp.text[:800]}")

    elements = resp.json()
    if not elements:
        sys.exit("Google returned an empty matrix.")
    el = elements[0]
    if el.get("condition") != "ROUTE_EXISTS":
        sys.exit("Google could not build a route between those points "
                 f"(condition = {el.get('condition')}). Move the coordinates "
                 "onto a drivable road, or pass --dest-lat/--dest-lon.")

    dist_m = float(el["distanceMeters"])
    dur_s = _seconds(el.get("duration"))
    static_s = _seconds(el.get("staticDuration")) or dur_s

    congestion_ratio = round(dur_s / static_s - 1, 3) if static_s else None
    current_speed = round(dist_m / dur_s * 3.6, 1) if dur_s else None
    free_flow_speed = round(dist_m / static_s * 3.6, 1) if static_s else None

    return {
        "distance_m": round(dist_m),
        "duration_s": round(dur_s) if dur_s else None,
        "static_duration_s": round(static_s) if static_s else None,
        "current_speed_kmh": current_speed,
        "free_flow_speed_kmh": free_flow_speed,
        "congestion_ratio": congestion_ratio,
    }


# ==== WEATHER: Open-Meteo (kept as a commented-out free / no-key fallback) ===
#
# Open-Meteo needs no API key and no sign-up. It is disabled by default so the
# script runs entirely on the one Google key. To use it instead of Google:
#   1. Un-comment the block below (_WMO_GROUPS, _wmo_to_condition,
#      get_weather_open_meteo).
#   2. In main(), swap:  weather = get_weather_google(lat, lon, key)
#                  for:  weather = get_weather_open_meteo(lat, lon)
#
# _WMO_GROUPS = {
#     "Clear":        {0},
#     "Clouds":       {1, 2, 3},
#     "Fog":          {45, 48},
#     "Drizzle":      {51, 53, 55, 56, 57},
#     "Rain":         {61, 63, 65, 66, 67, 80, 81, 82},
#     "Snow":         {71, 73, 75, 77, 85, 86},
#     "Thunderstorm": {95, 96, 99},
# }
#
#
# def _wmo_to_condition(code):
#     for name, codes in _WMO_GROUPS.items():
#         if code in codes:
#             return name
#     return "Unknown"
#
#
# def get_weather_open_meteo(lat, lon):
#     params = {
#         "latitude": lat,
#         "longitude": lon,
#         "current": ("temperature_2m,relative_humidity_2m,precipitation,"
#                     "weather_code,wind_speed_10m,wind_gusts_10m"),
#         "hourly": "visibility",
#         "wind_speed_unit": "ms",
#         "timezone": "auto",
#         "forecast_days": 1,
#     }
#     resp = requests.get("https://api.open-meteo.com/v1/forecast",
#                         params=params, timeout=HTTP_TIMEOUT)
#     resp.raise_for_status()
#     data = resp.json()
#     cur = data["current"]
#     code = int(cur["weather_code"])
#
#     visibility_m = 10000.0
#     hourly = data.get("hourly", {})
#     times, vis = hourly.get("time", []), hourly.get("visibility", [])
#     if times and vis:
#         prefix = cur["time"][:13]
#         idx = next((i for i, t in enumerate(times) if t.startswith(prefix)), 0)
#         if vis[idx] is not None:
#             visibility_m = float(vis[idx])
#
#     return {
#         "source": "open-meteo",
#         "temperature_C": cur["temperature_2m"],
#         "condition": _wmo_to_condition(code),
#         "detail": f"WMO {code}",
#         "wind_speed_ms": cur["wind_speed_10m"],
#         "wind_gust_ms": cur.get("wind_gusts_10m"),
#         "visibility_m": visibility_m,
#         "humidity_pct": cur["relative_humidity_2m"],
#         "precipitation_mm": cur.get("precipitation"),
#     }


# ==== WEATHER: Google Maps Platform Weather API (active source) =============

_GOOGLE_WX_GROUPS = {
    "Clear":        {"CLEAR", "MOSTLY_CLEAR"},
    "Clouds":       {"PARTLY_CLOUDY", "MOSTLY_CLOUDY", "CLOUDY", "WINDY"},
    "Fog":          {"FOG", "HAZE", "MIST"},
    "Drizzle":      {"LIGHT_RAIN_SHOWERS", "DRIZZLE", "LIGHT_RAIN"},
    "Rain":         {"RAIN_SHOWERS", "RAIN", "HEAVY_RAIN", "HEAVY_RAIN_SHOWERS",
                     "RAIN_AND_SNOW", "WIND_AND_RAIN", "SCATTERED_SHOWERS"},
    "Snow":         {"SNOW", "SNOW_SHOWERS", "HEAVY_SNOW", "LIGHT_SNOW",
                     "SNOWSTORM", "BLIZZARD", "FLURRIES"},
    "Thunderstorm": {"THUNDERSTORM", "THUNDERSHOWER", "HAIL", "SCATTERED_THUNDERSTORMS"},
}


def _google_wx_to_condition(wx_type):
    for name, types in _GOOGLE_WX_GROUPS.items():
        if wx_type in types:
            return name
    return "Unknown"


def get_weather_google(lat, lon, api_key):
    params = {
        "key": api_key,
        "location.latitude": lat,
        "location.longitude": lon,
        "unitsSystem": "METRIC",
    }
    resp = requests.get(GOOGLE_WEATHER_URL, params=params, timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        low = resp.text.lower()
        hint = ""
        if "api key not valid" in low or "api_key_invalid" in low:
            hint = "\n-> The key string is wrong. Copy it again from Google Cloud Console."
        elif ("permission_denied" in low or "has not been used" in low
              or "is disabled" in low or "not been enabled" in low):
            hint = ("\n-> Enable the 'Weather API' for this project: "
                    "https://console.cloud.google.com/apis/library/weather.googleapis.com"
                    "\n   (Or re-enable the commented-out Open-Meteo source in this "
                    "file - see the 'WEATHER: Open-Meteo' comment block.)")
        elif "referer" in low or "restrict" in low:
            hint = ("\n-> The key has HTTP-referrer/IP restrictions that block a "
                    "server call. Use an unrestricted key or an IP restriction "
                    "that includes this machine.")
        sys.exit(f"Google Weather API returned HTTP {resp.status_code}.{hint}\n\n"
                 f"Full response:\n{resp.text[:800]}")
    d = resp.json()

    wx_type = (d.get("weatherCondition") or {}).get("type", "")
    wind = d.get("wind") or {}
    wind_speed_kmh = ((wind.get("speed") or {}).get("value"))
    wind_gust_kmh = ((wind.get("gust") or {}).get("value"))
    vis_km = ((d.get("visibility") or {}).get("distance"))
    qpf = ((d.get("precipitation") or {}).get("qpf") or {}).get("quantity")

    return {
        "source": "google",
        "temperature_C": (d.get("temperature") or {}).get("degrees"),
        "condition": _google_wx_to_condition(wx_type),
        "detail": wx_type or "n/a",
        "wind_speed_ms": round(wind_speed_kmh / 3.6, 2) if wind_speed_kmh is not None else 0.0,
        "wind_gust_ms": round(wind_gust_kmh / 3.6, 2) if wind_gust_kmh is not None else None,
        "visibility_m": vis_km * 1000.0 if vis_km is not None else 10000.0,
        "humidity_pct": d.get("relativeHumidity"),
        "precipitation_mm": qpf,
    }


# ==== RULE-BASED RECOMMENDATION (same logic as the original) ================

def recommend_speed(weather, congestion_ratio, base_speed, free_flow_speed=None):
    if free_flow_speed:
        base_speed = min(base_speed, round(free_flow_speed))
    speed = base_speed

    if weather["condition"] in ("Rain", "Drizzle", "Thunderstorm"):
        speed -= 20
    elif weather["condition"] == "Fog":
        speed -= 25
    elif weather["condition"] == "Snow":
        speed -= 35

    if weather["visibility_m"] < 5000:
        speed -= 10
    if weather["wind_speed_ms"] > 12:
        speed -= 5

    if congestion_ratio is not None:
        if congestion_ratio > 0.30:
            speed -= 25
        elif congestion_ratio > 0.15:
            speed -= 15
        elif congestion_ratio > 0.05:
            speed -= 5

    return max(speed, 20)


# ==== CSV ==================================================================

CSV_HEADER = [
    "timestamp_utc", "location_name", "lat", "lon", "dest_lat", "dest_lon",
    "weather_source", "temperature_C", "condition", "condition_detail",
    "wind_speed_ms", "wind_gust_ms", "visibility_m", "humidity_pct",
    "precipitation_mm", "segment_distance_m", "duration_s", "static_duration_s",
    "current_speed_kmh", "free_flow_speed_kmh", "congestion_ratio",
    "hour_local", "base_speed", "recommended_speed",
]


def append_row(row):
    new_file = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(CSV_HEADER)
        w.writerow(row)


# ==== MAIN ================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Recommend a speed limit for one road location from live "
                    "weather + Google live traffic.")
    p.add_argument("--lat", type=float, help="Latitude of the location")
    p.add_argument("--lon", type=float, help="Longitude of the location")
    p.add_argument("--dest-lat", type=float, help="Other end of the probe segment")
    p.add_argument("--dest-lon", type=float, help="Other end of the probe segment")
    p.add_argument("--bearing", type=float, default=DEFAULT_BEARING_DEG,
                   help="Compass bearing for the auto probe segment "
                        f"(deg, default {DEFAULT_BEARING_DEG} = north)")
    p.add_argument("--seg-km", type=float, default=AUTO_SEGMENT_KM,
                   help=f"Auto probe segment length km (default {AUTO_SEGMENT_KM})")
    p.add_argument("--name", default="", help="Optional label for the location")
    p.add_argument("--base-speed", type=int, default=DEFAULT_BASE_SPEED,
                   help=f"Legal/design max speed km/h (default {DEFAULT_BASE_SPEED})")
    p.add_argument("--optimal", action="store_true",
                   help="Use TRAFFIC_AWARE_OPTIMAL (more accurate, costs more)")
    p.add_argument("--key", default="", help="Google Maps API key (overrides env)")
    p.add_argument("--no-csv", action="store_true", help="Print only; do not save")
    return p.parse_args()


def resolve_key(cli_key):
    key = cli_key or os.environ.get("GOOGLE_MAPS_API_KEY", "") or GOOGLE_MAPS_API_KEY
    if not key:
        sys.exit(
            "No Google Maps API key found. Do one of:\n"
            "  * set an env var:  setx GOOGLE_MAPS_API_KEY \"your-key\"  (reopen terminal)\n"
            "  * pass it inline:  python python/vsl_local.py --key your-key --lat .. --lon ..\n"
            "  * paste it into the GOOGLE_MAPS_API_KEY constant in this file")
    return key


def main():
    a = parse_args()

    lat = a.lat if a.lat is not None else float(input("Latitude  (e.g. 34.364735): ").strip())
    lon = a.lon if a.lon is not None else float(input("Longitude (e.g. 35.782127): ").strip())

    if a.dest_lat is not None and a.dest_lon is not None:
        dest = (a.dest_lat, a.dest_lon)
        seg_note = "explicit --dest point"
    else:
        dest = offset_point(lat, lon, a.seg_km, a.bearing)
        seg_note = f"auto {a.seg_km} km @ {a.bearing:.0f} deg -> {dest[0]:.6f}, {dest[1]:.6f}"

    key = resolve_key(a.key)

    print(f"\nLocation : {lat:.6f}, {lon:.6f}")
    print(f"Segment  : {seg_note}")
    print("Querying Google Routes API + weather ...")

    weather = get_weather_google(lat, lon, key)
    # Free / no-key alternative: see the commented-out "WEATHER: Open-Meteo"
    # block above, then use:  weather = get_weather_open_meteo(lat, lon)

    traffic = get_traffic((lat, lon), dest, key, optimal=a.optimal)

    rec = recommend_speed(weather, traffic["congestion_ratio"], a.base_speed,
                          free_flow_speed=traffic["free_flow_speed_kmh"])

    now_utc = datetime.now(timezone.utc)
    hour_local = datetime.now().hour

    print(f"\n============ WEATHER ({weather['source']}) ============")
    print(f"  Condition        : {weather['condition']} ({weather['detail']})")
    print(f"  Temperature      : {weather['temperature_C']} C")
    print(f"  Wind / gust      : {weather['wind_speed_ms']} / {weather['wind_gust_ms']} m/s")
    print(f"  Visibility       : {weather['visibility_m']:.0f} m")
    print(f"  Humidity         : {weather['humidity_pct']} %")
    print(f"  Precipitation    : {weather['precipitation_mm']} mm")

    print("\n============ TRAFFIC (Google Routes API) ============")
    print(f"  Segment length   : {traffic['distance_m']} m")
    print(f"  Travel time      : {traffic['duration_s']} s "
          f"(free flow {traffic['static_duration_s']} s)")
    print(f"  Current speed    : {traffic['current_speed_kmh']} km/h")
    print(f"  Free-flow speed  : {traffic['free_flow_speed_kmh']} km/h")
    print(f"  Congestion ratio : {traffic['congestion_ratio']}  "
          f"(0 = free flow, 0.25 = 25% slower than normal)")

    print("\n============ RECOMMENDATION ============")
    print(f"  Base speed        : {a.base_speed} km/h")
    print(f"  RECOMMENDED SPEED : {rec} km/h")
    print("=======================================\n")

    if not a.no_csv:
        append_row([
            now_utc.isoformat(timespec="seconds"), a.name, lat, lon,
            round(dest[0], 6), round(dest[1], 6),
            weather["source"], weather["temperature_C"], weather["condition"],
            weather["detail"], weather["wind_speed_ms"], weather["wind_gust_ms"],
            round(weather["visibility_m"]), weather["humidity_pct"],
            weather["precipitation_mm"], traffic["distance_m"],
            traffic["duration_s"], traffic["static_duration_s"],
            traffic["current_speed_kmh"], traffic["free_flow_speed_kmh"],
            traffic["congestion_ratio"], hour_local, a.base_speed, rec,
        ])
        print(f"Row appended to {CSV_PATH}")


if __name__ == "__main__":
    main()
