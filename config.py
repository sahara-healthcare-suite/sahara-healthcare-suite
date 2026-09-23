import os
from typing import Optional


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def get_optional_env(name: str) -> Optional[str]:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def get_allowed_origins() -> list[str]:
    raw = os.getenv(
        "ALLOWED_ORIGINS",
        "https://sahara-healthcare-suite.pages.dev,https://sahara-healthcare-suite-1.pages.dev,http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000",
    )
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


INTRON_API_KEY = get_optional_env("INTRON_API_KEY")
OPENAI_API_KEY = get_optional_env("OPENAI_API_KEY")
ANTHROPIC_API_KEY = get_optional_env("ANTHROPIC_API_KEY")
ALLOWED_ORIGINS = get_allowed_origins()
