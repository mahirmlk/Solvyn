from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+asyncpg://solvyn:solvyn@localhost:5432/solvyn"
    redis_url: str = "redis://localhost:6379/0"

    openrouter_api_key: str = ""
    groq_api_key: str = ""
    deepseek_api_key: str = ""

    llm_default_provider: str = "openrouter"
    llm_default_model: str = "openai/gpt-4o"

    default_tenant_id: str = "default"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    json_logging: bool = True
    api_key_secret: str = "change-me-to-a-random-secret"
    allowed_origins: list[str] = ["http://localhost:3000"]


settings = Settings()

if settings.api_key_secret == "change-me-to-a-random-secret":
    import logging

    logging.getLogger(__name__).warning(
        "SECURITY WARNING: The default api_key_secret is being used. "
        "Set a strong, random secret via the API_KEY_SECRET environment variable."
    )
