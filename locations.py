"""
locations.py — Gestione posizioni GPS per ogni gara
Sprint 4: partenza, parcheggi, ristori, ecc.
"""

import uuid
from database import supabase

TIPI_POSIZIONE = {
    "partenza": "🏁",
    "arrivo": "🏆",
    "parcheggio": "🅿️",
    "segreteria": "🗂️",
    "ritiro_sacche": "🎒",
    "deposito_sacche": "📦",
    "hotel": "🏨",
    "expo": "🏪",
    "ristoro": "🍌",
    "punto_medico": "🏥",
    "altro": "📍"
}


def add_location(race_id: str, name: str, tipo: str, lat: float = None, lng: float = None,
                  google_maps_url: str = None, notes: str = None, provisions: str = None) -> dict:
    location = {
        "id": str(uuid.uuid4()),
        "race_id": race_id,
        "name": name,
        "type": tipo,
        "lat": lat,
        "lng": lng,
        "google_maps_url": google_maps_url,
        "notes": notes,
        "provisions": provisions
    }
    result = supabase.table("locations").insert(location).execute()
    return result.data[0] if result.data else location


def get_locations(race_id: str) -> list:
    result = supabase.table("locations").select("*").eq("race_id", race_id).execute()
    return result.data or []


def update_location(location_id: str, name: str, tipo: str, lat=None, lng=None,
                    google_maps_url=None, notes=None, provisions=None):
    supabase.table("locations").update({
        "name": name,
        "type": tipo,
        "lat": lat,
        "lng": lng,
        "google_maps_url": google_maps_url or None,
        "notes": notes or None,
        "provisions": provisions or None,
    }).eq("id", location_id).execute()


def delete_location(location_id: str):
    supabase.table("locations").delete().eq("id", location_id).execute()
