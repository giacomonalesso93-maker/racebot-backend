"""
embeddings.py — Processa il PDF e gestisce la ricerca semantica
Sprint 1: OpenAI text-embedding-3-small per embedding + ricerca coseno in Python puro
"""

import json
import math
import os
from pathlib import Path

from pypdf import PdfReader
from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
STORE_PATH = Path(os.getenv("CHROMA_DIR", "./embeddings_store"))
EMBED_MODEL = "text-embedding-3-small"


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x ** 2 for x in a))
    norm_b = math.sqrt(sum(x ** 2 for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _get_store_file(race_id: str) -> Path:
    STORE_PATH.mkdir(parents=True, exist_ok=True)
    return STORE_PATH / f"{race_id}.json"


def extract_text_from_pdf(pdf_path: str) -> str:
    reader = PdfReader(pdf_path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def split_into_chunks(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i: i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return [c for c in chunks if c.strip()]


def embed_text(text: str) -> list[float]:
    response = client.embeddings.create(
        model=EMBED_MODEL,
        input=text
    )
    return response.data[0].embedding


def process_pdf(pdf_path: str, race_id: str) -> int:
    """Legge il PDF, lo divide in chunk, crea gli embedding e li salva."""
    text = extract_text_from_pdf(pdf_path)
    chunks = split_into_chunks(text)

    store = []
    for chunk in chunks:
        vector = embed_text(chunk)
        store.append({"text": chunk, "vector": vector})

    store_file = _get_store_file(race_id)
    with open(store_file, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False)

    return len(chunks)


def search(query: str, race_id: str, top_k: int = 4) -> list[str]:
    """Cerca i chunk più rilevanti per la domanda dell'utente."""
    store_file = _get_store_file(race_id)
    if not store_file.exists():
        return []

    with open(store_file, "r", encoding="utf-8") as f:
        store = json.load(f)

    query_vector = embed_text(query)

    scored = [
        (item["text"], _cosine_similarity(query_vector, item["vector"]))
        for item in store
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

    return [text for text, _ in scored[:top_k]]
