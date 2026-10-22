"""Static configuration from the environment (.env).

Values a person changes at runtime (sync interval, thresholds, flags) live in the
`app_settings` table; the values here are only their defaults.
"""
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'lienwaiver.db'}"
    data_dir: Path = BASE_DIR / "data"
    app_base_url: str = "http://localhost:8000"
    secret_key: str = "dev-only-change-me"
    log_level: str = "INFO"
    timezone: str = "America/Chicago"

    # Smartsheet (read-only)
    smartsheet_token: str = ""
    smartsheet_api_base: str = "https://api.smartsheet.com/2.0"

    # Google SSO
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_domain: str = "buildoutpros.com"
    # Local development only: skip Google sign-in and act as this user.
    dev_auth_email: str = ""

    # Seed users
    admin_emails: str = "wwestmoreland@buildoutpros.com"
    default_reviewer_email: str = "wwestmoreland@buildoutpros.com"

    # Defaults for runtime settings
    sync_interval_minutes: int = 15
    major_trade_threshold: float = 20000.0
    run_scheduler: bool = True

    # Later phases
    anthropic_api_key: str = ""
    fernet_key: str = ""

    @field_validator("app_base_url")
    @classmethod
    def _normalize_base_url(cls, v: str) -> str:
        """Accept "example.up.railway.app" or a trailing slash; Google needs the exact form."""
        v = v.strip().rstrip("/")
        if v and not v.startswith(("http://", "https://")):
            v = "https://" + v
        return v

    @property
    def admin_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.admin_emails.split(",") if e.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
