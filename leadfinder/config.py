"""Configuration from `.env` plus the editable YAML/text files in the project folder.

Paths are always built with pathlib so they work on Windows. The project "home" is the
folder that holds `.env`, `leads.db`, `trades.yaml` etc. It defaults to the repository root
(where `pip install -e .` was run) and can be overridden with the LEADFINDER_HOME variable.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
STATIC_DIR = REPO_ROOT / "static"
UI_TEMPLATES_DIR = PACKAGE_DIR / "web" / "templates"

SECRET_FIELDS = {
    "GOOGLE_API_KEY",
    "COMPANIES_HOUSE_API_KEY",
    "SMTP_PASSWORD",
    "NETLIFY_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "SECRET_KEY",
}

# Keys shown (set / not set, never the value) on the Settings page.
CHECKED_KEYS = [
    "GOOGLE_API_KEY",
    "COMPANIES_HOUSE_API_KEY",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "FROM_NAME",
    "FROM_EMAIL",
    "DAILY_SEND_LIMIT",
    "MY_NAME",
    "MY_PHONE",
    "BUSINESS_ADDRESS",
    "BACKUP_DIR",
    "NETLIFY_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "AI_MODEL",
    "FREE_TIER_MODE",
    "BIND_HOST",
    "SECRET_KEY",
]


def default_home() -> Path:
    env = os.environ.get("LEADFINDER_HOME")
    return Path(env).expanduser().resolve() if env else REPO_ROOT


def _bool(v: Any, default: bool) -> bool:
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(v: Any, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _float(v: Any, default: float) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    home: Path
    google_api_key: str = ""
    companies_house_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    from_name: str = "Vibora"
    from_email: str = ""
    daily_send_limit: int = 30
    my_name: str = ""
    my_phone: str = ""
    business_address: str = ""
    backup_dir_raw: str = ""
    netlify_auth_token: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)
    ai_model: str = "claude-haiku-5-5"
    ai_max_per_search: int = 30
    free_tier_mode: bool = True
    free_tier_safety: float = 0.9
    monthly_api_budget_gbp: float = 20.0
    bind_host: str = "127.0.0.1"
    secret_key: str = field(default="", repr=False)
    raw: dict[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:  # never leak secrets in logs or tracebacks
        return f"Settings(home={self.home!s})"

    # ---- paths -----------------------------------------------------------
    @property
    def env_path(self) -> Path:
        return self.home / ".env"

    @property
    def db_path(self) -> Path:
        return self.home / "leads.db"

    @property
    def backup_dir(self) -> Path:
        if self.backup_dir_raw:
            return Path(self.backup_dir_raw).expanduser()
        return self.home / "backups"

    @property
    def backup_dir_is_default(self) -> bool:
        return not self.backup_dir_raw

    @property
    def previews_dir(self) -> Path:
        return self.home / "previews"

    @property
    def templates_dir(self) -> Path:
        return self.home / "templates"

    @property
    def cli_state_path(self) -> Path:
        return self.home / ".leadfinder-user.json"

    def config_file(self, name: str) -> Path:
        """User-editable config files live in home; fall back to the copies shipped in the repo."""
        p = self.home / name
        if p.exists():
            return p
        return REPO_ROOT / name

    def template_file(self, name: str) -> Path:
        p = self.templates_dir / name
        if p.exists():
            return p
        return REPO_ROOT / "templates" / name

    # ---- feature flags ---------------------------------------------------
    @property
    def ai_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def companies_house_enabled(self) -> bool:
        return bool(self.companies_house_api_key)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.from_email)

    def key_status(self) -> list[tuple[str, bool]]:
        return [(k, bool(self.raw.get(k, "").strip())) for k in CHECKED_KEYS]


def load_settings(home: Path | None = None, generate_secret: bool = True) -> Settings:
    home = Path(home) if home else default_home()
    env_path = home / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        values = {k: (v or "") for k, v in dotenv_values(env_path, encoding="utf-8").items()}
    # Real environment variables win (handy for CI); only for known keys.
    for k in list(CHECKED_KEYS) + ["AI_MAX_PER_SEARCH", "FREE_TIER_SAFETY", "MONTHLY_API_BUDGET_GBP"]:
        if os.environ.get(k):
            values[k] = os.environ[k]

    if generate_secret and not values.get("SECRET_KEY", "").strip():
        values["SECRET_KEY"] = ensure_secret_key(env_path)

    g = lambda k, d="": (values.get(k) or d).strip()  # noqa: E731
    return Settings(
        home=home,
        google_api_key=g("GOOGLE_API_KEY"),
        companies_house_api_key=g("COMPANIES_HOUSE_API_KEY"),
        smtp_host=g("SMTP_HOST"),
        smtp_port=_int(values.get("SMTP_PORT"), 587),
        smtp_user=g("SMTP_USER"),
        smtp_password=values.get("SMTP_PASSWORD", "") or "",
        from_name=g("FROM_NAME", "Vibora"),
        from_email=g("FROM_EMAIL"),
        daily_send_limit=_int(values.get("DAILY_SEND_LIMIT"), 30),
        my_name=g("MY_NAME"),
        my_phone=g("MY_PHONE"),
        business_address=g("BUSINESS_ADDRESS"),
        backup_dir_raw=g("BACKUP_DIR"),
        netlify_auth_token=g("NETLIFY_AUTH_TOKEN"),
        anthropic_api_key=g("ANTHROPIC_API_KEY"),
        ai_model=g("AI_MODEL", "claude-haiku-5-5"),
        ai_max_per_search=_int(values.get("AI_MAX_PER_SEARCH"), 30),
        free_tier_mode=_bool(values.get("FREE_TIER_MODE"), True),
        free_tier_safety=min(1.0, max(0.1, _float(values.get("FREE_TIER_SAFETY"), 0.9))),
        monthly_api_budget_gbp=_float(values.get("MONTHLY_API_BUDGET_GBP"), 20.0),
        bind_host=g("BIND_HOST", "127.0.0.1"),
        secret_key=g("SECRET_KEY"),
        raw={k: str(v) for k, v in values.items()},
    )


def ensure_secret_key(env_path: Path) -> str:
    """Generate a random SECRET_KEY and write it into .env (creating .env if needed)."""
    key = secrets.token_urlsafe(48)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith("SECRET_KEY="):
            lines[i] = f"SECRET_KEY={key}"
            replaced = True
    if not replaced:
        lines.append(f"SECRET_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return key


# ---------------------------------------------------------------------------
# Editable config files
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)


def load_free_tier(settings: Settings) -> dict[str, Any]:
    return load_yaml(settings.config_file("free_tier.yaml"))


def load_website_rules(settings: Settings) -> dict[str, Any]:
    return load_yaml(settings.config_file("website_rules.yaml"))


def load_towns(settings: Settings) -> list[dict[str, Any]]:
    data = load_yaml(settings.config_file("towns.yaml"))
    return list(data.get("towns", []))


def load_chains(settings: Settings) -> list[str]:
    path = settings.config_file("chains.txt")
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out
