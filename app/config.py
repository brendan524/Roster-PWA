"""App settings, read from environment variables (see .env.example).

Secrets (DB password, PIN pepper, Google key) come from the environment /
mounted files only — never hard-code them here.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    database_url: str = "postgresql+psycopg://roster:roster@localhost:5432/roster"
    pin_pepper: str = ""  # secret key for HMAC-hashing PINs. Must be set; never change after PINs are issued.
    cookie_secure: bool = True  # False only for local http development
    session_days: int = 30
    timezone: str = "Australia/Melbourne"  # used to decide "current month" + "today"

    # Login protection
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # PINs (clock-in credential, used from Stage 2)
    pin_length: int = 6

    # Roster source
    roster_source: Literal["google", "csv"] = "google"
    roster_sheet_id: str = ""
    google_service_account_file: str = "/run/secrets/google-service-account.json"
    roster_csv_dir: str = "./sample-roster"  # used when roster_source=csv (local dev / demo)
    roster_cache_seconds: int = 120
    roster_min_refresh_seconds: int = 20  # forced refreshes are throttled to this


@lru_cache
def get_settings() -> Settings:
    return Settings()
