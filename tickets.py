"""
tickets.py — Gestione ticket per domande senza risposta
Sprint 3: crea ticket, notifica email organizzatore, risposta dal pannello
"""

import os
import uuid
import resend
from dotenv import load_dotenv
from database import supabase

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
EMAIL_FROM = os.getenv("EMAIL_FROM", "onboarding@resend.dev")
resend.api_key = RESEND_API_KEY


def create_ticket(race_id: str, race_name: str, question: str, participant_email: str = None) -> dict:
    """Crea un nuovo ticket per una domanda senza risposta."""
    ticket = {
        "id": str(uuid.uuid4()),
        "race_id": race_id,
        "race_name": race_name,
        "question": question,
        "participant_email": participant_email,
        "status": "aperto"
    }
    result = supabase.table("tickets").insert(ticket).execute()
    return result.data[0] if result.data else ticket


def notify_organizer(organizer_email: str, organizer_name: str, ticket: dict):
    """Invia email all'organizzatore per un nuovo ticket."""
    try:
        params = {
            "from": EMAIL_FROM,
            "to": [organizer_email],
            "subject": f"Nuovo ticket — {ticket['race_name']}",
            "html": f"""
            <div style="font-family: system-ui, sans-serif; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #2563eb;">Nuovo ticket da rispondere</h2>
                <p>Ciao <strong>{organizer_name}</strong>,</p>
                <p>Un partecipante ha posto una domanda a cui il chatbot non ha saputo rispondere.</p>
                <div style="background: #f3f4f6; border-radius: 8px; padding: 16px; margin: 20px 0;">
                    <strong>Gara:</strong> {ticket['race_name']}<br><br>
                    <strong>Domanda:</strong> {ticket['question']}
                </div>
                <p>Accedi al pannello per rispondere:</p>
                <a href="http://localhost:8000/dashboard/tickets"
                   style="background: #2563eb; color: white; padding: 10px 20px; border-radius: 6px; text-decoration: none; display: inline-block; margin-top: 8px;">
                   Vai ai ticket
                </a>
            </div>
            """
        }
        result = resend.Emails.send(params)
        print(f"Email organizzatore inviata: {result}")
    except Exception as e:
        print(f"Errore invio email organizzatore: {e}")


def notify_participant(participant_email: str, race_name: str, question: str, reply: str):
    """Invia email al partecipante con la risposta dell'organizzatore."""
    try:
        params = {
            "from": EMAIL_FROM,
            "to": [participant_email],
            "subject": f"Risposta alla tua domanda — {race_name}",
            "html": f"""
            <div style="font-family: system-ui, sans-serif; max-width: 600px; margin: 0 auto;">
                <h2 style="color: #2563eb;">Risposta alla tua domanda</h2>
                <p>Hai chiesto:</p>
                <div style="background: #f3f4f6; border-radius: 8px; padding: 16px; margin: 16px 0;">
                    {question}
                </div>
                <p>Risposta dell'organizzatore:</p>
                <div style="background: #d1fae5; border-radius: 8px; padding: 16px; margin: 16px 0;">
                    {reply}
                </div>
                <p style="color: #6b7280; font-size: 14px;">— Lo staff di {race_name}</p>
            </div>
            """
        }
        result = resend.Emails.send(params)
        print(f"Email partecipante inviata: {result}")
    except Exception as e:
        print(f"Errore invio email partecipante: {e}")


def get_tickets_for_organizer(organizer_id: str) -> list:
    """Restituisce tutti i ticket delle gare di un organizzatore."""
    races = supabase.table("races").select("id").eq("organizer_id", organizer_id).execute().data
    if not races:
        return []
    race_ids = [r["id"] for r in races]
    tickets = supabase.table("tickets").select("*").in_("race_id", race_ids).order("created_at", desc=True).execute().data
    return tickets or []


def reply_to_ticket(ticket_id: str, reply: str) -> dict:
    """Salva la risposta dell'organizzatore e aggiorna lo stato."""
    result = supabase.table("tickets").update({
        "organizer_reply": reply,
        "status": "risolto"
    }).eq("id", ticket_id).execute()
    return result.data[0] if result.data else {}
