"""Bridge: load the ACTIVE scoring_config for engine callers (Doc 8 §5 —
the caller loads cfg = active_scoring_config(); every persisted artefact
stores scoring_config_version_id)."""


def active_scoring_config():
    """Returns (cfg_dict, scoring_config_id). Falls back to the packaged
    default v1 when no DB row is active (fresh install / tests)."""
    try:
        from fundos.config.models import ScoringConfig
        row = ScoringConfig.active()
        if row:
            return row.data, row.id
    except Exception:
        pass
    from fundos.engines.default_config import DEFAULT_SCORING_CONFIG_V1
    return DEFAULT_SCORING_CONFIG_V1, None
