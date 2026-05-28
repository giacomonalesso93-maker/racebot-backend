"""
auth.py — Gestione login, registrazione e sessioni tramite Supabase
"""

import os
import uuid
import hashlib
import hmac
import base64
from dotenv import load_dotenv
from database import supabase

load_dotenv()


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password), password_hash)


def create_session_token(organizer_id: str) -> str:
    data = f"{organizer_id}"
    return base64.b64encode(data.encode()).decode()


def decode_session_token(token: str) -> str | None:
    try:
        return base64.b64decode(token.encode()).decode()
    except Exception:
        return None


def register_organizer(email: str, password: str, name: str) -> dict | None:
    organizer_id = str(uuid.uuid4())
    result = supabase.table("organizers").insert({
        "id": organizer_id,
        "email": email,
        "password_hash": hash_password(password),
        "name": name
    }).execute()
    if result.data:
        return result.data[0]
    return None


def login_organizer(email: str, password: str) -> str | None:
    result = supabase.table("organizers").select("*").eq("email", email).execute()
    if not result.data:
        return None
    organizer = result.data[0]
    if not verify_password(password, organizer["password_hash"]):
        return None
    return create_session_token(organizer["id"])


def get_current_organizer(token: str) -> dict | None:
    organizer_id = decode_session_token(token)
    if not organizer_id:
        return None
    result = supabase.table("organizers").select("*").eq("id", organizer_id).execute()
    if result.data:
        return result.data[0]
    return None
