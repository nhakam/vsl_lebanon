import requests
import csv
import os
from datetime import datetime
from google.colab import drive

# ==== MOUNT DRIVE (so data persists across sessions) ====
drive.mount('/content/drive')

# ==== CONFIG ====
GOOGLE_API_KEY = "xxx"       # Distance Matrix key
OPENWEATHER_API_KEY = "xxx"  # OpenWeatherMap key

CSV_PATH = "/content/drive/MyDrive/VSL_dataset.csv"

# Waypoints along Balamand -> Zgharta (adjust/add more points from Google Maps
# by right-clicking directly on the road as you did before)
WAYPOINTS = [
    ("Balamand",   34.364735, 35.782127),
    ("Midpoint_1", 34.375000, 35.815000),
    ("Midpoint_2", 34.385000, 35.850000),
    ("Zgharta",    34.393990, 35.896412),
]


# ---- Weather ----
def get_weather(lat, lng, api_key):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"lat": lat, "lon": lng, "appid": api_key, "units": "metric"}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    return {
        "temperature_C": data["main"]["temp"],
        "condition": data["weather"][0]["main"],
        "wind_speed_ms": data["wind"]["speed"],
        "visibility_m": data.get("visibility", 10000),
        "humidity_pct": data["main"]["humidity"],
    }


# ---- Congestion between two consecutive waypoints ----
def get_congestion(origin, destination, api_key):
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": f"{origin[0]},{origin[1]}",
        "destinations": f"{destination[0]},{destination[1]}",
        "departure_time": "now",
        "key": api_key
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    element = data["rows"][0]["elements"][0]
    if element["status"] != "OK":
        return None
    typical = element["duration"]["value"]
    live = element["duration_in_traffic"]["value"]
    return round((live / typical) - 1, 3)


# ---- Rule-based starting label (review/adjust manually afterward) ----
def recommend_speed(weather, congestion_ratio):
    base_speed = 80  # km/h, adjust to your road's legal/typical max

    speed = base_speed

    # Weather penalties
    if weather["condition"] in ["Rain", "Drizzle", "Thunderstorm"]:
        speed -= 20
    elif weather["condition"] in ["Fog", "Mist", "Haze"]:
        speed -= 25
    elif weather["condition"] == "Snow":
        speed -= 35

    if weather["visibility_m"] < 5000:
        speed -= 10

    if weather["wind_speed_ms"] > 12:
        speed -= 5

    # Congestion penalty
    if congestion_ratio is not None:
        if congestion_ratio > 0.30:
            speed -= 25
        elif congestion_ratio > 0.15:
            speed -= 15
        elif congestion_ratio > 0.05:
            speed -= 5

    return max(speed, 20)  # never recommend below 20 km/h


# ---- Ensure CSV exists with header ----
def ensure_csv():
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "segment", "lat", "lng",
                "temperature_C", "condition", "wind_speed_ms",
                "visibility_m", "humidity_pct",
                "congestion_ratio", "hour", "recommended_speed"
            ])


# ==== COLLECT ONE ROUND OF SAMPLES ====
def collect_round():
    ensure_csv()
    rows = []
    now = datetime.now()

    for i in range(len(WAYPOINTS) - 1):
        name_a, lat_a, lng_a = WAYPOINTS[i]
        name_b, lat_b, lng_b = WAYPOINTS[i + 1]
        segment_name = f"{name_a}-{name_b}"

        weather = get_weather(lat_a, lng_a, OPENWEATHER_API_KEY)
        congestion = get_congestion((lat_a, lng_a), (lat_b, lng_b), GOOGLE_API_KEY)
        speed_label = recommend_speed(weather, congestion)

        row = [
            now.isoformat(), segment_name, lat_a, lng_a,
            weather["temperature_C"], weather["condition"], weather["wind_speed_ms"],
            weather["visibility_m"], weather["humidity_pct"],
            congestion, now.hour, speed_label
        ]
        rows.append(row)

    with open(CSV_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"Added {len(rows)} rows at {now.isoformat()}")
    return rows


# ==== RUN ====
collect_round()

# Check current total row count
with open(CSV_PATH) as f:
    total = sum(1 for _ in f) - 1  # minus header
print(f"Total samples so far: {total}")
