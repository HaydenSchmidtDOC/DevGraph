"""Security-first default settings.

Every default here is deliberately the safe/off value per the Design Brief
(Principle 2 — local-first, no cloud dependencies, no telemetry by default).
Nothing in this module should silently enable outbound network calls.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEVGRAPH_", env_file=".env", extra="ignore")

    neo4j_uri: str = "bolt://127.0.0.1:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "devgraph-local-dev"
    # Neo4j's data directory as this process can see it (a read-only mount
    # of the data volume, or the volume's host path). Optional: without it
    # the dashboard reports store size as unavailable.
    neo4j_data_dir: Path | None = None

    telemetry_enabled: bool = False
    allow_cross_repo: bool = False
    cloud_sync: bool = False
    enable_run_cypher: bool = False
    git_recency_track_author: bool = False

    mentions_ambiguous_mode: str = "all"
    registry_db_path: Path = Path.home() / ".devgraph" / "registry.sqlite3"
    watch_debounce_ms: int = 500
    health_check_interval_s: int = 30

    log_file: Path | None = None

    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765

    @field_validator("neo4j_data_dir", mode="before")
    @classmethod
    def _blank_data_dir_is_unset(cls, value: object) -> object:
        # Path("") is the current directory, which would read as an
        # "unreadable" data dir rather than an unset one.
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.log_file is None:
        s.log_file = Path.home() / ".devgraph" / "devgraph.log"
    return s
