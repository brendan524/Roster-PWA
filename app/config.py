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

    # Xero (onshore leave balances, read-only)
    xero_client_id: str = ""
    xero_client_secret: str = ""
    xero_redirect_uri: str = ""  # e.g. https://hr.abgroup.au/api/admin/xero/callback
    xero_sync_hours: float = 6
    auto_clock_minutes: float = 5  # how often auto-clock staff get their rostered punches (0 = off)

    # Email (Google Workspace: smtp.gmail.com, a mailbox like hr@abgroup.au and its app password)
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""  # defaults to smtp_user
    alert_email: str = ""  # system alerts (backups, Xero, roster sheet)
    expense_approvers: str = "AB0001"  # Staff IDs who can approve expense claims (and get the emails)
    availability_email: str = ""  # whoever builds the roster (Dan): staff availability as it comes in
    app_url: str = ""  # e.g. https://hr.abgroup.au, for links in emails
    digest_hour: int = 7  # daily admin digest, local time; -1 = off
    digest_days: str = "mon-fri"  # days the daily check goes out, e.g. "mon-fri" or "mon,wed,fri"
    digest_email: str = ""  # who gets the daily check (comma-separated); empty = every admin with an email
    jobs_enabled: bool = True  # background email/alert jobs (off in tests)

    # Pay report (fortnightly, emailed the Monday after each period ends)
    pay_period_end: str = "2026-10-04"  # any past period's last day (a Sunday); periods repeat every 14 days
    pay_report_email: str = ""  # defaults to ALERT_EMAIL
    pay_report_hour: int = 6  # local time on the Monday; -1 = don't email
    pay_day_offset_days: int = 3  # pay day = period end + this
    office_address: str = ""  # where travel to client sites is measured from
    # Roster Locations containing any of these (case-insensitive) are permanent campuses: gear kept there, no travel paid.
    campus_patterns: str = "TULLAMARINE,BUTLER WAY,HQ,MELTON,GALACTIC,39 COLLINS"
    google_maps_api_key: str = ""  # Routes API, for km/drive time to client sites (optional)
    grand_final_fridays: str = "2026-09-25"  # Vic public holiday, set each year once announced
    extra_public_holidays: str = ""  # any other one-off public holidays, comma-separated YYYY-MM-DD

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
