"""
Feature flags (Doc 6 §2) — a thin functions-module over AppConfiguration.

Each flag is a def flag_name() -> bool reading the config row with a safe
default, so callers never touch the model directly and a missing row never
crashes a request. Every optional behaviour in Docs 1–5 keyed by a toggle
goes through a flag function, never a raw settings read.
"""


def _config():
    from fundos.config.models import AppConfiguration
    return AppConfiguration.objects.first()   # never cache at module import


def get_flag(name: str, default=False):
    """Generic accessor for flags that have no dedicated function yet.

    Reads AppConfiguration.<name> when the column exists, else the free-form
    JSON blob, else `default`. Never raises — a missing config row or column
    simply yields the default.

    The blob is `AppConfiguration.features`. This used to look for
    `feature_flags`, a field that does not exist on the model, so the JSON
    branch was unreachable and every flag without its own column silently
    returned its default no matter what an administrator set. `feature_flags`
    is still accepted first, in case an installation added such a column.
    """
    cfg = _config()
    if cfg is None:
        return default
    if hasattr(cfg, name):
        value = getattr(cfg, name)
        if value is not None:
            return value
    for attr in ("feature_flags", "features"):
        blob = getattr(cfg, attr, None)
        if isinstance(blob, dict) and name in blob:
            return blob[name]
    return default


def ai_mocked() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "ai_mocked", True))


def show_numeric_readiness() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "show_numeric_readiness", False))


def enable_research_extensions() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "enable_research_extensions", False))


def enable_instrument_selection() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "enable_instrument_selection", True))


def enable_dilution_simulator() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "enable_dilution_simulator", True))


def whatsapp_channel_enabled() -> bool:
    """True iff phone-number-id + token are present (Doc 6 §2)."""
    cfg = _config()
    if not cfg:
        return False
    return bool(cfg.whatsapp_phone_number_id and cfg.whatsapp_access_token_value)


def ai_synthetic_disclosure_enabled() -> bool:
    cfg = _config()
    return bool(getattr(cfg, "ai_synthetic_disclosure_enabled", True))


def mandatory_ckb_fields() -> list:
    cfg = _config()
    fields = getattr(cfg, "mandatory_ckb_fields", None) or []
    if fields:
        return list(fields)
    # Safe default (Stage 1 BRD core set).
    return ["description", "sector", "business_model", "revenue_model",
            "arr", "burn", "runway_months", "customers", "founders",
            "cap_table", "incorporation"]
