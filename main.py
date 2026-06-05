"""
main.py — Server FastAPI per RaceBot
Sprint 1: motore AI (PDF → embedding → risposta)
Sprint 2: pannello organizzatore (login, gare, upload)
"""

import os
import uuid
import shutil
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv
import csv
import io
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Cookie, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

load_dotenv(override=True)

from database import supabase, create_tables
from auth import register_organizer, login_organizer, get_current_organizer
from embeddings import process_pdf, search
from chat import get_answer, stream_answer
from tickets import create_ticket, notify_organizer, notify_participant, get_tickets_for_organizer, reply_to_ticket
from emails import send_welcome_email, send_approval_email
from locations import add_location, get_locations, delete_location, update_location, TIPI_POSIZIONE
from custom_qa import add_qa, get_qa, delete_qa, get_qa_context
from plans import get_features, plan_label, plan_color, PLAN_LABELS, PLAN_ORDER, PLAN_MAX_RACES, PLAN_FEATURES

create_tables()

app = FastAPI(title="RaceBot API", version="0.3.0")
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─── ADMIN AUTH ───────────────────────────────────────────────
ADMIN_SESSIONS: set[str] = set()  # token attivi in memoria

def _admin_token_valid(token: str | None) -> bool:
    return bool(token and token in ADMIN_SESSIONS)

# Mappa sport_type → emoji
SPORT_EMOJIS = {
    "trail": "🏔️",
    "running": "🏃",
    "cycling": "🚴",
    "mtb": "🚵",
    "triathlon": "🏊",
    "ski": "⛷️",
    "ski_fondo": "🎿",
    "swim": "🏊",
    "kayak": "🚣",
    "trekking": "🥾",
    "obstacle": "💪",
    "altro": "🏅",
}


# ─── GPX PARSER ──────────────────────────────────────────────

def parse_gpx(content: bytes) -> list:
    """Estrae i punti della traccia da un file GPX. Restituisce lista di [lat, lon]."""
    root = ET.fromstring(content)

    namespaces = [
        "http://www.topografix.com/GPX/1/1",
        "http://www.topografix.com/GPX/1/0",
        ""
    ]
    points = []
    for ns in namespaces:
        prefix = f"{{{ns}}}" if ns else ""
        for trkpt in root.iter(f"{prefix}trkpt"):
            lat = trkpt.get("lat")
            lon = trkpt.get("lon")
            if lat and lon:
                points.append([round(float(lat), 6), round(float(lon), 6)])
        if points:
            break

    # Downsampling: max 600 punti per non appesantire il frontend
    if len(points) > 600:
        step = len(points) // 600
        points = points[::step]

    return points


def extract_gpx_waypoints(content: bytes) -> list:
    """Estrae i waypoint da un file GPX. Restituisce lista di dict con name, lat, lon, desc."""
    root = ET.fromstring(content)
    namespaces = [
        "http://www.topografix.com/GPX/1/1",
        "http://www.topografix.com/GPX/1/0",
        ""
    ]
    waypoints = []
    for ns in namespaces:
        prefix = f"{{{ns}}}" if ns else ""
        wpts = list(root.iter(f"{prefix}wpt"))
        if wpts:
            for wpt in wpts:
                lat = wpt.get("lat")
                lon = wpt.get("lon")
                if not lat or not lon:
                    continue
                name_el = wpt.find(f"{prefix}name")
                desc_el = wpt.find(f"{prefix}desc")
                name = name_el.text.strip() if name_el is not None and name_el.text else "Waypoint"
                desc = desc_el.text.strip() if desc_el is not None and desc_el.text else None
                waypoints.append({
                    "name": name,
                    "lat": round(float(lat), 6),
                    "lon": round(float(lon), 6),
                    "desc": desc
                })
            break
    return waypoints


def detect_location_type(name: str) -> str:
    """Rileva il tipo di posizione dal nome del waypoint."""
    n = name.lower()
    if any(k in n for k in ["ristoro", "aid", "rifornimento", "acqua", "food", "drink", "km "]):
        return "ristoro"
    if any(k in n for k in ["partenza", "start", "via", "inizio"]):
        return "partenza"
    if any(k in n for k in ["arrivo", "finish", "traguardo", "meta"]):
        return "arrivo"
    if any(k in n for k in ["parcheggio", "parking", "park", "auto"]):
        return "parcheggio"
    if any(k in n for k in ["medico", "medical", "soccorso", "sanit", "pronto"]):
        return "punto_medico"
    return "altro"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

Path("./templates").mkdir(exist_ok=True)
templates = Jinja2Templates(directory="templates")


# ─── HEALTH CHECK ────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


# ─── AUTH ────────────────────────────────────────────────────

@app.post("/api/register")
def api_register(request: Request, email: str = Form(...), password: str = Form(...), name: str = Form(...)):
    try:
        existing = supabase.table("organizers").select("id").eq("email", email).execute()
        if existing.data:
            return templates.TemplateResponse(request=request, name="register.html", context={"error": "Email già registrata. Prova ad accedere."})
        organizer = register_organizer(email, password, name)
        if not organizer:
            return templates.TemplateResponse(request=request, name="register.html", context={"error": "Errore durante la registrazione. Riprova."})
        send_welcome_email(email, name)
        return templates.TemplateResponse(request=request, name="register.html", context={"success": True})
    except Exception as e:
        return templates.TemplateResponse(request=request, name="register.html", context={"error": "Errore durante la registrazione. Riprova."})


@app.post("/api/login")
def api_login(request: Request, email: str = Form(...), password: str = Form(...)):
    token, error = login_organizer(email, password)
    if error == "wrong_credentials":
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Email o password non corretti."})
    if error == "pending":
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Il tuo account è in attesa di approvazione. Riceverai una email quando sarà attivato.", "pending": True})
    if error == "suspended":
        return templates.TemplateResponse(request=request, name="login.html", context={"error": "Il tuo account è stato sospeso. Contatta il supporto."})
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie(key="session", value=token, httponly=True)
    return response


@app.get("/api/logout")
def api_logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    return response


# ─── EVENTI ──────────────────────────────────────────────────

@app.post("/api/events")
def create_event(
    name: str = Form(...),
    date: str = Form(""),
    location: str = Form(""),
    sport_type: str = Form(""),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("events").insert({
        "id": str(uuid.uuid4()),
        "organizer_id": organizer["id"],
        "name": name,
        "date": date or None,
        "location": location or None,
        "sport_type": sport_type or None,
    }).execute()
    return RedirectResponse(url="/dashboard?ok=Evento+creato", status_code=303)


@app.post("/api/races/{race_id}/edit")
def edit_race(
    race_id: str,
    name: str = Form(...),
    date: str = Form(""),
    location: str = Form(""),
    elevation_gain: str = Form(""),
    length_km: str = Form(""),
    start_time: str = Form(""),
    secretary_email: str = Form(""),
    sport_type: str = Form(""),
    notes: str = Form(""),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("races").update({
        "name": name,
        "date": date or None,
        "location": location or None,
        "elevation_gain": elevation_gain or None,
        "length_km": length_km or None,
        "start_time": start_time or None,
        "secretary_email": secretary_email or None,
        "sport_type": sport_type or None,
        "notes": notes or None,
    }).eq("id", race_id).execute()
    race_data = supabase.table("races").select("event_id").eq("id", race_id).execute().data
    event_id = race_data[0].get("event_id") if race_data else None
    redirect = f"/dashboard/events/{event_id}" if event_id else "/dashboard?ok=Gara+aggiornata"
    return RedirectResponse(url=redirect, status_code=303)


@app.post("/api/events/{event_id}/edit")
def edit_event(
    event_id: str,
    name: str = Form(...),
    date: str = Form(""),
    location: str = Form(""),
    sport_type: str = Form(""),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("events").update({
        "name": name,
        "date": date or None,
        "location": location or None,
        "sport_type": sport_type or None,
    }).eq("id", event_id).execute()
    return RedirectResponse(url=f"/dashboard/events/{event_id}?ok=Evento+aggiornato", status_code=303)


@app.post("/api/events/{event_id}/general-info")
async def save_event_general_info(
    event_id: str,
    general_info: str = Form(""),
    secretary_location: str = Form(""),
    secretary_email: str = Form(""),
    event_logo: UploadFile = File(None),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    update_data = {
        "general_info": general_info or None,
        "secretary_location": secretary_location or None,
        "secretary_email": secretary_email or None,
    }
    if event_logo and event_logo.filename:
        ext = event_logo.filename.rsplit(".", 1)[-1].lower()
        if ext in ["png", "jpg", "jpeg", "svg", "webp"]:
            logo_filename = f"event_logo_{event_id}.{ext}"
            logo_path = os.path.join("static", logo_filename)
            content = await event_logo.read()
            with open(logo_path, "wb") as f:
                f.write(content)
            update_data["chatbot_logo_url"] = f"/static/{logo_filename}"
    supabase.table("events").update(update_data).eq("id", event_id).execute()
    return RedirectResponse(url=f"/dashboard/events/{event_id}?ok=Info+generali+salvate", status_code=303)


@app.post("/api/events/{event_id}/locations")
async def add_event_location(
    event_id: str,
    name: str = Form(...),
    type: str = Form("altro"),
    notes: str = Form(""),
    google_maps_url: str = Form(""),
    lat: str = Form(""),
    lng: str = Form(""),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("locations").insert({
        "id": str(uuid.uuid4()),
        "event_id": event_id,
        "race_id": None,
        "name": name,
        "type": type,
        "notes": notes or None,
        "google_maps_url": google_maps_url or None,
        "lat": float(lat) if lat else None,
        "lng": float(lng) if lng else None,
    }).execute()
    return RedirectResponse(url=f"/dashboard/events/{event_id}?ok=Posizione+aggiunta", status_code=303)


@app.post("/api/races/{race_id}/delete")
def delete_race(race_id: str, event_id: str = Form(""), session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("locations").delete().eq("race_id", race_id).execute()
    supabase.table("custom_qa").delete().eq("race_id", race_id).execute()
    supabase.table("races").delete().eq("id", race_id).execute()
    redirect = f"/dashboard/events/{event_id}" if event_id else "/dashboard"
    return RedirectResponse(url=redirect, status_code=303)


@app.post("/api/events/{event_id}/delete")
def delete_event(event_id: str, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("races").update({"event_id": None}).eq("event_id", event_id).execute()
    supabase.table("events").delete().eq("id", event_id).execute()
    return RedirectResponse(url="/dashboard", status_code=303)


# ─── GARE ────────────────────────────────────────────────────

@app.post("/api/races")
def create_race(
    name: str = Form(...),
    date: str = Form(""),
    location: str = Form(""),
    secretary_email: str = Form(""),
    elevation_gain: str = Form(""),
    length_km: str = Form(""),
    start_time: str = Form(""),
    notes: str = Form(""),
    event_id: str = Form(""),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")

    # Controlla limite gare per piano
    plan = organizer.get("plan") or "single"
    max_races = PLAN_MAX_RACES.get(plan)

    # Piano Single: le gare devono essere sempre dentro un evento
    if plan == "single" and not event_id:
        return RedirectResponse(
            url="/dashboard?error=Con+il+piano+Gara+Singola+devi+creare+prima+un+evento+e+aggiungere+la+gara+al+suo+interno.",
            status_code=303
        )

    if max_races is not None:
        current_count = len(supabase.table("races").select("id").eq("organizer_id", organizer["id"]).execute().data or [])
        if current_count >= max_races:
            return RedirectResponse(
                url=f"/dashboard?error=Hai+raggiunto+il+limite+di+{max_races}+gara+per+il+piano+{plan_label(plan)}.+Passa+a+un+piano+superiore.",
                status_code=303
            )

    supabase.table("races").insert({
        "id": str(uuid.uuid4()),
        "organizer_id": organizer["id"],
        "name": name,
        "date": date or None,
        "location": location or None,
        "secretary_email": secretary_email or None,
        "elevation_gain": elevation_gain or None,
        "length_km": length_km or None,
        "start_time": start_time or None,
        "notes": notes or None,
        "pdf_uploaded": False,
        "event_id": event_id if event_id else None
    }).execute()

    redirect = f"/dashboard/events/{event_id}" if event_id else "/dashboard?ok=Gara+creata"
    return RedirectResponse(url=redirect, status_code=303)


# ─── REGOLAMENTO TESTUALE ────────────────────────────────────

@app.post("/api/races/{race_id}/text-regulation")
def save_text_regulation(
    race_id: str,
    text_regulation: str = Form(...),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("races").update({"text_regulation": text_regulation or None}).eq("id", race_id).execute()
    race_data = supabase.table("races").select("event_id").eq("id", race_id).execute().data
    event_id = race_data[0].get("event_id") if race_data else None
    redirect = f"/dashboard/events/{event_id}" if event_id else "/dashboard"
    return RedirectResponse(url=f"{redirect}?ok=Regolamento+salvato", status_code=303)


@app.post("/api/races/{race_id}/clear-regulation")
def clear_regulation(race_id: str, mode: str = Form("text"), session: str = Cookie(default=None)):
    """Cancella PDF o regolamento testuale da una gara."""
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    if mode == "text":
        supabase.table("races").update({"text_regulation": None}).eq("id", race_id).execute()
    else:
        supabase.table("races").update({"pdf_uploaded": False}).eq("id", race_id).execute()
        supabase.table("embeddings").delete().eq("race_id", race_id).execute()
    race_data = supabase.table("races").select("event_id").eq("id", race_id).execute().data
    event_id = race_data[0].get("event_id") if race_data else None
    redirect = f"/dashboard/events/{event_id}" if event_id else "/dashboard"
    return RedirectResponse(url=f"{redirect}?ok=Regolamento+rimosso", status_code=303)


# ─── UPLOAD PDF ──────────────────────────────────────────────

@app.post("/api/races/{race_id}/upload")
async def upload_pdf(race_id: str, file: UploadFile = File(...), session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")

    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo file PDF accettati")

    pdf_path = UPLOAD_DIR / f"{race_id}.pdf"
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    process_pdf(str(pdf_path), race_id)

    supabase.table("races").update({"pdf_uploaded": True}).eq("id", race_id).execute()

    race_data = supabase.table("races").select("event_id").eq("id", race_id).execute().data
    event_id = race_data[0].get("event_id") if race_data else None
    redirect = f"/dashboard/events/{event_id}" if event_id else "/dashboard"
    return RedirectResponse(url=redirect, status_code=303)


# ─── UPLOAD GPX ──────────────────────────────────────────────

@app.post("/api/races/{race_id}/upload-gpx")
async def upload_gpx(race_id: str, file: UploadFile = File(...), session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")

    if not file.filename.lower().endswith(".gpx"):
        raise HTTPException(status_code=400, detail="Solo file GPX accettati")

    content = await file.read()
    try:
        track = parse_gpx(content)
        waypoints = extract_gpx_waypoints(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Errore nel parsing del file GPX: {str(e)}")

    if not track:
        raise HTTPException(status_code=400, detail="Nessun punto trovato nel file GPX")

    supabase.table("races").update({"gpx_data": track}).eq("id", race_id).execute()

    # Importa i waypoint come posizioni (se presenti)
    for wpt in waypoints:
        tipo = detect_location_type(wpt["name"])
        add_location(
            race_id=race_id,
            name=wpt["name"],
            tipo=tipo,
            lat=wpt["lat"],
            lng=wpt["lon"],
            notes=wpt["desc"] or None
        )

    imported = len(waypoints)
    redirect_url = f"/dashboard/races/{race_id}/locations"
    if imported > 0:
        redirect_url += f"?imported={imported}"
    return RedirectResponse(url=redirect_url, status_code=303)


# ─── CHATBOT API ─────────────────────────────────────────────

@app.post("/api/ask/{race_id}")
async def ask_question(
    request: Request,
    race_id: str,
    question: str = Form(...),
    history: str = Form(default="[]"),
    participant_email: str = Form(default=None)
):
    result = supabase.table("races").select("*").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Gara non trovata")

    race = result.data[0]
    chunks = search(question, race_id)

    if not chunks and race.get("text_regulation"):
        chunks = [race["text_regulation"]]
    elif not chunks:
        chunks = []

    locs = get_locations(race_id)

    # Recupera anche posizioni a livello evento (segreteria, hotel, info generali)
    event_id = race.get("event_id")
    event_locs = []
    event_general_info = ""
    if event_id:
        event_data = supabase.table("events").select("general_info,secretary_location,secretary_email,name").eq("id", event_id).execute().data
        if event_data:
            ev = event_data[0]
            if ev.get("general_info"):
                event_general_info = f"Informazioni generali dell'evento {ev['name']}:\n{ev['general_info']}"
            if ev.get("secretary_location"):
                event_general_info += f"\nSegreteria/Ritiro pettorali: {ev['secretary_location']}"
            if ev.get("secretary_email"):
                event_general_info += f"\nEmail organizzazione: {ev['secretary_email']}"
        event_locs_result = supabase.table("locations").select("*").eq("event_id", event_id).execute()
        event_locs = event_locs_result.data or []

    def build_location_context(locations, label):
        if not locations:
            return ""
        lines = [f"{label}:"]
        for loc in locations:
            line = f"- {loc['name']} ({loc['type']})"
            if loc.get("notes"):
                line += f": {loc['notes']}"
            if loc.get("provisions"):
                line += f" — Dotazione: {loc['provisions']}"
            url = loc.get("google_maps_url")
            if not url and loc.get("lat") and loc.get("lng"):
                url = f"https://www.google.com/maps?q={loc['lat']},{loc['lng']}"
            if url:
                line += f"\n  → LINK MAPPA: {url}"
            lines.append(line)
        return "\n".join(lines)

    location_context = build_location_context(locs, "Posizioni specifiche della gara")
    if event_locs:
        event_loc_context = build_location_context(event_locs, "Posizioni generali dell'evento (segreteria, hotel, parcheggi comuni)")
        location_context = event_loc_context + ("\n\n" + location_context if location_context else "")
    if event_general_info:
        location_context = event_general_info + ("\n\n" + location_context if location_context else "")

    qa_context = get_qa_context(race_id)
    # Aggiunge link download GPX se disponibile
    gpx_download_url = ""
    if race.get("gpx_data"):
        base_url = str(request.base_url).rstrip("/").replace("http://", "https://")
        gpx_download_url = f"{base_url}/p/{race_id}/download-gpx"

    race_info = {
        "date": race.get("date"),
        "location": race.get("location"),
        "elevation_gain": race.get("elevation_gain"),
        "length_km": race.get("length_km"),
        "start_time": race.get("start_time"),
        "secretary_email": race.get("secretary_email"),
        "notes": race.get("notes"),
        "gpx_download_url": gpx_download_url,
    }

    # Parsing storico conversazione
    try:
        parsed_history = json.loads(history) if history else []
    except Exception:
        parsed_history = []

    frasi_non_so = [
        "non ho questa informazione", "contatta la segreteria", "non è presente nel regolamento",
        "non trovo informazioni", "non ho trovato", "contattare la segreteria",
        "contattare direttamente", "ti consiglio di contattare",
        "non sono presenti informazioni", "non ho informazioni",
        "i don't have this information", "contact the secretariat",
        "je n'ai pas cette information", "no tengo esta información",
    ]

    # Prepara dizionario posizioni con link per iniezione automatica
    locs_with_links = {}
    for loc in (locs + event_locs):
        url = loc.get("google_maps_url")
        if not url and loc.get("lat") and loc.get("lng"):
            url = f"https://www.google.com/maps?q={loc['lat']},{loc['lng']}"
        if url:
            locs_with_links[loc["name"].lower()] = {
                "name": loc["name"],
                "url": url,
                "type": loc.get("type", "")
            }

    def generate():
        full_answer = []
        try:
            for chunk in stream_answer(
                question, chunks, race["name"],
                location_context, qa_context, race_info, parsed_history
            ):
                if chunk.startswith("data: [DONE]"):
                    # Raccogli la risposta completa per logging e ticket
                    answer = "".join(full_answer).replace("\\n", "\n")

                    # Inietta link mappa solo per i tipi di posizione rilevanti alla domanda
                    type_keywords = {
                        "parcheggio": ["parcheggio", "parking", "parcheggi", "park"],
                        "partenza": ["partenza", "start", "dove si parte", "dove parto"],
                        "arrivo": ["arrivo", "finish", "traguardo", "dove si arriva"],
                        "ristoro": ["ristoro", "ristori", "acqua", "rifornimento", "mangiare", "bere"],
                        "punto_medico": ["medico", "pronto soccorso", "ambulanza", "emergenza"],
                        "altro": [],
                    }
                    question_lower = question.lower()
                    relevant_types = set()
                    for loc_type, keywords in type_keywords.items():
                        if any(kw in question_lower for kw in keywords):
                            relevant_types.add(loc_type)

                    # Se non ha trovato tipi specifici ma è una domanda generica su posizioni
                    generic_keywords = ["dove", "mappa", "maps", "indirizzo", "posizione", "naviga", "location"]
                    if not relevant_types and any(kw in question_lower for kw in generic_keywords):
                        relevant_types = set(type_keywords.keys())

                    if locs_with_links and relevant_types:
                        matching = [loc for loc in locs_with_links.values() if loc["type"] in relevant_types]
                        if matching:
                            links_text = "\\n\\n🗺️ **Link mappe:**"
                            for loc_data in matching:
                                links_text += f"\\n📍 {loc_data['name']}: {loc_data['url']}"
                            yield f"data: {links_text}\n\n"
                    answered = not any(f in answer.lower() for f in frasi_non_so)

                    # Log domanda
                    supabase.table("questions_log").insert({
                        "id": str(uuid.uuid4()),
                        "race_id": race_id,
                        "race_name": race["name"],
                        "question": question,
                        "answered": answered
                    }).execute()

                    # Ticket se non risposto
                    ticket_id = None
                    if not answered:
                        ticket = create_ticket(race_id, race["name"], question, participant_email)
                        ticket_id = ticket["id"]
                        org = supabase.table("organizers").select("*").eq("id", race["organizer_id"]).execute().data
                        if org:
                            notify_organizer(org[0]["email"], org[0]["name"], ticket)

                    meta = json.dumps({"ticket_creato": not answered, "ticket_id": ticket_id})
                    yield f"data: [DONE]\n\n"
                    yield f"data: [META]{meta}\n\n"
                else:
                    # Accumula testo per logging
                    if chunk.startswith("data: "):
                        full_answer.append(chunk[6:].rstrip("\n"))
                    yield chunk
        except Exception as e:
            yield f"data: [ERROR]{str(e)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ─── TICKET ──────────────────────────────────────────────────

@app.post("/api/tickets/{ticket_id}/reply")
def reply_ticket(
    ticket_id: str,
    reply: str = Form(...),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")

    ticket = reply_to_ticket(ticket_id, reply)

    # Se il partecipante ha lasciato l'email → notificalo
    if ticket.get("participant_email"):
        notify_participant(ticket["participant_email"], ticket["race_name"], ticket["question"], reply)

    return RedirectResponse(url="/dashboard/tickets", status_code=303)


# ─── PAGINE HTML ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root():
    return RedirectResponse(url="/login")


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    return templates.TemplateResponse(request=request, name="register.html")


@app.get("/dashboard", response_class=HTMLResponse)
def page_dashboard(request: Request, ok: str = "", session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        return RedirectResponse(url="/login")
    all_races = supabase.table("races").select("*").eq("organizer_id", organizer["id"]).execute().data or []
    events_raw = supabase.table("events").select("*").eq("organizer_id", organizer["id"]).execute().data or []

    # Associa le gare agli eventi
    races_by_event = {}
    standalone_races = []
    for race in all_races:
        eid = race.get("event_id")
        if eid:
            races_by_event.setdefault(eid, []).append(race)
        else:
            standalone_races.append(race)

    events = []
    for ev in events_raw:
        ev["races"] = races_by_event.get(ev["id"], [])
        events.append(ev)

    # Stats per la barra riassuntiva
    race_ids = [r["id"] for r in all_races]
    open_tickets = 0
    total_questions = 0
    if race_ids:
        tickets_data = supabase.table("tickets").select("id, status").in_("race_id", race_ids).execute().data or []
        open_tickets = sum(1 for t in tickets_data if t.get("status") == "pending")
        total_questions = len(tickets_data)

    plan = organizer.get("plan") or "single"
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "organizer": organizer,
        "races": standalone_races,
        "events": events,
        "flash_ok": ok,
        "features": get_features(organizer),
        "plan_label": plan_label(plan),
        "plan_color": plan_color(plan),
        "plan_max_races": PLAN_MAX_RACES.get(plan),
        "total_races_count": len(all_races),
        "stats": {
            "total_races": len(all_races),
            "total_events": len(events_raw),
            "open_tickets": open_tickets,
            "total_questions": total_questions,
        }
    })


@app.post("/api/tickets/{ticket_id}/email")
def save_ticket_email(ticket_id: str, email: str = Form(...)):
    supabase.table("tickets").update({"participant_email": email}).eq("id", ticket_id).execute()
    return {"status": "ok"}


@app.post("/api/races/{race_id}/locations")
def create_location(
    race_id: str,
    name: str = Form(...),
    tipo: str = Form(...),
    lat: str = Form(default=""),
    lng: str = Form(default=""),
    google_maps_url: str = Form(default=""),
    notes: str = Form(default=""),
    provisions: str = Form(default=""),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    add_location(
        race_id=race_id,
        name=name,
        tipo=tipo,
        lat=float(lat) if lat else None,
        lng=float(lng) if lng else None,
        google_maps_url=google_maps_url or None,
        notes=notes or None,
        provisions=provisions or None
    )
    return RedirectResponse(url=f"/dashboard/races/{race_id}/locations", status_code=303)


@app.post("/api/locations/{location_id}/edit")
def edit_location(
    location_id: str,
    race_id: str = Form(...),
    name: str = Form(...),
    tipo: str = Form(...),
    lat: str = Form(default=""),
    lng: str = Form(default=""),
    google_maps_url: str = Form(default=""),
    notes: str = Form(default=""),
    provisions: str = Form(default=""),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    update_location(
        location_id=location_id,
        name=name,
        tipo=tipo,
        lat=float(lat) if lat else None,
        lng=float(lng) if lng else None,
        google_maps_url=google_maps_url or None,
        notes=notes or None,
        provisions=provisions or None,
    )
    return RedirectResponse(url=f"/dashboard/races/{race_id}/locations", status_code=303)


@app.post("/api/locations/{location_id}/delete")
def remove_location(location_id: str, race_id: str = Form(""), session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    delete_location(location_id)
    redirect = f"/dashboard/races/{race_id}/locations" if race_id else "/dashboard"
    return RedirectResponse(url=redirect, status_code=303)


@app.post("/api/races/{race_id}/qa")
def create_qa(
    race_id: str,
    question: str = Form(...),
    answer: str = Form(...),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    add_qa(race_id, question, answer)
    return RedirectResponse(url=f"/dashboard/races/{race_id}/qa", status_code=303)


@app.post("/api/qa/{qa_id}/delete")
def remove_qa(qa_id: str, race_id: str = Form(...), session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    delete_qa(qa_id)
    return RedirectResponse(url=f"/dashboard/races/{race_id}/qa", status_code=303)


@app.get("/dashboard/races/{race_id}/qa", response_class=HTMLResponse)
def page_qa(request: Request, race_id: str, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        return RedirectResponse(url="/login")
    result = supabase.table("races").select("*").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404)
    race = result.data[0]
    items = get_qa(race_id)
    return templates.TemplateResponse(request=request, name="qa.html", context={
        "organizer": organizer,
        "race": race,
        "items": items
    })


@app.post("/api/races/{race_id}/analyze")
async def analyze_race_questions(
    race_id: str,
    session: str = Cookie(default=None)
):
    """Analizza le domande degli ultimi 30 giorni e genera insights per l'organizzatore."""
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")

    # Recupera domande ultimi 30 giorni
    from datetime import datetime, timedelta
    since = (datetime.utcnow() - timedelta(days=30)).isoformat()
    questions = supabase.table("questions_log").select("*").eq("race_id", race_id).gte("created_at", since).execute().data or []

    if not questions:
        return {"insights": [], "message": "Nessuna domanda negli ultimi 30 giorni"}

    # Separa risposte automatiche da ticket
    answered = [q["question"] for q in questions if q.get("answered")]
    unanswered = [q["question"] for q in questions if not q.get("answered")]

    # Conta frequenze domande senza risposta
    from collections import Counter
    unanswered_counts = Counter(unanswered)
    answered_counts = Counter(answered)

    # Top lacune (domande senza risposta, minimo 2 occorrenze)
    top_gaps = [(q, c) for q, c in unanswered_counts.most_common(10) if c >= 1]
    # Top domande frequenti (risposte ma molto chieste, minimo 5 occorrenze)
    top_frequent = [(q, c) for q, c in answered_counts.most_common(10) if c >= 3]

    if not top_gaps and not top_frequent:
        return {"insights": [], "message": "Dati insufficienti per generare suggerimenti"}

    # Usa Claude per generare suggerimenti intelligenti
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    gaps_text = "\n".join([f"- '{q}' ({c} volte)" for q, c in top_gaps]) if top_gaps else "Nessuna"
    frequent_text = "\n".join([f"- '{q}' ({c} volte)" for q, c in top_frequent]) if top_frequent else "Nessuna"

    prompt = f"""Sei un consulente esperto di comunicazione per eventi sportivi.
Analizza queste domande ricevute da un chatbot per una gara sportiva e genera suggerimenti concreti per l'organizzatore.

DOMANDE SENZA RISPOSTA (lacune nel regolamento):
{gaps_text}

DOMANDE FREQUENTI (risposta c'è ma i partecipanti chiedono comunque):
{frequent_text}

Per ogni gruppo, genera massimo 3 suggerimenti concreti e brevi in italiano.
Formato JSON:
{{
  "gaps": [
    {{"question": "domanda originale", "suggestion": "Aggiungi una sezione X al regolamento che spieghi Y", "priority": "alta/media/bassa"}}
  ],
  "frequent": [
    {{"question": "domanda originale", "suggestion": "Metti in evidenza l'informazione su X nella sezione Y del regolamento", "priority": "alta/media/bassa"}}
  ]
}}
Rispondi SOLO con il JSON, nessun testo aggiuntivo."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        analysis = json.loads(response.content[0].text)
    except Exception:
        # Fallback: genera insights semplici senza Claude
        analysis = {
            "gaps": [{"question": q, "suggestion": f"Aggiungi informazioni su questo argomento nel regolamento", "priority": "alta" if c >= 3 else "media"} for q, c in top_gaps[:3]],
            "frequent": [{"question": q, "suggestion": f"Questa domanda viene chiesta spesso — mettila in evidenza o nelle FAQ", "priority": "media"} for q, c in top_frequent[:3]]
        }

    # Salva insights nel DB (prima elimina quelli vecchi pending)
    supabase.table("race_insights").delete().eq("race_id", race_id).eq("status", "pending").execute()

    insights_to_save = []
    for item in analysis.get("gaps", []):
        count = unanswered_counts.get(item["question"], 1)
        insights_to_save.append({
            "id": str(uuid.uuid4()),
            "race_id": race_id,
            "type": "gap",
            "question_example": item["question"],
            "count": count,
            "suggestion": item["suggestion"],
            "status": "pending"
        })
    for item in analysis.get("frequent", []):
        count = answered_counts.get(item["question"], 1)
        insights_to_save.append({
            "id": str(uuid.uuid4()),
            "race_id": race_id,
            "type": "frequent",
            "question_example": item["question"],
            "count": count,
            "suggestion": item["suggestion"],
            "status": "pending"
        })

    if insights_to_save:
        supabase.table("race_insights").insert(insights_to_save).execute()

    return {"insights": insights_to_save, "analyzed": len(questions)}


@app.get("/api/races/{race_id}/insights")
def get_race_insights(race_id: str, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    insights = supabase.table("race_insights").select("*").eq("race_id", race_id).eq("status", "pending").order("count", desc=True).execute().data or []
    return insights


@app.post("/api/insights/{insight_id}/done")
def mark_insight_done(insight_id: str, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("race_insights").update({"status": "done"}).eq("id", insight_id).execute()
    return {"ok": True}


@app.post("/api/insights/{insight_id}/dismiss")
def dismiss_insight(insight_id: str, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")
    supabase.table("race_insights").update({"status": "dismissed"}).eq("id", insight_id).execute()
    return {"ok": True}


@app.get("/api/races/{race_id}/info")
def api_race_info(race_id: str):
    result = supabase.table("races").select("name,date,start_time,location,length_km,elevation_gain,notes").eq("id", race_id).execute()
    if not result.data:
        return {}
    return result.data[0]


@app.get("/api/races/{race_id}/locations")
def api_get_locations(race_id: str):
    return get_locations(race_id)


@app.get("/api/races/{race_id}/gpx-track")
def api_gpx_track(race_id: str):
    result = supabase.table("races").select("gpx_data").eq("id", race_id).execute()
    if not result.data:
        return []
    return result.data[0].get("gpx_data") or []


@app.get("/p/{race_id}/download-gpx")
def download_gpx(race_id: str):
    """Endpoint pubblico per scaricare il file GPX della gara."""
    result = supabase.table("races").select("name, gpx_data").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Gara non trovata")
    race = result.data[0]
    gpx_data = race.get("gpx_data")
    if not gpx_data:
        raise HTTPException(status_code=404, detail="Tracciato GPX non disponibile")

    # Ricostruisce il file GPX dai punti salvati
    race_name = race.get("name", "gara").replace(" ", "_")
    gpx_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="Repliq" xmlns="http://www.topografix.com/GPX/1/1">
  <trk>
    <name>{race.get("name", "Percorso")}</name>
    <trkseg>
"""
    for point in gpx_data:
        if len(point) >= 2:
            lat, lon = point[0], point[1]
            ele = point[2] if len(point) > 2 else ""
            ele_tag = f"      <ele>{ele}</ele>\n" if ele else ""
            gpx_content += f"    <trkpt lat=\"{lat}\" lon=\"{lon}\">\n{ele_tag}    </trkpt>\n"

    gpx_content += "    </trkseg>\n  </trk>\n</gpx>"

    from fastapi.responses import Response
    return Response(
        content=gpx_content,
        media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="{race_name}.gpx"'}
    )


@app.post("/api/races/{race_id}/chatbot-settings")
async def save_chatbot_settings(
    race_id: str,
    chatbot_name: str = Form(""),
    chatbot_color: str = Form("#2563eb"),
    welcome_message: str = Form(""),
    chatbot_logo: UploadFile = File(None),
    session: str = Cookie(default=None)
):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401, detail="Non autenticato")

    update_data = {
        "chatbot_name": chatbot_name or None,
        "chatbot_color": chatbot_color or "#2563eb",
        "welcome_message": welcome_message or None,
    }

    # Upload logo se fornito
    if chatbot_logo and chatbot_logo.filename:
        ext = chatbot_logo.filename.rsplit(".", 1)[-1].lower()
        if ext in ["png", "jpg", "jpeg", "svg", "webp"]:
            logo_filename = f"logo_{race_id}.{ext}"
            logo_path = os.path.join("static", logo_filename)
            content = await chatbot_logo.read()
            with open(logo_path, "wb") as f:
                f.write(content)
            update_data["chatbot_logo_url"] = f"/static/{logo_filename}"

    supabase.table("races").update(update_data).eq("id", race_id).execute()
    return RedirectResponse(url=f"/dashboard?ok=Chatbot+personalizzato", status_code=303)


@app.get("/dashboard/races/{race_id}/locations", response_class=HTMLResponse)
def page_locations(request: Request, race_id: str, imported: int = 0, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        return RedirectResponse(url="/login")
    result = supabase.table("races").select("*").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404)
    race = result.data[0]
    locs = get_locations(race_id)
    gpx_track = race.get("gpx_data") or []
    gpx_points = len(gpx_track)

    gpx_bbox = None
    if gpx_track:
        lats = [p[0] for p in gpx_track if p and len(p) >= 2]
        lons = [p[1] for p in gpx_track if p and len(p) >= 2]
        if lats and lons:
            gpx_bbox = [[min(lats), min(lons)], [max(lats), max(lons)]]

    return templates.TemplateResponse(request=request, name="locations.html", context={
        "organizer": organizer,
        "race": race,
        "locations": locs,
        "tipi": TIPI_POSIZIONE,
        "gpx_points": gpx_points,
        "gpx_bbox": gpx_bbox,
        "imported_waypoints": imported
    })


@app.get("/dashboard/tickets", response_class=HTMLResponse)
def page_tickets(request: Request, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        return RedirectResponse(url="/login")
    tickets = get_tickets_for_organizer(organizer["id"])
    return templates.TemplateResponse(request=request, name="tickets.html", context={
        "organizer": organizer,
        "tickets": tickets
    })


@app.get("/dashboard/export/tickets")
def export_tickets_csv(session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401)
    races = supabase.table("races").select("id, name").eq("organizer_id", organizer["id"]).execute().data or []
    race_ids = [r["id"] for r in races]
    race_names = {r["id"]: r["name"] for r in races}
    tickets = []
    if race_ids:
        tickets = supabase.table("tickets").select("*").in_("race_id", race_ids).order("created_at", desc=True).execute().data or []

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Gara", "Domanda", "Stato", "Email partecipante", "Risposta", "Data"])
    for t in tickets:
        writer.writerow([
            race_names.get(t.get("race_id"), ""),
            t.get("question", ""),
            t.get("status", ""),
            t.get("participant_email", ""),
            t.get("reply", ""),
            t.get("created_at", "")[:10] if t.get("created_at") else "",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=tickets.csv"}
    )


@app.get("/dashboard/export/questions")
def export_questions_csv(session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        raise HTTPException(status_code=401)
    races = supabase.table("races").select("id, name").eq("organizer_id", organizer["id"]).execute().data or []
    race_ids = [r["id"] for r in races]
    questions = []
    if race_ids:
        questions = supabase.table("questions_log").select("*").in_("race_id", race_ids).order("created_at", desc=True).execute().data or []

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Gara", "Domanda", "Risposta automatica", "Data"])
    for q in questions:
        writer.writerow([
            q.get("race_name", ""),
            q.get("question", ""),
            "Sì" if q.get("answered") else "No",
            q.get("created_at", "")[:10] if q.get("created_at") else "",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=domande.csv"}
    )


@app.get("/dashboard/stats", response_class=HTMLResponse)
def page_stats(request: Request, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        return RedirectResponse(url="/login")

    races = supabase.table("races").select("*").eq("organizer_id", organizer["id"]).execute().data or []
    race_ids = [r["id"] for r in races]

    questions = []
    tickets = []
    if race_ids:
        questions = supabase.table("questions_log").select("*").in_("race_id", race_ids).order("created_at", desc=True).execute().data or []
        tickets = supabase.table("tickets").select("*").in_("race_id", race_ids).execute().data or []

    # Domande più frequenti
    from collections import Counter
    from datetime import date, timedelta
    question_counts = Counter(q["question"].lower().strip() for q in questions)
    top_questions = question_counts.most_common(8)

    # Stats per gara
    race_stats = []
    for race in races:
        rq = [q for q in questions if q["race_id"] == race["id"]]
        rt = [t for t in tickets if t["race_id"] == race["id"]]
        race_stats.append({
            "name": race["name"],
            "total": len(rq),
            "answered": sum(1 for q in rq if q.get("answered")),
            "tickets": len(rt),
            "tickets_open": sum(1 for t in rt if t.get("status") == "pending")
        })
    race_stats.sort(key=lambda x: x["total"], reverse=True)

    # Domande per giorno (ultimi 30 giorni)
    today = date.today()
    days_30 = [(today - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    days_7 = days_30[-7:]
    questions_by_day_30 = {d: 0 for d in days_30}
    for q in questions:
        if q.get("created_at"):
            day = q["created_at"][:10]
            if day in questions_by_day_30:
                questions_by_day_30[day] += 1

    # Domande senza risposta automatica (ticket recenti)
    unanswered = sorted(
        [t for t in tickets if t.get("status") == "pending"],
        key=lambda x: x.get("created_at", ""),
        reverse=True
    )[:10]
    race_names_map = {r["id"]: r["name"] for r in races}
    for u in unanswered:
        u["race_name"] = race_names_map.get(u.get("race_id"), "—")

    # Tasso risposta automatica
    answered_rate = round(sum(1 for q in questions if q.get("answered")) / len(questions) * 100) if questions else 0

    return templates.TemplateResponse(request=request, name="stats.html", context={
        "organizer": organizer,
        "race_stats": race_stats,
        "top_questions": top_questions,
        "total_questions": len(questions),
        "total_answered": sum(1 for q in questions if q.get("answered")),
        "total_tickets": len(tickets),
        "tickets_open": sum(1 for t in tickets if t.get("status") == "pending"),
        "answered_rate": answered_rate,
        "days_30": list(questions_by_day_30.keys()),
        "counts_30": list(questions_by_day_30.values()),
        "days_7": days_7,
        "counts_7": [questions_by_day_30[d] for d in days_7],
        "race_chart_labels": [rs["name"][:20] for rs in race_stats],
        "race_chart_data": [rs["total"] for rs in race_stats],
        "unanswered": unanswered,
    })


@app.get("/dashboard/events/{event_id}", response_class=HTMLResponse)
def page_event_detail(request: Request, event_id: str, session: str = Cookie(default=None)):
    organizer = get_current_organizer(session) if session else None
    if not organizer:
        return RedirectResponse(url="/login")
    result = supabase.table("events").select("*").eq("id", event_id).execute()
    if not result.data:
        raise HTTPException(status_code=404)
    event = result.data[0]
    races = supabase.table("races").select("*").eq("event_id", event_id).execute().data or []
    event_locations = supabase.table("locations").select("*").eq("event_id", event_id).execute().data or []
    org_plan = organizer.get("plan") or "single"
    all_races_count = len(supabase.table("races").select("id").eq("organizer_id", organizer["id"]).execute().data or [])
    return templates.TemplateResponse(request=request, name="event_detail.html", context={
        "organizer": organizer,
        "event": event,
        "races": races,
        "event_locations": event_locations,
        "plan_label": plan_label(org_plan),
        "plan_max_races": PLAN_MAX_RACES.get(org_plan),
        "total_races_count": all_races_count,
    })


@app.get("/chat/event/{event_id}", response_class=HTMLResponse)
def page_event_chat(request: Request, event_id: str):
    result = supabase.table("events").select("*").eq("id", event_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Evento non trovato")
    event = result.data[0]
    races = supabase.table("races").select("*").eq("event_id", event_id).execute().data or []
    return templates.TemplateResponse(request=request, name="event_chat.html", context={
        "event": event,
        "races": races
    })


@app.get("/p/{race_id}", response_class=HTMLResponse)
def page_public_race(request: Request, race_id: str):
    result = supabase.table("races").select("*").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Gara non trovata")
    race = result.data[0]
    gpx_track = race.get("gpx_data") or []
    gpx_bbox = None
    if gpx_track:
        lats = [p[0] for p in gpx_track if p and len(p) >= 2]
        lons = [p[1] for p in gpx_track if p and len(p) >= 2]
        if lats and lons:
            gpx_bbox = [[min(lats), min(lons)], [max(lats), max(lons)]]
    locs = get_locations(race_id)
    return templates.TemplateResponse(request=request, name="race_public.html", context={
        "race": race,
        "gpx_points": len(gpx_track),
        "gpx_bbox": gpx_bbox,
        "locations": locs,
    })


@app.get("/p/event/{event_id}", response_class=HTMLResponse)
def page_public_event(request: Request, event_id: str):
    result = supabase.table("events").select("*").eq("id", event_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Evento non trovato")
    event = result.data[0]
    races = supabase.table("races").select("*").eq("event_id", event_id).execute().data or []
    for race in races:
        race["sport_type_emoji"] = SPORT_EMOJIS.get(race.get("sport_type", ""), "🏅")
    event["sport_type_emoji"] = SPORT_EMOJIS.get(event.get("sport_type", ""), "🏅")
    sport_labels = {"trail":"Trail Running","running":"Corsa","cycling":"Ciclismo","mtb":"MTB","triathlon":"Triathlon","ski":"Sci","ski_fondo":"Sci di Fondo","swim":"Nuoto","kayak":"Kayak","trekking":"Trekking","obstacle":"OCR","altro":"Sport"}
    event["sport_label"] = sport_labels.get(event.get("sport_type", ""), "Sport")
    return templates.TemplateResponse(request=request, name="event_public.html", context={
        "event": event,
        "races": races,
    })


@app.get("/widget/{race_id}", response_class=HTMLResponse)
def page_widget(request: Request, race_id: str):
    result = supabase.table("races").select("*").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Gara non trovata")
    race = result.data[0]
    race["sport_type_emoji"] = SPORT_EMOJIS.get(race.get("sport_type", ""), "🏃")
    return templates.TemplateResponse(request=request, name="widget.html", context={"race": race})


@app.get("/widget-preview/{race_id}", response_class=HTMLResponse)
def page_widget_preview(request: Request, race_id: str):
    result = supabase.table("races").select("*").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Gara non trovata")
    race = result.data[0]
    color = race.get("chatbot_color") or "#2563eb"
    name = race.get("chatbot_name") or race["name"]
    welcome = race.get("welcome_message") or f"Ciao! 👋 Sono l'assistente di <strong>{race['name']}</strong>. Come posso aiutarti?"
    emoji = race.get("sport_type_emoji") or "🏃"
    logo_html = f'<img src="{race["chatbot_logo_url"]}" style="width:30px;height:30px;object-fit:contain;border-radius:6px;" alt="logo">' if race.get("chatbot_logo_url") else emoji

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Anteprima Widget — {race['name']}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f1f5f9; }}
    .preview-bar {{ background: #0f172a; color: white; padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }}
    .preview-label {{ font-size: 12px; font-weight: 700; color: #4ade80; letter-spacing: 0.5px; text-transform: uppercase; }}
    .preview-name {{ font-size: 14px; color: rgba(255,255,255,0.7); margin-left: 12px; }}
    .preview-hint {{ font-size: 12px; color: rgba(255,255,255,0.5); }}
    .fake-site {{ max-width: 900px; margin: 40px auto; padding: 0 24px 120px; }}
    .fake-header {{ background: white; border-radius: 12px; padding: 20px 28px; margin-bottom: 24px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
    .fake-logo {{ font-size: 18px; font-weight: 800; color: #0f172a; }}
    .fake-nav {{ display: flex; gap: 20px; }}
    .fake-nav span {{ font-size: 13px; color: #94a3b8; }}
    .fake-hero {{ background: linear-gradient(135deg, #1e3a8a, {color}); border-radius: 12px; padding: 48px 32px; text-align: center; margin-bottom: 24px; }}
    .fake-hero h1 {{ font-size: 28px; font-weight: 800; color: white; margin-bottom: 8px; }}
    .fake-hero p {{ color: rgba(255,255,255,0.75); font-size: 14px; }}
    .fake-content {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }}
    .fake-card {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
    .fake-card h3 {{ font-size: 14px; font-weight: 700; color: #0f172a; margin-bottom: 12px; }}
    .fake-line {{ height: 10px; background: #f1f5f9; border-radius: 4px; margin-bottom: 8px; }}

    /* WIDGET INLINE */
    #rb-bubble {{ position: fixed; bottom: 24px; right: 24px; width: 58px; height: 58px; background: linear-gradient(135deg, {color}, {color}cc); border-radius: 50%; display: flex; align-items: center; justify-content: center; cursor: pointer; z-index: 9999; box-shadow: 0 6px 24px rgba(37,99,235,0.45); transition: transform 0.2s, box-shadow 0.2s; font-size: 24px; }}
    #rb-bubble:hover {{ transform: scale(1.08); }}
    #rb-bubble.open {{ transform: scale(0.92); }}
    #rb-window {{ position: fixed; bottom: 96px; right: 24px; width: 360px; height: 520px; background: white; border-radius: 20px; box-shadow: 0 16px 60px rgba(0,0,0,0.18); display: flex; flex-direction: column; overflow: hidden; z-index: 9998; transform: scale(0.85) translateY(20px); opacity: 0; pointer-events: none; transition: transform 0.25s cubic-bezier(0.34,1.56,0.64,1), opacity 0.2s; transform-origin: bottom right; }}
    #rb-window.open {{ transform: scale(1) translateY(0); opacity: 1; pointer-events: all; }}
    .rb-header {{ background: linear-gradient(135deg, #1e3a8a, {color}); padding: 14px 16px; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }}
    .rb-header-left {{ display: flex; align-items: center; gap: 10px; }}
    .rb-avatar {{ width: 36px; height: 36px; border-radius: 10px; background: rgba(255,255,255,0.15); border: 1.5px solid rgba(255,255,255,0.25); display: flex; align-items: center; justify-content: center; font-size: 18px; }}
    .rb-title {{ font-size: 14px; font-weight: 800; color: white; }}
    .rb-subtitle {{ font-size: 10px; color: rgba(255,255,255,0.65); display: flex; align-items: center; gap: 4px; margin-top: 1px; }}
    .rb-dot {{ width: 5px; height: 5px; background: #4ade80; border-radius: 50%; }}
    .rb-close {{ background: rgba(255,255,255,0.12); border: none; color: white; width: 28px; height: 28px; border-radius: 8px; cursor: pointer; font-size: 14px; display: flex; align-items: center; justify-content: center; }}
    .rb-messages {{ flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 12px; background: #f8fafc; }}
    .rb-row {{ display: flex; align-items: flex-end; gap: 7px; }}
    .rb-row.user {{ flex-direction: row-reverse; }}
    .rb-av {{ width: 30px; height: 30px; border-radius: 50%; background: linear-gradient(135deg,{color},#7c3aed); display: flex; align-items: center; justify-content: center; font-size: 16px; flex-shrink: 0; color: white; font-weight: 900; box-shadow: 0 2px 8px rgba(37,99,235,0.3); animation: rb-av-pulse 3s ease-in-out infinite; }}
    @keyframes rb-av-pulse {{ 0%,100%{{ box-shadow: 0 2px 8px rgba(37,99,235,0.25); }} 50%{{ box-shadow: 0 3px 16px rgba(124,58,237,0.5); }} }}
    .rb-msg {{ max-width: 80%; padding: 9px 13px; font-size: 13px; line-height: 1.6; border-radius: 16px; word-wrap: break-word; }}
    .rb-msg.bot {{ background: white; color: #1e293b; border: 1px solid #e2e8f0; border-bottom-left-radius: 3px; box-shadow: 0 1px 4px rgba(0,0,0,0.05); }}
    .rb-msg.user {{ background: linear-gradient(135deg,{color},#1d4ed8); color: white; border-bottom-right-radius: 3px; }}
    .rb-msg.streaming::after {{ content:"▋"; display:inline; animation:blink 0.7s step-end infinite; color:{color}; margin-left:2px; font-size:11px; }}
    @keyframes blink {{ 0%,100%{{opacity:1;}} 50%{{opacity:0;}} }}
    .typing-dots {{ display:flex; align-items:center; gap:4px; padding:10px 14px; }}
    .typing-dots span {{ width:6px; height:6px; background:#94a3b8; border-radius:50%; animation:bounce 1.2s infinite; }}
    .typing-dots span:nth-child(2){{animation-delay:0.2s;}} .typing-dots span:nth-child(3){{animation-delay:0.4s;}}
    @keyframes bounce{{0%,60%,100%{{transform:translateY(0);opacity:.4;}}30%{{transform:translateY(-6px);opacity:1;}}}}
    .rb-quick {{ display:flex; flex-wrap:wrap; gap:6px; padding:2px 0 4px 33px; }}
    .rb-quick-btn {{ font-size:11px; padding:4px 10px; border-radius:20px; background:#eff6ff; border:1px solid #bfdbfe; color:#1d4ed8; cursor:pointer; white-space:nowrap; }}
    .rb-input-area {{ padding: 10px 12px 12px; display: flex; gap: 8px; align-items: center; border-top: 1px solid #e2e8f0; background: white; flex-shrink: 0; }}
    .rb-input-wrap {{ flex:1; display:flex; align-items:center; background:#f8fafc; border:1.5px solid #e2e8f0; border-radius:20px; padding:0 12px; }}
    .rb-input-wrap:focus-within {{ border-color:{color}; background:white; }}
    .rb-input-wrap input {{ flex:1; padding:9px 0; border:none; background:transparent; font-size:13px; outline:none; color:#0f172a; }}
    .rb-send {{ width:36px; height:36px; border-radius:10px; background:{color}; border:none; color:white; font-size:16px; cursor:pointer; display:flex; align-items:center; justify-content:center; flex-shrink:0; }}
    .rb-powered {{ text-align:center; font-size:10px; color:#94a3b8; padding-bottom:4px; background:white; }}
    .rb-powered a {{ color:{color}; text-decoration:none; font-weight:700; }}
  </style>
</head>
<body>
  <div class="preview-bar">
    <div style="display:flex;align-items:center;">
      <div class="preview-label">👁️ Anteprima widget</div>
      <div class="preview-name">{race['name']}</div>
    </div>
    <div class="preview-hint">Clicca la bolla in basso a destra →</div>
  </div>

  <div class="fake-site">
    <div class="fake-header">
      <div class="fake-logo">🏔️ ASD Trail Running</div>
      <div class="fake-nav"><span>Home</span><span>Gare</span><span>Info</span><span>Contatti</span></div>
    </div>
    <div class="fake-hero">
      <h1>{race['name']}</h1>
      <p>Benvenuto sulla pagina ufficiale della gara — il chatbot risponde a tutte le tue domande</p>
    </div>
    <div class="fake-content">
      <div class="fake-card">
        <h3>Informazioni evento</h3>
        <div class="fake-line" style="width:80%"></div>
        <div class="fake-line" style="width:60%"></div>
        <div class="fake-line" style="width:70%"></div>
        <div class="fake-line" style="width:50%"></div>
      </div>
      <div class="fake-card">
        <h3>Come arrivare</h3>
        <div class="fake-line"></div>
        <div class="fake-line" style="width:75%"></div>
        <div class="fake-line" style="width:85%"></div>
      </div>
    </div>
  </div>

  <!-- WIDGET INLINE -->
  <div id="rb-bubble" onclick="rbToggle()"><span>💬</span></div>
  <div id="rb-window">
    <div class="rb-header">
      <div class="rb-header-left">
        <div class="rb-avatar">{logo_html}</div>
        <div>
          <div class="rb-title">{name}</div>
          <div class="rb-subtitle"><span class="rb-dot"></span> Assistente attivo</div>
        </div>
      </div>
      <button class="rb-close" onclick="rbToggle()">✕</button>
    </div>
    <div class="rb-messages" id="rb-messages">
      <div class="rb-row">
        <div class="rb-av">{"✦" if not logo_html.startswith("<img") else ""}{logo_html if logo_html.startswith("<img") else ""}</div>
        <div class="rb-msg bot">{welcome}</div>
      </div>
    </div>
    <div class="rb-quick" id="rb-quick">
      <button class="rb-quick-btn" onclick="rbQuick(this,'Dove parcheggio?')">🅿️ Parcheggi?</button>
      <button class="rb-quick-btn" onclick="rbQuick(this,'A che ora parte la gara?')">⏰ Orario?</button>
      <button class="rb-quick-btn" onclick="rbQuick(this,'Materiale obbligatorio?')">🎒 Materiale?</button>
    </div>
    <div class="rb-input-area">
      <div class="rb-input-wrap">
        <input type="text" id="rb-input" placeholder="Scrivi una domanda..." autocomplete="off">
      </div>
      <button class="rb-send" id="rb-send" onclick="rbSend()">➤</button>
    </div>
    <div class="rb-powered">Powered by <a href="https://repliq.it" target="_blank">Repliq</a></div>
  </div>

  <script>
    const RACE_ID = "{race_id}";
    const msgs = document.getElementById("rb-messages");
    const input = document.getElementById("rb-input");
    const sendBtn = document.getElementById("rb-send");
    let history = [];
    function rbToggle() {{
      document.getElementById("rb-bubble").classList.toggle("open");
      document.getElementById("rb-window").classList.toggle("open");
      if (document.getElementById("rb-window").classList.contains("open")) input.focus();
    }}
    function rbQuick(btn, q) {{
      const qc = document.getElementById("rb-quick");
      if (qc) qc.remove();
      input.value = q;
      rbSend();
    }}
    input.addEventListener("keydown", e => {{ if (e.key === "Enter") rbSend(); }});
    function rbFmt(text) {{
      return text.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
        .replace(/\\n/g,"<br>").replace(/\*\*(.*?)\*\*/g,"<strong>$1</strong>")
        .replace(/(https?:\/\/[^\\s<&]+)/g,'<a href="$1" target="_blank">🗺️ Mappa</a>');
    }}
    function rbAddRow(text, type) {{
      const row = document.createElement("div");
      row.className = "rb-row" + (type === "user" ? " user" : "");
      if (type !== "user") {{ const av = document.createElement("div"); av.className = "rb-av"; av.textContent = "✦"; row.appendChild(av); }}
      const div = document.createElement("div");
      div.className = "rb-msg " + (type === "user" ? "user" : type === "stream" ? "bot streaming" : "bot");
      if (type === "typing") {{ div.innerHTML = '<div class="typing-dots"><span></span><span></span><span></span></div>'; }}
      else {{ div.innerHTML = rbFmt(text); }}
      row.appendChild(div); msgs.appendChild(row); msgs.scrollTop = msgs.scrollHeight;
      return div;
    }}
    async function rbSend() {{
      const q = input.value.trim(); if (!q) return;
      input.value = ""; sendBtn.disabled = true;
      rbAddRow(q, "user"); history.push({{role:"user",content:q}});
      const botDiv = rbAddRow("", "stream");
      let fullText = "";
      try {{
        const fd = new FormData(); fd.append("question", q); fd.append("history", JSON.stringify(history.slice(0,-1)));
        const resp = await fetch("/api/ask/" + RACE_ID, {{method:"POST",body:fd}});
        if (!resp.ok) {{ botDiv.className="rb-msg bot"; botDiv.innerHTML=rbFmt("Errore. Riprova."); return; }}
        const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf = "";
        while (true) {{
          const {{done,value}} = await reader.read(); if (done) break;
          buf += dec.decode(value,{{stream:true}});
          const parts = buf.split("\\n\\n"); buf = parts.pop();
          for (const part of parts) {{
            if (!part.startsWith("data: ")) continue;
            const data = part.slice(6);
            if (data === "[DONE]") {{ botDiv.classList.remove("streaming"); continue; }}
            if (data.startsWith("[META]") || data.startsWith("[ERROR]")) continue;
            fullText += data.replace(/\\\\n/g,"\\n");
            botDiv.innerHTML = rbFmt(fullText); msgs.scrollTop = msgs.scrollHeight;
          }}
        }}
        history.push({{role:"assistant",content:fullText}});
      }} catch(e) {{ botDiv.classList.remove("streaming"); botDiv.innerHTML=rbFmt("Errore di connessione."); }}
      finally {{ sendBtn.disabled = false; input.focus(); }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/widget-preview/event/{event_id}", response_class=HTMLResponse)
def page_widget_preview_event(request: Request, event_id: str):
    result = supabase.table("events").select("*").eq("id", event_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Evento non trovato")
    event = result.data[0]
    races = supabase.table("races").select("id,name").eq("event_id", event_id).execute().data or []
    color = event.get("chatbot_color") or "#2563eb"
    name = event.get("chatbot_name") or event["name"]
    logo_html = f'<img src="{event["chatbot_logo_url"]}" style="width:30px;height:30px;object-fit:contain;border-radius:6px;" alt="logo">' if event.get("chatbot_logo_url") else "🏆"
    if not races:
        return HTMLResponse(content=f"<p style='font-family:system-ui;padding:40px;color:#64748b;'>Nessuna gara trovata. Aggiungi prima una gara dall'organizzatore.</p>")
    import json
    races_json = json.dumps([{"id": r["id"], "name": r["name"]} for r in races])

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Anteprima Widget — {event['name']}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f1f5f9; }}
    .preview-bar {{ background: #0f172a; color: white; padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }}
    .preview-label {{ font-size: 12px; font-weight: 700; color: #4ade80; letter-spacing: 0.5px; text-transform: uppercase; }}
    .preview-name {{ font-size: 14px; color: rgba(255,255,255,0.7); margin-left: 12px; }}
    .preview-hint {{ font-size: 12px; color: rgba(255,255,255,0.5); }}
    .fake-site {{ max-width: 900px; margin: 40px auto; padding: 0 24px 120px; }}
    .fake-header {{ background: white; border-radius: 12px; padding: 20px 28px; margin-bottom: 24px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
    .fake-logo {{ font-size: 18px; font-weight: 800; color: #0f172a; }}
    .fake-nav {{ display: flex; gap: 20px; }}
    .fake-nav span {{ font-size: 13px; color: #94a3b8; }}
    .fake-hero {{ background: linear-gradient(135deg, #1e3a8a, {color}); border-radius: 12px; padding: 48px 32px; text-align: center; margin-bottom: 24px; }}
    .fake-hero h1 {{ font-size: 28px; font-weight: 800; color: white; margin-bottom: 8px; }}
    .fake-hero p {{ color: rgba(255,255,255,0.75); font-size: 14px; }}
    .fake-content {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }}
    .fake-card {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
    .fake-card h3 {{ font-size: 14px; font-weight: 700; color: #0f172a; margin-bottom: 12px; }}
    .fake-line {{ height: 10px; background: #f1f5f9; border-radius: 4px; margin-bottom: 8px; }}
    #rb-bubble {{ position: fixed; bottom: 24px; right: 24px; width: 58px; height: 58px; background: {color}; border-radius: 50%; display: flex; align-items: center; justify-content: center; cursor: pointer; z-index: 9999; box-shadow: 0 6px 24px rgba(37,99,235,0.45); transition: transform 0.2s; font-size: 24px; }}
    #rb-bubble:hover {{ transform: scale(1.08); }}
    #rb-window {{ position: fixed; bottom: 96px; right: 24px; width: 360px; height: 520px; background: white; border-radius: 20px; box-shadow: 0 16px 60px rgba(0,0,0,0.18); display: flex; flex-direction: column; overflow: hidden; z-index: 9998; transform: scale(0.85) translateY(20px); opacity: 0; pointer-events: none; transition: transform 0.25s cubic-bezier(0.34,1.56,0.64,1), opacity 0.2s; transform-origin: bottom right; }}
    #rb-window.open {{ transform: scale(1) translateY(0); opacity: 1; pointer-events: all; }}
    .rb-header {{ background: linear-gradient(135deg, #1e3a8a, {color}); padding: 14px 16px; display: flex; align-items: center; justify-content: space-between; flex-shrink: 0; }}
    .rb-header-left {{ display: flex; align-items: center; gap: 10px; }}
    .rb-avatar {{ width: 36px; height: 36px; border-radius: 10px; background: rgba(255,255,255,0.15); display: flex; align-items: center; justify-content: center; font-size: 18px; }}
    .rb-title {{ font-size: 14px; font-weight: 800; color: white; }}
    .rb-subtitle {{ font-size: 10px; color: rgba(255,255,255,0.65); display: flex; align-items: center; gap: 4px; margin-top: 1px; }}
    .rb-dot {{ width: 5px; height: 5px; background: #4ade80; border-radius: 50%; }}
    .rb-close {{ background: rgba(255,255,255,0.12); border: none; color: white; width: 28px; height: 28px; border-radius: 8px; cursor: pointer; font-size: 14px; display: flex; align-items: center; justify-content: center; }}
    .rb-messages {{ flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 12px; background: #f8fafc; }}
    .rb-row {{ display: flex; align-items: flex-end; gap: 7px; }}
    .rb-row.user {{ flex-direction: row-reverse; }}
    .rb-av {{ width: 30px; height: 30px; border-radius: 50%; background: linear-gradient(135deg, {color}, #7c3aed); background-size: 200% 200%; display: flex; align-items: center; justify-content: center; font-size: 14px; flex-shrink: 0; box-shadow: 0 2px 8px rgba(37,99,235,0.3); animation: rb-av-pulse 3s ease-in-out infinite; }}
    @keyframes rb-av-pulse {{ 0%,100%{{ box-shadow: 0 2px 8px rgba(37,99,235,0.25); }} 50%{{ box-shadow: 0 3px 16px rgba(124,58,237,0.5); }} }}
    .rb-msg {{ max-width: 80%; padding: 9px 13px; font-size: 13px; line-height: 1.6; border-radius: 16px; word-wrap: break-word; }}
    .rb-msg.bot {{ background: white; color: #1e293b; border: 1px solid #e2e8f0; border-bottom-left-radius: 3px; }}
    .rb-msg.user {{ background: {color}; color: white; border-bottom-right-radius: 3px; }}
    .rb-quick {{ display:flex; flex-wrap:wrap; gap:6px; padding:2px 0 4px 33px; }}
    .rb-quick-btn {{ font-size:11px; padding:4px 10px; border-radius:20px; background:#eff6ff; border:1px solid #bfdbfe; color:#1d4ed8; cursor:pointer; }}
    .rb-input-area {{ padding: 10px 12px 12px; display: flex; gap: 8px; align-items: center; border-top: 1px solid #e2e8f0; background: white; flex-shrink: 0; }}
    .rb-input-wrap {{ flex:1; display:flex; align-items:center; background:#f8fafc; border:1.5px solid #e2e8f0; border-radius:20px; padding:0 12px; }}
    .rb-input-wrap input {{ flex:1; padding:9px 0; border:none; background:transparent; font-size:13px; outline:none; }}
    .rb-send {{ width:36px; height:36px; border-radius:10px; background:{color}; border:none; color:white; font-size:16px; cursor:pointer; display:flex; align-items:center; justify-content:center; flex-shrink:0; }}
    .rb-powered {{ text-align:center; font-size:10px; color:#94a3b8; padding-bottom:4px; background:white; }}
  </style>
</head>
<body>
  <div class="preview-bar">
    <div style="display:flex;align-items:center;">
      <div class="preview-label">👁️ Anteprima widget</div>
      <div class="preview-name">{event['name']}</div>
    </div>
    <div class="preview-hint">Clicca la bolla in basso a destra →</div>
  </div>
  <div class="fake-site">
    <div class="fake-header">
      <div class="fake-logo">🏆 {event['name']}</div>
      <div class="fake-nav"><span>Home</span><span>Gare</span><span>Info</span><span>Contatti</span></div>
    </div>
    <div class="fake-hero">
      <h1>{event['name']}</h1>
      <p>Benvenuto sulla pagina ufficiale dell'evento</p>
    </div>
    <div class="fake-content">
      <div class="fake-card"><h3>Informazioni evento</h3><div class="fake-line" style="width:80%"></div><div class="fake-line" style="width:60%"></div></div>
      <div class="fake-card"><h3>Come arrivare</h3><div class="fake-line"></div><div class="fake-line" style="width:75%"></div></div>
    </div>
  </div>
  <div id="rb-bubble" onclick="rbToggle()"><span>💬</span></div>
  <div id="rb-window">
    <div class="rb-header">
      <div class="rb-header-left">
        <div class="rb-avatar">{logo_html}</div>
        <div><div class="rb-title">{name}</div><div class="rb-subtitle"><span class="rb-dot"></span> Assistente attivo</div></div>
      </div>
      <button class="rb-close" onclick="rbToggle()">✕</button>
    </div>
    <div class="rb-messages" id="rb-messages">
      <div class="rb-row"><div class="rb-av" style="color:white;font-weight:900;font-size:16px;">✦</div><div class="rb-msg bot">Ciao! 👋 A quale gara partecipi?</div></div>
    </div>
    <div class="rb-quick" id="rb-race-select"></div>
    <div class="rb-input-area" id="rb-input-area" style="display:none;">
      <div class="rb-input-wrap"><input type="text" id="rb-input" placeholder="Scrivi una domanda..." autocomplete="off" disabled></div>
      <button class="rb-send" id="rb-send" onclick="rbSend()" disabled>➤</button>
    </div>
    <div class="rb-powered">Powered by <a href="https://repliq.it" target="_blank">Repliq</a></div>
  </div>
  <script>
    const RACES = {races_json};
    const msgs = document.getElementById("rb-messages");
    const input = document.getElementById("rb-input");
    const sendBtn = document.getElementById("rb-send");
    let history = [];
    let RACE_ID = null;

    const raceSelect = document.getElementById("rb-race-select");

    function selectRace(id, raceName) {{
      RACE_ID = id;
      if (raceSelect) raceSelect.remove();
      document.getElementById("rb-input-area").style.display = "flex";
      input.disabled = false;
      sendBtn.disabled = false;
      input.focus();
    }}

    if (RACES.length === 1) {{
      // Una sola gara: salta la selezione, vai diretto alla chat
      const r = RACES[0];
      msgs.innerHTML = '<div class="rb-row"><div class="rb-av">🏆</div><div class="rb-msg bot">Ciao! 👋 Sono l&#39;assistente di <strong>' + r.name + '</strong>. Come posso aiutarti?</div></div>';
      if (raceSelect) raceSelect.remove();
      selectRace(r.id, r.name);
    }} else {{
      // Più gare: mostra selezione
      RACES.forEach(r => {{
        const btn = document.createElement("button");
        btn.className = "rb-quick-btn";
        btn.textContent = "🏁 " + r.name;
        btn.onclick = () => {{
          rbAddRow("🏁 " + r.name, "user");
          rbAddRow("Perfetto! Sono pronto per le tue domande su <strong>" + r.name + "</strong>. Come posso aiutarti?", "bot");
          selectRace(r.id, r.name);
        }};
        raceSelect.appendChild(btn);
      }});
    }}

    function rbToggle() {{
      document.getElementById("rb-bubble").classList.toggle("open");
      document.getElementById("rb-window").classList.toggle("open");
      if (document.getElementById("rb-window").classList.contains("open") && !RACE_ID) raceSelect && raceSelect.scrollIntoView();
    }}
    input.addEventListener("keydown", e => {{ if (e.key === "Enter" && RACE_ID) rbSend(); }});
    function rbFmt(t) {{
      return t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
        .replace(/\\n/g,"<br>").replace(/\*\*(.*?)\*\*/g,"<strong>$1</strong>");
    }}
    function rbAddRow(text, type) {{
      const row = document.createElement("div"); row.className="rb-row"+(type==="user"?" user":"");
      if (type!=="user") {{ const av=document.createElement("div"); av.className="rb-av"; av.style.color="white"; av.style.fontWeight="900"; av.style.fontSize="16px"; av.textContent="✦"; row.appendChild(av); }}
      const div=document.createElement("div"); div.className="rb-msg "+(type==="user"?"user":"bot");
      div.innerHTML=rbFmt(text); row.appendChild(div); msgs.appendChild(row); msgs.scrollTop=msgs.scrollHeight; return div;
    }}
    async function rbSend() {{
      const q=input.value.trim(); if(!q) return;
      input.value=""; sendBtn.disabled=true;
      rbAddRow(q,"user"); history.push({{role:"user",content:q}});
      const botDiv=rbAddRow("...","bot"); let fullText="";
      try {{
        const fd=new FormData(); fd.append("question",q); fd.append("history",JSON.stringify(history.slice(0,-1)));
        const resp=await fetch("/api/ask/"+RACE_ID,{{method:"POST",body:fd}});
        if(!resp.ok){{ botDiv.innerHTML="Errore. Riprova."; sendBtn.disabled=false; return; }}
        const reader=resp.body.getReader(); const dec=new TextDecoder(); let buf="";
        botDiv.innerHTML="";
        while(true) {{
          const {{done,value}}=await reader.read(); if(done) break;
          buf+=dec.decode(value,{{stream:true}});
          const parts=buf.split("\\n\\n"); buf=parts.pop();
          for(const part of parts) {{
            if(!part.startsWith("data: ")) continue;
            const data=part.slice(6);
            if(data==="[DONE]"||data.startsWith("[META]")||data.startsWith("[ERROR]")) continue;
            fullText+=data.replace(/\\\\n/g,"\\n");
            botDiv.innerHTML=rbFmt(fullText); msgs.scrollTop=msgs.scrollHeight;
          }}
        }}
        history.push({{role:"assistant",content:fullText}});
      }} catch(e) {{ botDiv.innerHTML="Errore di connessione."; }}
      finally {{ sendBtn.disabled=false; input.focus(); }}
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)



@app.get("/widget/event/{event_id}", response_class=HTMLResponse)
def page_widget_event(request: Request, event_id: str):
    result = supabase.table("events").select("*").eq("id", event_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Evento non trovato")
    event = result.data[0]
    chat_url = str(request.base_url) + f"chat/event/{event_id}"
    color = "#2563eb"
    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Repliq Widget — {event['name']}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: transparent; }}
    #rb-bubble {{
      position: fixed; bottom: 24px; right: 24px;
      width: 58px; height: 58px;
      background: linear-gradient(135deg, #1e3a8a, {color});
      border-radius: 50%; display: flex; align-items: center; justify-content: center;
      cursor: pointer; z-index: 9999; font-size: 24px;
      box-shadow: 0 6px 24px rgba(37,99,235,0.45);
      transition: transform 0.2s;
    }}
    #rb-bubble:hover {{ transform: scale(1.08); }}
    #rb-frame {{
      position: fixed; bottom: 96px; right: 24px;
      width: 380px; height: 580px; border: none;
      border-radius: 20px; z-index: 9998;
      box-shadow: 0 16px 60px rgba(0,0,0,0.2);
      transform: scale(0.85) translateY(20px); opacity: 0;
      pointer-events: none;
      transition: transform 0.25s cubic-bezier(0.34,1.56,0.64,1), opacity 0.2s;
      transform-origin: bottom right;
    }}
    #rb-frame.open {{ transform: scale(1) translateY(0); opacity: 1; pointer-events: all; }}
    @media(max-width:440px) {{ #rb-frame {{ width: calc(100vw - 24px); right: 12px; }} #rb-bubble {{ right: 12px; bottom: 12px; }} }}
  </style>
</head>
<body>
  <div id="rb-bubble" onclick="toggle()">💬</div>
  <iframe id="rb-frame" src="{chat_url}" title="Repliq Chatbot"></iframe>
  <script>
    function toggle() {{
      document.getElementById("rb-frame").classList.toggle("open");
      document.getElementById("rb-bubble").textContent = document.getElementById("rb-frame").classList.contains("open") ? "✕" : "💬";
    }}
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/widget-preview/event/{event_id}", response_class=HTMLResponse)
def page_widget_preview_event(request: Request, event_id: str):
    result = supabase.table("events").select("*").eq("id", event_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Evento non trovato")
    event = result.data[0]
    chat_url = str(request.base_url) + f"chat/event/{event_id}"
    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Anteprima Widget — {event['name']}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: system-ui, sans-serif; background: #f1f5f9; }}
    .preview-bar {{ background: #0f172a; color: white; padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 100; }}
    .preview-label {{ font-size: 12px; font-weight: 700; color: #4ade80; letter-spacing: 0.5px; text-transform: uppercase; }}
    .preview-name {{ font-size: 14px; color: rgba(255,255,255,0.7); margin-left: 12px; }}
    .preview-hint {{ font-size: 12px; color: rgba(255,255,255,0.5); }}
    .fake-site {{ max-width: 900px; margin: 40px auto; padding: 0 24px; }}
    .fake-header {{ background: white; border-radius: 12px; padding: 20px 28px; margin-bottom: 24px; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
    .fake-logo {{ font-size: 18px; font-weight: 800; color: #0f172a; }}
    .fake-nav {{ display: flex; gap: 20px; }}
    .fake-nav span {{ font-size: 13px; color: #94a3b8; }}
    .fake-hero {{ background: linear-gradient(135deg, #1e3a8a, #2563eb); border-radius: 12px; padding: 48px 32px; text-align: center; margin-bottom: 24px; }}
    .fake-hero h1 {{ font-size: 28px; font-weight: 800; color: white; margin-bottom: 8px; }}
    .fake-hero p {{ color: rgba(255,255,255,0.75); font-size: 14px; }}
    .fake-content {{ display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }}
    .fake-card {{ background: white; border-radius: 12px; padding: 24px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }}
    .fake-card h3 {{ font-size: 14px; font-weight: 700; color: #0f172a; margin-bottom: 12px; }}
    .fake-line {{ height: 10px; background: #f1f5f9; border-radius: 4px; margin-bottom: 8px; }}
    .widget-iframe {{ position: fixed; bottom: 0; right: 0; width: 420px; height: 620px; border: none; z-index: 9999; border-radius: 20px 0 0 0; }}
    @media(max-width:600px) {{ .fake-content {{ grid-template-columns: 1fr; }} .widget-iframe {{ width: 100%; border-radius: 0; }} }}
  </style>
</head>
<body>
  <div class="preview-bar">
    <div style="display:flex;align-items:center;">
      <div class="preview-label">👁️ Anteprima widget</div>
      <div class="preview-name">{event['name']}</div>
    </div>
    <div class="preview-hint">La bolla blu in basso a destra è il tuo chatbot</div>
  </div>
  <div class="fake-site">
    <div class="fake-header">
      <div class="fake-logo">🏔️ ASD Trail Running</div>
      <div class="fake-nav"><span>Home</span><span>Gare</span><span>Info</span><span>Contatti</span></div>
    </div>
    <div class="fake-hero">
      <h1>{event['name']}</h1>
      <p>Benvenuto sulla pagina ufficiale dell'evento — il chatbot risponde a tutte le tue domande</p>
    </div>
    <div class="fake-content">
      <div class="fake-card">
        <h3>Informazioni evento</h3>
        <div class="fake-line" style="width:80%"></div>
        <div class="fake-line" style="width:60%"></div>
        <div class="fake-line" style="width:70%"></div>
      </div>
      <div class="fake-card">
        <h3>Come arrivare</h3>
        <div class="fake-line"></div>
        <div class="fake-line" style="width:75%"></div>
      </div>
    </div>
  </div>
  <iframe src="{str(request.base_url)}widget/event/{event_id}" class="widget-iframe" title="Repliq Widget"></iframe>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/chat/{race_id}", response_class=HTMLResponse)
def page_chat(request: Request, race_id: str, session: str = Cookie(default=None)):
    result = supabase.table("races").select("*").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Gara non trovata")
    race = result.data[0]
    gpx_track = race.get("gpx_data") or []
    gpx_bbox = None
    if gpx_track:
        lats = [p[0] for p in gpx_track if p and len(p) >= 2]
        lons = [p[1] for p in gpx_track if p and len(p) >= 2]
        if lats and lons:
            gpx_bbox = [[min(lats), min(lons)], [max(lats), max(lons)]]
    is_organizer = bool(get_current_organizer(session) if session else None)
    # Aggiungi emoji sport al contesto
    race["sport_type_emoji"] = SPORT_EMOJIS.get(race.get("sport_type", ""), "🏃")
    return templates.TemplateResponse(request=request, name="chat.html", context={
        "race": race,
        "gpx_points": len(gpx_track),
        "gpx_bbox": gpx_bbox,
        "is_organizer": is_organizer
    })


# ─────────────────────────────────────────────────────────────────
# SUPER ADMIN CONSOLE
# ─────────────────────────────────────────────────────────────────

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin_login.html", context={})


@app.post("/admin/login")
def admin_login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    admin_user = os.getenv("ADMIN_USERNAME", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "")
    if not admin_pass:
        raise HTTPException(status_code=503, detail="Admin non configurato (imposta ADMIN_PASSWORD nel .env)")
    if username != admin_user or password != admin_pass:
        return templates.TemplateResponse(request=request, name="admin_login.html", context={"error": "Credenziali errate"})
    token = str(uuid.uuid4())
    ADMIN_SESSIONS.add(token)
    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie("admin_session", token, httponly=True, samesite="lax")
    return response


@app.get("/admin/logout")
def admin_logout(admin_session: str = Cookie(default=None)):
    ADMIN_SESSIONS.discard(admin_session or "")
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, admin_session: str = Cookie(default=None)):
    if not _admin_token_valid(admin_session):
        return RedirectResponse(url="/admin/login", status_code=303)

    organizers_all = supabase.table("organizers").select("*").execute().data or []
    pending_organizers = [o for o in organizers_all if o.get("status") == "pending"]
    organizers = [o for o in organizers_all if o.get("status") != "pending"]
    all_races = supabase.table("races").select("id, organizer_id").execute().data or []
    all_tickets = supabase.table("tickets").select("id, race_id").execute().data or []
    all_races_full = supabase.table("races").select("id, organizer_id").execute().data or []

    # Conta gare e ticket per organizzatore
    races_by_org: dict[str, int] = {}
    for r in all_races_full:
        oid = r.get("organizer_id", "")
        races_by_org[oid] = races_by_org.get(oid, 0) + 1

    # Arricchisci organizzatori
    for org in organizers:
        org["race_count"] = races_by_org.get(org["id"], 0)
        org["plan_label"] = plan_label(org.get("plan") or "single")
        org["plan_color"] = plan_color(org.get("plan") or "single")

    # Stats aggregate
    plan_counts: dict[str, int] = {}
    for org in organizers:
        p = org.get("plan") or "single"
        plan_counts[p] = plan_counts.get(p, 0) + 1

    aggregate = {
        "total_organizers": len(organizers),
        "total_races": len(all_races),
        "total_tickets": len(all_tickets),
        "plan_counts": {PLAN_LABELS.get(k, k): v for k, v in plan_counts.items()},
    }

    from datetime import datetime
    return templates.TemplateResponse(request=request, name="admin.html", context={
        "organizers": organizers,
        "pending_organizers": pending_organizers,
        "aggregate": aggregate,
        "plan_options": PLAN_ORDER,
        "plan_labels": PLAN_LABELS,
        "now_str": datetime.utcnow().strftime("%Y-%m-%d"),
    })


@app.post("/admin/organizers/{organizer_id}/update")
def admin_update_organizer(
    organizer_id: str,
    plan: str = Form(...),
    plan_expires_at: str = Form(""),
    admin_notes: str = Form(""),
    admin_session: str = Cookie(default=None),
):
    if not _admin_token_valid(admin_session):
        raise HTTPException(status_code=403, detail="Non autorizzato")
    if plan not in PLAN_ORDER:
        raise HTTPException(status_code=400, detail="Piano non valido")
    supabase.table("organizers").update({
        "plan": plan,
        "plan_expires_at": plan_expires_at or None,
        "admin_notes": admin_notes or None,
    }).eq("id", organizer_id).execute()
    return RedirectResponse(url="/admin?ok=Organizzatore+aggiornato", status_code=303)


@app.post("/admin/organizers/{organizer_id}/approve")
def admin_approve_organizer(
    organizer_id: str,
    plan: str = Form("single"),
    admin_session: str = Cookie(default=None),
):
    if not _admin_token_valid(admin_session):
        raise HTTPException(status_code=403, detail="Non autorizzato")
    supabase.table("organizers").update({
        "status": "active",
        "plan": plan if plan in PLAN_ORDER else "single",
    }).eq("id", organizer_id).execute()
    # Invia email di approvazione all'organizzatore
    org = supabase.table("organizers").select("email,name").eq("id", organizer_id).execute().data
    if org:
        send_approval_email(org[0]["email"], org[0]["name"], plan if plan in PLAN_ORDER else "single")
    return RedirectResponse(url="/admin?ok=Account+approvato", status_code=303)


@app.post("/admin/organizers/{organizer_id}/reject")
def admin_reject_organizer(
    organizer_id: str,
    admin_session: str = Cookie(default=None),
):
    if not _admin_token_valid(admin_session):
        raise HTTPException(status_code=403, detail="Non autorizzato")
    supabase.table("organizers").update({"status": "suspended"}).eq("id", organizer_id).execute()
    return RedirectResponse(url="/admin?ok=Account+rifiutato", status_code=303)
