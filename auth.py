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
        "name": name,
        "status": "pending"
    }).execute()
    if result.data:
        return result.data[0]
    return None


def login_organizer(email: str, password: str) -> tuple[str | None, str | None]:
    """
    Restituisce (token, errore).
    errore può essere: None (ok), 'wrong_credentials', 'pending', 'suspended'
    """
    result = supabase.table("organizers").select("*").eq("email", email).execute()
    if not result.data:
        return None, "wrong_credentials"
    organizer = result.data[0]
    if not verify_password(password, organizer["password_hash"]):
        return None, "wrong_credentials"
    status = organizer.get("status", "active")
    if status == "pending":
        return None, "pending"
    if status == "suspended":
        return None, "suspended"
    return create_session_token(organizer["id"]), None


def get_current_organizer(token: str) -> dict | None:
    organizer_id = decode_session_token(token)
    if not organizer_id:
        return None
    result = supabase.table("organizers").select("*").eq("id", organizer_id).execute()
    if result.data:
        org = result.data[0]
        if org.get("status") in ("pending", "suspended"):
            return None
        return org
    return None
