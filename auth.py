"""
auth.py — Gestione login, registrazione e sessioni tramite Supabase

Sicurezza:
- Password: scrypt con salt casuale (stdlib, nessuna dipendenza).
  Gli hash legacy SHA-256 vengono verificati e migrati automaticamente al login.
- Sessioni: token firmati HMAC-SHA256 con SECRET_KEY — non falsificabili.
"""

import os
import uuid
import hashlib
import hmac
import base64
import secrets
from dotenv import load_dotenv
from database import supabase

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY mancante nel .env — necessaria per firmare le sessioni")

# ─── PASSWORD ────────────────────────────────────────────────

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    """Hash scrypt con salt casuale. Formato: scrypt$<salt_hex>$<hash_hex>"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    if password_hash.startswith("scrypt$"):
        try:
            _, salt_hex, digest_hex = password_hash.split("$")
            digest = hashlib.scrypt(
                password.encode(), salt=bytes.fromhex(salt_hex),
                n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
            )
            return hmac.compare_digest(digest.hex(), digest_hex)
        except Exception:
            return False
    # Legacy: SHA-256 senza salt (account creati prima della migrazione)
    legacy = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(legacy, password_hash)


def _is_legacy_hash(password_hash: str) -> bool:
    return not password_hash.startswith("scrypt$")


# ─── SESSIONI (token firmati) ────────────────────────────────

def _sign(data: str) -> str:
    return hmac.new(SECRET_KEY.encode(), data.encode(), hashlib.sha256).hexdigest()


def create_session_token(organizer_id: str) -> str:
    payload = base64.urlsafe_b64encode(organizer_id.encode()).decode()
    return f"{payload}.{_sign(payload)}"


def decode_session_token(token: str) -> str | None:
    try:
        payload, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload), signature):
            return None
        return base64.urlsafe_b64decode(payload.encode()).decode()
    except Exception:
        return None


# ─── ORGANIZZATORI ───────────────────────────────────────────

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
    # Migrazione automatica: aggiorna gli hash legacy al primo login riuscito
    if _is_legacy_hash(organizer["password_hash"]):
        supabase.table("organizers").update(
            {"password_hash": hash_password(password)}
        ).eq("id", organizer["id"]).execute()
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
