"""
Centralized LLM model configuration with global override support.

Environment variables:
    LLM_PRIMARY_OVERRIDE - If set, used as primary model for ALL stages
    LLM_FALLBACK_OVERRIDE - If set, used as fallback model for ALL stages
    
Stage-specific variables (used when override not set):
    EXTRACT_NOM_MODEL / EXTRACT_NOM_FALLBACK_MODEL
    DIRECTOR_ASSESS_PRIMARY_MODEL / DIRECTOR_ASSESS_FALLBACK_MODEL
    REM_ASSESS_PRIMARY_MODEL / REM_ASSESS_FALLBACK_MODEL
    OTHER_RESOLUTIONS_PRIMARY_MODEL / OTHER_RESOLUTIONS_FALLBACK_MODEL
    PERFORMANCE_MODEL / PERFORMANCE_FALLBACK_MODEL
    AUDIT_PRIMARY_MODEL / AUDIT_FALLBACK_MODEL
    REMEDIATION_PRIMARY_MODEL / REMEDIATION_FALLBACK_MODEL
    TEXT_SUMMARY_MODEL / TEXT_SUMMARY_FALLBACK_MODEL
    BOARD_QUESTIONS_MODEL / BOARD_QUESTIONS_FALLBACK_MODEL
"""

import os


def get_model(stage_primary: str, stage_fallback: str = "") -> tuple[str, str]:
    """
    Get the (primary, fallback) model pair for a stage.

    Args:
        stage_primary: Stage-specific primary model env var name
        stage_fallback: Stage-specific fallback model env var name (optional)

    Returns:
        Tuple of (primary_model, fallback_model)

    Reads the override lazily (at call time) rather than at import time, so a
    stage module that calls load_dotenv() after importing this module still sees
    the override - the previous import-time caching silently ignored it.
    """
    primary = os.getenv("LLM_PRIMARY_OVERRIDE") or os.getenv(stage_primary, "stealth/ox-alpha")

    fallback = os.getenv("LLM_FALLBACK_OVERRIDE")
    if not fallback and stage_fallback:
        fallback = os.getenv(stage_fallback, "")
    if fallback is None:
        fallback = ""

    return primary, fallback


def get_single_model(stage_var: str) -> str:
    """Get single model for stages that only use one model."""
    return os.getenv("LLM_PRIMARY_OVERRIDE") or os.getenv(stage_var, "stealth/ox-alpha")


# Stage-specific model getters
def extract_nom_models() -> tuple[str, str]:
    return get_model("EXTRACT_NOM_MODEL", "EXTRACT_NOM_FALLBACK_MODEL")


def director_assess_models() -> tuple[str, str]:
    return get_model("DIRECTOR_ASSESS_PRIMARY_MODEL", "DIRECTOR_ASSESS_FALLBACK_MODEL")


def rem_assess_models() -> tuple[str, str]:
    return get_model("REM_ASSESS_PRIMARY_MODEL", "REM_ASSESS_FALLBACK_MODEL")


def other_resolutions_models() -> tuple[str, str]:
    return get_model("OTHER_RESOLUTIONS_PRIMARY_MODEL", "OTHER_RESOLUTIONS_FALLBACK_MODEL")


def performance_models() -> tuple[str, str]:
    return get_model("PERFORMANCE_MODEL", "PERFORMANCE_FALLBACK_MODEL")


def audit_models() -> tuple[str, str]:
    return get_model("AUDIT_PRIMARY_MODEL", "AUDIT_FALLBACK_MODEL")


def remediation_models() -> tuple[str, str]:
    return get_model("REMEDIATION_PRIMARY_MODEL", "REMEDIATION_FALLBACK_MODEL")


def text_summary_models() -> tuple[str, str]:
    return get_model("TEXT_SUMMARY_MODEL", "TEXT_SUMMARY_FALLBACK_MODEL")


def board_questions_models() -> tuple[str, str]:
    return get_model("BOARD_QUESTIONS_MODEL", "BOARD_QUESTIONS_FALLBACK_MODEL")


def report_rendering_models() -> tuple[str, str]:
    return get_model("REPORT_RENDERING_MODEL", "REPORT_RENDERING_FALLBACK_MODEL")


# Single-model stages
def news_prefetch_model() -> str:
    return get_single_model("NEWS_PREFETCH_MODEL")