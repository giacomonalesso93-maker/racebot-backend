"""
database.py — Connessione a Supabase tramite client Python
"""

import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


def create_tables():
    """
    Le tabelle vengono create direttamente su Supabase via SQL Editor.
    Questa funzione è un placeholder — non fa nulla.
    """
    pass


def get_db():
    """Restituisce il client Supabase (compatibilità con main.py)"""
    return supabase
