from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql+asyncpg://solvyn:solvyn@localhost:5432/solvyn"
    redis_url: str = "redis://localhost:6379/0"
    sandbox_workspace_root: str = ".solvyn/workspaces"
    sandbox_image: str = "solvyn-sandbox:local"
    sandbox_runtime_url: str = ""
    sandbox_runtime_token: str = ""
    enable_workflow_scheduler: bool = True

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

    # --- Auth / identity ---
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30
    allow_public_registration: bool = False
    bootstrap_token: str = ""
    # Comma-separated Fernet keys; first key encrypts new credentials and all listed keys can decrypt.
    integration_encryption_keys: str = ""
    github_webhook_secret: str = ""
    # Local dev only: allows booting without real secrets.
    # Must remain False in any networked deployment.
    allow_insecure_dev: bool = False

    # --- DB pool tuning (Phase 0 audit: connection-storm crash risk) ---
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_recycle_seconds: int = 1800

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

    @model_validator(mode="after")
    def _require_secrets_in_prod(self) -> "Settings":
        """Fail fast in production when auth secrets are missing (P0-002).

        Imports of this module happen during pytest collection before any
        fixture runs, so the check is *deferred*: `Settings()` never raises
        on import. `enforce_prod_secrets()` — called from the FastAPI
        lifespan in `main.py` — raises `RuntimeError` at boot time when the
        server starts without secrets and ALLOW_INSECURE_DEV is false.
        Unit tests construct `Settings(api_key_secret=..., ...)` directly so
        they opt out of the import-time default via explicit arguments.
        """
        return self


settings = Settings()


def enforce_prod_secrets() -> None:
    """Raise at server boot if secrets are absent (P0-002).

    Call this from the FastAPI lifespan before accepting traffic. Tests that
    do not exercise the lifespan are unaffected.
    """
    if settings.allow_insecure_dev:
        return
    missing = []
    if not settings.api_key_secret:
        missing.append("API_KEY_SECRET")
    if not settings.jwt_secret:
        missing.append("JWT_SECRET")
    if settings.sandbox_runtime_url and not settings.sandbox_runtime_token:
        missing.append("SANDBOX_RUNTIME_TOKEN")
    if missing:
        raise RuntimeError(
            f"Refusing to start: missing required secrets {', '.join(missing)}. "
            "Set them in the environment, or set ALLOW_INSECURE_DEV=true only "
            "for local development."
        )
