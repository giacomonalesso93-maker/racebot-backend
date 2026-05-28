"""
plans.py — Definizione piani e feature gating per RaceBot
"""

PLAN_LABELS = {
    "single":     "Gara Singola",
    "base":       "Stagione Base",
    "pro":        "Stagione Pro",
    "federation": "Federazione",
}

PLAN_COLORS = {
    "single":     "#64748b",
    "base":       "#0891b2",
    "pro":        "#7c3aed",
    "federation": "#b45309",
}

PLAN_ORDER = ["single", "base", "pro", "federation"]

# Features disponibili per piano (ogni piano include quelle del precedente)
_SINGLE = {
    "chatbot", "pdf_upload", "hosted_page", "maps",
    "widget_embed", "basic_stats",
}
_BASE = _SINGLE | {
    "unlimited_races", "gpx_upload", "structured_refreshments",
    "color_customization",
}
_PRO = _BASE | {
    "ticketing", "auto_emails", "realtime_notifications",
    "weather_alerts", "push_communications", "faq_generator",
    "ai_assistant", "advanced_analytics", "csv_export",
    "multilingual", "whatsapp",
}
_FEDERATION = _PRO | {
    "multi_org", "white_label", "api_access", "account_manager",
}

PLAN_FEATURES: dict[str, set] = {
    "single":     _SINGLE,
    "base":       _BASE,
    "pro":        _PRO,
    "federation": _FEDERATION,
}

# Piano minimo richiesto per ciascuna feature (usato nell'UI per i lock)
FEATURE_MIN_PLAN: dict[str, str] = {}
for _plan in PLAN_ORDER:
    for _feat in PLAN_FEATURES[_plan]:
        if _feat not in FEATURE_MIN_PLAN:
            FEATURE_MIN_PLAN[_feat] = _plan

# Max gare per piano (None = illimitato)
PLAN_MAX_RACES: dict[str, int | None] = {
    "single":     1,
    "base":       None,
    "pro":        None,
    "federation": None,
}


def has_feature(organizer: dict, feature: str) -> bool:
    """Restituisce True se l'organizzatore ha accesso alla feature."""
    plan = organizer.get("plan") or "single"
    return feature in PLAN_FEATURES.get(plan, _SINGLE)


def get_features(organizer: dict) -> dict[str, bool]:
    """Restituisce un dict feature→bool per il Jinja2 template."""
    plan = organizer.get("plan") or "single"
    all_features = PLAN_FEATURES.get("federation", _FEDERATION)
    active = PLAN_FEATURES.get(plan, _SINGLE)
    return {f: (f in active) for f in all_features}


def plan_label(plan: str) -> str:
    return PLAN_LABELS.get(plan, plan)


def plan_color(plan: str) -> str:
    return PLAN_COLORS.get(plan, "#64748b")


def required_plan_label(feature: str) -> str:
    """Restituisce il nome del piano minimo richiesto per la feature."""
    return PLAN_LABELS.get(FEATURE_MIN_PLAN.get(feature, "single"), "")
