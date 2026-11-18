"""Settings read from .env (same pattern as approve-helper's core/config.py)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    gemini_api_key: str = ""
    # gemini-3.8-live is the recommended Live model (sync + async function
    # calling). Fallback: gemini-2.5-flash-native-audio-preview-12-2025.
    gemini_live_model: str = "gemini-3.8-live"
    gemini_voice: str = "Kore"

    # Hard cap on one call, so a forgotten tab can't keep billing.
    max_session_seconds: int = 480

    rules_path: Path = ROOT / "rules" / "report_rules.yaml"
    jobs_path: Path = ROOT / "data" / "jobs.json"
    reports_dir: Path = ROOT / "data" / "reports"
    drafts_dir: Path = ROOT / "data" / "drafts"

    # USD per 1M tokens (Gemini Live, paid tier, ai.google.dev pricing
    # 2026-11). Only used to show an estimate in the UI.
    price_audio_in: float = 3.00
    price_text_in: float = 0.75
    price_audio_out: float = 12.00
    price_text_out: float = 4.50


settings = Settings()
