from __future__ import annotations

import os
from pathlib import Path

from .cloud_store import CloudStore, CloudStoreConfig
from .persistence import PersistenceConfigurationError, PersistenceStore
from .store import LocalStore


def normalize_storage_mode(mode: str | None = None) -> str:
    return (mode or os.environ.get("STORAGE_MODE", "local")).strip().lower()


def create_persistence_store(mode: str | None = None, path: str | Path | None = None) -> PersistenceStore:
    selected_mode = normalize_storage_mode(mode)
    if selected_mode == "local":
        return LocalStore(path or "local_data/state.json")
    if selected_mode == "cloud":
        return CloudStore(CloudStoreConfig.from_env())
    raise PersistenceConfigurationError(
        f"Unsupported STORAGE_MODE={selected_mode!r}. Supported modes: local, cloud."
    )
