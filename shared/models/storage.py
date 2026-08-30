from __future__ import annotations

import os
from pathlib import Path

from .persistence import PersistenceConfigurationError, PersistenceStore
from .store import LocalStore


def normalize_storage_mode(mode: str | None = None) -> str:
    return (mode or os.environ.get("STORAGE_MODE", "local")).strip().lower()


def create_persistence_store(mode: str | None = None, path: str | Path | None = None) -> PersistenceStore:
    selected_mode = normalize_storage_mode(mode)
    if selected_mode == "local":
        return LocalStore(path or "local_data/state.json")
    if selected_mode == "cloud":
        raise PersistenceConfigurationError(
            "STORAGE_MODE=cloud is reserved for the future Firestore/Cloud Storage backend and is not enabled in Phase 1A."
        )
    raise PersistenceConfigurationError(
        f"Unsupported STORAGE_MODE={selected_mode!r}. Supported mode in Phase 1A: local."
    )
