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
    # All roster sheet times are in this zone (offshore shifts included). Also decides "today".
    timezone: str = "Australia/Melbourne"
    # Offshore staff also see each shift converted to their local time.
    offshore_timezone: str = "Asia/Manila"
    offshore_timezone_label: str = "Philippines time"

    # Login protection
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # PINs (clock-in credential)
    pin_length: int = 6
    max_pin_failures: int = 5
    personal_pin_lock_minutes: int = 15
    kiosk_pin_lock_seconds: int = 60

    # Clock in/out
    late_grace_minutes: int = 10  # later than this after shift start = flagged late
    early_grace_minutes: int = 10  # earlier than this before shift end = flagged early finish
    duplicate_window_seconds: int = 60  # same person, same action, within this = one punch
    # Kiosk locations offered at setup: "Name:onshore|offshore", comma-separated.
    kiosk_locations: str = "AB HQ:onshore,WrkPod:offshore"

    # Off-server backup copies (Google Shared Drive folder or drive ID); empty = off
    backup_drive_folder_id: str = ""
    backup_drive_keep_days: int = 90

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
