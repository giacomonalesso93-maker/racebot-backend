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
from fastapi.templating import Jinja2Templates

load_dotenv(override=True)

from database import supabase, create_tables
from auth import register_organizer, login_organizer, get_current_organizer
from embeddings import process_pdf, search
from chat import get_answer, stream_answer
from tickets import create_ticket, notify_organizer, notify_participant, get_tickets_for_organizer, reply_to_ticket
from locations import add_location, get_locations, delete_location, update_location, TIPI_POSIZIONE
from custom_qa import add_qa, get_qa, delete_qa, get_qa_context
from plans import get_features, plan_label, plan_color, PLAN_LABELS, PLAN_ORDER, PLAN_MAX_RACES, PLAN_FEATURES

create_tables()

app = FastAPI(title="RaceBot API", version="0.3.0")

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
def api_register(email: str = Form(...), password: str = Form(...), name: str = Form(...)):
    try:
        organizer = register_organizer(email, password, name)
        if not organizer:
            raise HTTPException(status_code=400, detail="Errore durante la registrazione")
        response = RedirectResponse(url="/login", status_code=303)
        return response
    except Exception as e:
        raise HTTPException(status_code=400, detail="Email già registrata")


@app.post("/api/login")
def api_login(email: str = Form(...), password: str = Form(...)):
    token = login_organizer(email, password)
    if not token:
        raise HTTPException(status_code=401, detail="Credenziali non valide")
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
    return RedirectResponse(url="/dashboard?ok=Evento+aggiornato", status_code=303)


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
    location_context = ""
    if locs:
        lines = ["Posizioni e punti logistici della gara (IMPORTANTE: quando menzioni una posizione che ha un link mappa, includi SEMPRE il link esatto nella risposta):"]
        for loc in locs:
            line = f"- {loc['name']} ({loc['type']})"
            if loc.get("notes"):
                line += f": {loc['notes']}"
            if loc.get("provisions"):
                line += f" — Dotazione: {loc['provisions']}"
            if loc.get("google_maps_url"):
                line += f"\n  → LINK MAPPA DA INCLUDERE NELLA RISPOSTA: {loc['google_maps_url']}"
            lines.append(line)
        location_context = "\n".join(lines)

    qa_context = get_qa_context(race_id)
    race_info = {
        "date": race.get("date"),
        "location": race.get("location"),
        "elevation_gain": race.get("elevation_gain"),
        "length_km": race.get("length_km"),
        "start_time": race.get("start_time"),
        "secretary_email": race.get("secretary_email"),
        "notes": race.get("notes"),
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
    # Usa google_maps_url se disponibile, altrimenti genera da coordinate
    locs_with_links = {}
    for loc in locs:
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
    return templates.TemplateResponse(request=request, name="event_detail.html", context={
        "organizer": organizer,
        "event": event,
        "races": races
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
    return templates.TemplateResponse(request=request, name="event_public.html", context={
        "event": event,
        "races": races,
    })


@app.get("/widget/{race_id}", response_class=HTMLResponse)
def page_widget(request: Request, race_id: str):
    result = supabase.table("races").select("*").eq("id", race_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Gara non trovata")
    return templates.TemplateResponse(request=request, name="widget.html", context={"race": result.data[0]})


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

    organizers = supabase.table("organizers").select("*").execute().data or []
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
