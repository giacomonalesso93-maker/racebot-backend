"""
weather.py — Previsioni meteo via Open-Meteo (gratuito, no API key)
Usato dal chatbot per rispondere a domande sul meteo della gara.
"""

import urllib.request
import urllib.parse
import json
from datetime import datetime, timedelta


WMO_DESCRIPTIONS = {
    0:  "Cielo sereno ☀️",
    1:  "Prevalentemente sereno 🌤️",
    2:  "Parzialmente nuvoloso ⛅",
    3:  "Nuvoloso ☁️",
    45: "Nebbia 🌫️",
    48: "Nebbia gelata 🌫️",
    51: "Pioviggine leggera 🌦️",
    53: "Pioviggine moderata 🌦️",
    55: "Pioviggine intensa 🌧️",
    61: "Pioggia leggera 🌧️",
    63: "Pioggia moderata 🌧️",
    65: "Pioggia intensa 🌧️",
    71: "Neve leggera ❄️",
    73: "Neve moderata ❄️",
    75: "Neve intensa ❄️",
    77: "Granuli di neve 🌨️",
    80: "Rovesci leggeri 🌦️",
    81: "Rovesci moderati 🌧️",
    82: "Rovesci intensi ⛈️",
    85: "Rovesci di neve 🌨️",
    86: "Rovesci di neve intensi ❄️",
    95: "Temporale ⛈️",
    96: "Temporale con grandine ⛈️",
    99: "Temporale con grandine intensa ⛈️",
}


def _geocode(location: str) -> tuple[float, float] | None:
    """Converte il nome di una località in coordinate lat/lon via Open-Meteo Geocoding."""
    try:
        query = urllib.parse.urlencode({"name": location, "count": 1, "language": "it", "format": "json"})
        url = f"https://geocoding-api.open-meteo.com/v1/search?{query}"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        results = data.get("results")
        if not results:
            return None
        return results[0]["latitude"], results[0]["longitude"]
    except Exception:
        return None


def _parse_date(date_str: str) -> datetime | None:
    """Tenta di estrarre una data da stringhe come '15 giugno 2025', '2025-06-15', '15/06/2025'."""
    date_str = date_str.strip()
    formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass

    # Formato italiano testuale: "15 giugno 2025"
    MESI = {
        "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
        "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
        "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    }
    parts = date_str.lower().split()
    if len(parts) >= 3:
        try:
            day = int(parts[0])
            month = MESI.get(parts[1])
            year = int(parts[2])
            if month:
                return datetime(year, month, day)
        except (ValueError, IndexError):
            pass
    return None


def get_weather_context(location: str | None, date_str: str | None) -> str:
    """
    Ritorna una stringa di contesto meteo da includere nel prompt di Claude.
    Restituisce stringa vuota se non è possibile ottenere le previsioni.
    """
    if not location or not date_str:
        return ""

    race_date = _parse_date(date_str)
    if not race_date:
        return ""

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    days_ahead = (race_date - today).days

    # Open-Meteo fornisce previsioni fino a 16 giorni
    if days_ahead < 0 or days_ahead > 16:
        return ""

    coords = _geocode(location)
    if not coords:
        return ""

    lat, lon = coords
    target_date = race_date.strftime("%Y-%m-%d")

    try:
        params = urllib.parse.urlencode({
            "latitude": lat,
            "longitude": lon,
            "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,windspeed_10m_max",
            "timezone": "Europe/Rome",
            "start_date": target_date,
            "end_date": target_date,
        })
        url = f"https://api.open-meteo.com/v1/forecast?{params}"
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())

        daily = data.get("daily", {})
        if not daily.get("time"):
            return ""

        wmo = daily.get("weathercode", [None])[0]
        t_max = daily.get("temperature_2m_max", [None])[0]
        t_min = daily.get("temperature_2m_min", [None])[0]
        precip = daily.get("precipitation_sum", [None])[0]
        precip_prob = daily.get("precipitation_probability_max", [None])[0]
        wind = daily.get("windspeed_10m_max", [None])[0]

        desc = WMO_DESCRIPTIONS.get(wmo, "Variabile") if wmo is not None else "Variabile"

        lines = [f"Previsioni meteo per il giorno della gara ({target_date}) a {location}:"]
        lines.append(f"- Condizioni: {desc}")
        if t_min is not None and t_max is not None:
            lines.append(f"- Temperatura: min {t_min}°C / max {t_max}°C")
        if precip_prob is not None:
            lines.append(f"- Probabilità pioggia: {precip_prob}%")
        if precip is not None and precip > 0:
            lines.append(f"- Precipitazioni previste: {precip} mm")
        if wind is not None:
            lines.append(f"- Vento massimo: {wind} km/h")

        return "\n".join(lines)

    except Exception:
        return ""
