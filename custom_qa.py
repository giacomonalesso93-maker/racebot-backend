"""
custom_qa.py — Risposte personalizzate dell'organizzatore
L'organizzatore aggiunge coppie domanda/risposta che il chatbot usa prima di cercare nel PDF.
"""

import uuid
from database import supabase


def add_qa(race_id: str, question: str, answer: str) -> dict:
    qa = {
        "id": str(uuid.uuid4()),
        "race_id": race_id,
        "question": question.strip(),
        "answer": answer.strip()
    }
    result = supabase.table("custom_qa").insert(qa).execute()
    return result.data[0] if result.data else qa


def get_qa(race_id: str) -> list:
    result = supabase.table("custom_qa").select("*").eq("race_id", race_id).order("created_at").execute()
    return result.data or []


def delete_qa(qa_id: str):
    supabase.table("custom_qa").delete().eq("id", qa_id).execute()


def get_qa_context(race_id: str) -> str:
    """Restituisce le Q&A personalizzate come testo di contesto per Claude."""
    items = get_qa(race_id)
    if not items:
        return ""
    lines = ["Risposte personalizzate dell'organizzatore (usa queste risposte esatte quando la domanda è simile):"]
    for item in items:
        lines.append(f"D: {item['question']}")
        lines.append(f"R: {item['answer']}")
    return "\n".join(lines)
