"""Central configuration via pydantic-settings (reads .env automatically)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Insforge / PostgREST
    insforge_api_url: str = "https://placeholder.us-east.insforge.app"
    insforge_service_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""

    # Storage
    insforge_storage_bucket: str = "pipeline-docs"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # LLM model choices
    llm_matching_model: str = "claude-opus-4-5"
    llm_generation_model: str = "claude-haiku-4-5-20251001"
    llm_verification_model: str = "claude-opus-4-5"

    # Retrieval
    bm25_top_k: int = 15

    # Minimum extracted span count before we consider extraction successful
    min_span_count: int = 10


settings = Settings()
