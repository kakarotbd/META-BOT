"""Shared, non-secret application metadata loaded by both GUI and Railway."""
from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "bot_app_config.txt"

# Load root environment variables before either entry point creates services.
# host platform variables take precedence because dotenv does not override them.
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class AppConfig:
    bot_app_name: str
    bot_app_tagline: str
    logo_path: Path
    admin_user_id: int
    allowed_guild_id: int


def load_app_config(config_path=CONFIG_PATH):
    """Load the small key=value app metadata file with clear startup errors."""
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required app configuration file is missing: {config_path}") from exc

    values = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"Invalid app configuration on line {number}: use KEY=VALUE.")
        key, value = (part.strip() for part in line.split("=", 1))
        if not key or not value:
            raise RuntimeError(f"Invalid app configuration on line {number}: key and value are required.")
        values[key] = value

    required = ("BOT_APP_NAME", "BOT_APP_TAGLINE", "LOGO_PATH")
    missing = [key for key in required if key not in values]
    if missing:
        raise RuntimeError("Missing required app configuration value(s): " + ", ".join(missing))

    admin_user_id_text = os.getenv("ADMIN_USER_ID", "").strip()
    try:
        admin_user_id = int(admin_user_id_text)
    except ValueError as exc:
        raise RuntimeError("ADMIN_USER_ID environment variable must be a numeric Discord ID.") from exc
    if admin_user_id <= 0:
        raise RuntimeError("ADMIN_USER_ID environment variable must be a positive Discord ID.")

    allowed_guild_id_text = os.getenv("ALLOWED_GUILD_ID", "").strip()
    try:
        allowed_guild_id = int(allowed_guild_id_text)
    except ValueError as exc:
        raise RuntimeError("ALLOWED_GUILD_ID environment variable must be a numeric Discord server ID.") from exc
    if allowed_guild_id <= 0:
        raise RuntimeError("ALLOWED_GUILD_ID environment variable must be a positive Discord ID.")

    logo_path = (PROJECT_ROOT / values["LOGO_PATH"]).resolve()
    if PROJECT_ROOT not in logo_path.parents:
        raise RuntimeError("LOGO_PATH must stay inside this project folder.")
    return AppConfig(values["BOT_APP_NAME"], values["BOT_APP_TAGLINE"], logo_path, admin_user_id, allowed_guild_id)


APP_CONFIG = load_app_config()
