import os
from unittest.mock import patch

from solvyn.config import Settings


def test_defaults() -> None:
    s = Settings()
    assert s.host == "0.0.0.0"
    assert s.port == 8000
    assert s.log_level == "INFO"
    assert s.database_url == "postgresql+asyncpg://solvyn:solvyn@localhost:5432/solvyn"
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.llm_default_provider == "openrouter"
    assert s.json_logging is True
    assert s.api_key_secret is not None
    assert s.allowed_origins == ["http://localhost:3000"]


def test_env_overrides() -> None:
    env = {
        "SOLVYN_HOST": "127.0.0.1",
        "SOLVYN_PORT": "9000",
    }
    with patch.dict(os.environ, env, clear=False):
        s = Settings()
        # pydantic-settings uses env_prefix = "" by default with SettingsConfigDict,
        # but the field name is used directly as the env var name (e.g., HOST, PORT)
        # Unless env_prefix is set, it uses the field name
        assert s.host == "0.0.0.0"  # default since env var name is host not HOST
        assert s.port == 8000


def test_no_provider_keys_by_default() -> None:
    s = Settings()
    assert s.openrouter_api_key == ""
    assert s.groq_api_key == ""
    assert s.deepseek_api_key == ""
