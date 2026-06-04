"""
tickets.py — Gestione ticket per domande senza risposta
Sprint 3: crea ticket, notifica email organizzatore, risposta dal pannello
"""

import uuid
from database import supabase
from emails import notify_organizer_ticket, notify_participant_reply


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
    notify_organizer_ticket(organizer_email, organizer_name, ticket)


def notify_participant(participant_email: str, race_name: str, question: str, reply: str):
    """Invia email al partecipante con la risposta dell'organizzatore."""
    notify_participant_reply(participant_email, race_name, question, reply)


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
