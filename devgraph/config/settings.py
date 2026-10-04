"""Security-first default settings.

Every default here is deliberately the safe/off value per the Design Brief
(Principle 2 — local-first, no cloud dependencies, no telemetry by default).
Nothing in this module should silently enable outbound network calls.
"""

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def devgraph_home() -> Path:
    """Directory holding DevGraph's per-user state (registry, logs, .env).

    Follows an exported DEVGRAPH_REGISTRY_DB_PATH so the home moves with the
    registry; otherwise ~/.devgraph. ``~`` is expanded the same way as for the
    registry_db_path setting. A value that is still relative after expansion
    is ignored, since its parent would be the working directory.
    """
    registry = os.environ.get("DEVGRAPH_REGISTRY_DB_PATH")
    if registry:
        path = Path(registry).expanduser()
        if path.is_absolute():
            return path.parent
        logger.warning(
            "Ignoring relative DEVGRAPH_REGISTRY_DB_PATH %r when locating the settings home; using ~/.devgraph",
            registry,
        )
    return Path.home() / ".devgraph"


def env_files() -> tuple[Path, ...]:
    """The .env files Settings reads, lowest precedence first.

    Only fixed locations are used, never the working directory: the DevGraph
    checkout root (when running from a source checkout) and the DevGraph
    home, which wins over the checkout. Exported environment variables win
    over both.
    """
    files: list[Path] = []
    checkout_root = Path(__file__).resolve().parents[2]
    if (checkout_root / "pyproject.toml").is_file():
        files.append(checkout_root / ".env")
    files.append(devgraph_home() / ".env")
    return tuple(files)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEVGRAPH_", extra="ignore")

    def __init__(self, **values) -> None:
        values.setdefault("_env_file", env_files())
        super().__init__(**values)

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

    @field_validator("registry_db_path")
    @classmethod
    def _expand_registry_home(cls, value: Path) -> Path:
        return value.expanduser()

    watch_debounce_ms: int = 500
    health_check_interval_s: int = 30

    log_file: Path | None = None

    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8765


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.log_file is None:
        s.log_file = Path.home() / ".devgraph" / "devgraph.log"
    return s
