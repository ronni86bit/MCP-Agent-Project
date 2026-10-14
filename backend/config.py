"""Application settings, loaded from environment / .env (see .env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = "INFO"
    mcp_call_timeout_seconds: float = 20.0

    # Phase 3 — LLM via Ollama's OpenAI-compatible endpoint.
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b-instruct"
    llm_temperature: float = 0.1
    # CPU-class local models process prompts slowly (~3s/token-prompt measured
    # on the dev machine); synthesis-sized calls need generous timeouts.
    llm_timeout_seconds: float = 600.0

    # Phase 6 — reliability knobs (all bounded; nothing retries indefinitely).
    llm_retry_attempts: int = 1          # EXTRA attempts for transient provider errors
    llm_retry_backoff_seconds: float = 2.0
    circuit_failure_threshold: int = 3   # consecutive availability failures -> open
    circuit_cooldown_seconds: float = 60.0
    rate_limit_per_server_per_minute: int = 240

    # Investigation loop bounds — the loop is never unbounded.
    max_investigation_steps: int = 8
    max_plan_steps: int = 4
    max_plan_revisions: int = 2
    max_llm_calls: int = 16
    max_investigation_seconds: float = 1200.0

    # Phase 2+ — Postgres (business data + agent state + audit).
    database_url: str = ""
    # DSN the postgres-mcp server connects with: a dedicated READ-ONLY role.
    postgres_mcp_dsn: str = ""
    # Admin DSN used only by scripts (schema setup + seeding), never by the MCP server.
    ops_admin_dsn: str = ""

    # Phase 4 — fine-grained PAT, read-only scopes only. Optional for public repos.
    github_token: str = ""
    # Repository github-mcp reads (owner/name).
    github_repo: str = "ronni86bit/MCP-Agent-Project"


@lru_cache
def get_settings() -> Settings:
    return Settings()
