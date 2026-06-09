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
    llm_cache_ttl: int = 3600

    default_tenant_id: str = "default"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    json_logging: bool = True
    api_key_secret: str = ""
    allowed_origins: list[str] = ["http://localhost:3000"]

    rate_limit_per_ip: int = 100
    rate_limit_per_tenant: int = 500
    rate_limit_window: int = 60

    cost_budget_per_tenant: float = 100.0
    cost_alert_threshold: float = 0.8

    memory_ttl_days: int = 90

    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    otel_enabled: bool = False
    otlp_endpoint: str | None = None

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


settings = Settings()

if not settings.api_key_secret:
    import logging

    logging.getLogger(__name__).critical(
        "SECURITY: API_KEY_SECRET environment variable is not set. "
        "The server will start but authentication will reject all requests. "
        "Set a strong, random secret via the API_KEY_SECRET environment variable."
    )
