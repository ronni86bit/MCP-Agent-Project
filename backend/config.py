"""Application settings, loaded from environment / .env (see .env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"
    mcp_call_timeout_seconds: float = 20.0

    # Phase 3 — LLM via Ollama's OpenAI-compatible endpoint.
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = ""

    # Phase 2+ — Postgres (business data + agent state + audit).
    database_url: str = ""
    # DSN the postgres-mcp server connects with: a dedicated READ-ONLY role.
    postgres_mcp_dsn: str = ""
    # Admin DSN used only by scripts (schema setup + seeding), never by the MCP server.
    ops_admin_dsn: str = ""

    # Phase 4 — fine-grained PAT, read-only scopes only.
    github_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
