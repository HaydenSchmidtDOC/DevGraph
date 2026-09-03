"""Security-first default settings.

Every default here is deliberately the safe/off value per the Design Brief
(Principle 2 — local-first, no cloud dependencies, no telemetry by default).
Nothing in this module should silently enable outbound network calls.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEVGRAPH_", env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "devgraph-local-dev"

    telemetry_enabled: bool = False
    allow_cross_repo: bool = False
    cloud_sync: bool = False
    enable_run_cypher: bool = False
    git_recency_track_author: bool = False

    mentions_ambiguous_mode: str = "all"
    registry_db_path: Path = Path.home() / ".devgraph" / "registry.sqlite3"
    watch_debounce_ms: int = 500
    health_check_interval_s: int = 30

    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765


@lru_cache
def get_settings() -> Settings:
    return Settings()
