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
