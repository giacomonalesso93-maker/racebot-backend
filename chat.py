"""
chat.py — Chiama Claude API con il contesto estratto dal PDF
Supporta: streaming SSE, storico conversazione, multilingua, race_info, meteo
"""

import os
import anthropic
from weather import get_weather_context

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def build_system_and_sections(context_chunks: list[str], race_name: str,
                               location_context: str, qa_context: str,
                               race_info: dict = None) -> tuple[str, str]:
    """Costruisce system prompt e sezioni di contesto condivisi tra streaming e non."""
    context = "\n\n---\n\n".join(context_chunks)

    race_info_lines = []
    if race_info:
        if race_info.get("date"):
            race_info_lines.append(f"- Data: {race_info['date']}")
        if race_info.get("start_time"):
            race_info_lines.append(f"- Orario di partenza: {race_info['start_time']}")
        if race_info.get("location"):
            race_info_lines.append(f"- Località: {race_info['location']}")
        if race_info.get("length_km"):
            race_info_lines.append(f"- Lunghezza: {race_info['length_km']} km")
        if race_info.get("elevation_gain"):
            race_info_lines.append(f"- Dislivello positivo: {race_info['elevation_gain']}")
        if race_info.get("secretary_email"):
            race_info_lines.append(f"- Email segreteria: {race_info['secretary_email']}")
        if race_info.get("notes"):
            race_info_lines.append(f"- Note aggiuntive: {race_info['notes']}")
        if race_info.get("gpx_download_url"):
            race_info_lines.append(f"- Download tracciato GPX: {race_info['gpx_download_url']}")

    # Fetch meteo se disponibili data e location
    weather_context = ""
    if race_info:
        weather_context = get_weather_context(
            race_info.get("location"),
            race_info.get("date")
        )

    system_prompt = f"""You are the official virtual assistant for {race_name}.

LANGUAGE RULE: Always respond in the exact same language the user writes in.
If the user writes in Italian → respond in Italian.
If the user writes in English → respond in English.
If the user writes in French → respond in French.
If the user writes in Spanish → respond in Spanish.
If the user writes in German → respond in German.
Always be friendly and precise.

You have five information sources, in order of priority:
1. Organizer's custom answers (use these first, verbatim)
2. Basic race info (date, location, elevation, length, start time — answer immediately)
3. Official race regulations (PDF or text)
4. Locations and logistics points
5. Weather forecast for race day (use only if available and the user asks about weather)

LOCATION RULE: When mentioning any location (parking, start, finish, refreshments, etc.) that has a "Link mappa" in the context, ALWAYS include the URL in your response on a new line, exactly as provided. Example: "Parcheggio P1 in Via Roma. 🗺️ https://maps.google.com/..."

GPX RULE: If the user asks to download the GPX track, the route file, or the track for their GPS device/watch, ALWAYS include the download link from "Download tracciato GPX" in your response as a plain URL (NOT as markdown). Example: "Puoi scaricare il tracciato GPX da questo link: http://..."

SCOPE RULE: You are ONLY the assistant for {race_name}, not a general-purpose assistant. If the question has NOTHING to do with the race itself (e.g. asking for restaurant/pizzeria recommendations, hotels, generic tourist info, general life advice, or any unrelated topic), you MUST start your reply with this exact opening sentence, translated to match the user's language precisely as shown below (do not paraphrase it):
- Italian: "Questa domanda esula dal mio ambito: sono l'assistente ufficiale di {race_name} e rispondo solo a domande sulla gara (regolamento, logistica, percorso, ristori, parcheggi, orari)."
- English: "This question is outside my scope: I'm the official assistant for {race_name} and I only answer questions about the race (rules, logistics, route, aid stations, parking, schedule)."
- French: "Cette question sort de mon champ d'action : je suis l'assistant officiel de {race_name} et je réponds uniquement aux questions sur la course (règlement, logistique, parcours, ravitaillements, parkings, horaires)."
- Spanish: "Esta pregunta queda fuera de mi ámbito: soy el asistente oficial de {race_name} y solo respondo preguntas sobre la carrera (reglamento, logística, recorrido, avituallamientos, aparcamiento, horarios)."
- German: "Diese Frage liegt außerhalb meines Aufgabenbereichs: Ich bin der offizielle Assistent für {race_name} und beantworte nur Fragen zum Rennen (Reglement, Logistik, Streckenverlauf, Verpflegung, Parkplätze, Zeiten)."
After this opening sentence, you may still add a brief, friendly follow-up using official race info if something tangentially relevant exists (e.g. official food points at the race itself) and, only if helpful, suggest the user search online or ask locals — never invent specific local recommendations (restaurant names, hotels, etc.).
Do NOT use this opening for questions ABOUT the race that you simply lack information on (e.g. a course-marking detail missing from the regulation) — for those keep using the normal "I don't have this information" fallback below.

If the answer is not in any source, say (in the user's language): "I don't have this information. I recommend contacting the race secretariat."
Never invent information."""

    sections = ""
    if race_info_lines:
        sections += f"Informazioni base della gara {race_name}:\n" + "\n".join(race_info_lines) + "\n\n"
    if context:
        sections += f"Regolamento della gara:\n\n{context}"
    if location_context:
        sections += f"\n\n{location_context}"
    if qa_context:
        sections += f"\n\n{qa_context}"
    if weather_context:
        sections += f"\n\n{weather_context}"

    return system_prompt, sections


def get_answer(question: str, context_chunks: list[str], race_name: str = "la gara",
               location_context: str = "", qa_context: str = "",
               race_info: dict = None, history: list = None) -> str:
    """Risposta completa (non streaming). Usata come fallback."""
    system_prompt, sections = build_system_and_sections(
        context_chunks, race_name, location_context, qa_context, race_info
    )

    messages = _build_messages(sections, question, history)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=messages
    )
    return response.content[0].text


def stream_answer(question: str, context_chunks: list[str], race_name: str = "la gara",
                  location_context: str = "", qa_context: str = "",
                  race_info: dict = None, history: list = None):
    """
    Generator che produce testo in streaming tramite SSE.
    Yielda stringhe nel formato: 'data: <testo>\\n\\n'
    Termina con: 'data: [DONE]\\n\\n'
    """
    system_prompt, sections = build_system_and_sections(
        context_chunks, race_name, location_context, qa_context, race_info
    )

    messages = _build_messages(sections, question, history)

    with client.messages.stream(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=messages
    ) as stream:
        for text in stream.text_stream:
            # Escape newlines per SSE
            safe = text.replace("\n", "\\n")
            yield f"data: {safe}\n\n"

    yield "data: [DONE]\n\n"


def _build_messages(sections: str, question: str, history: list = None) -> list:
    """Costruisce l'array messages per Claude includendo lo storico."""
    messages = []

    # Aggiungi storico conversazione (massimo ultimi 10 scambi)
    if history:
        for msg in history[-20:]:  # max 20 messaggi (10 scambi)
            role = msg.get("role")
            content = msg.get("content", "")
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})

    # Messaggio corrente con contesto
    user_content = question
    if sections:
        user_content = f"{sections}\n\n---\n\nDomanda: {question}"

    messages.append({"role": "user", "content": user_content})
    return messages
