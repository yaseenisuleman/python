"""Object store abstraction with a local filesystem implementation."""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class ObjectStore(ABC):
    """Abstract object store interface."""

    @abstractmethod
    def put(self, key: str, local_path: str | Path) -> str:
        """Upload or copy a local file to the store."""

    @abstractmethod
    def get(self, key: str, local_path: str | Path) -> Path:
        """Download or copy a file from the store to a local path."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Return True if the object exists."""


class LocalObjectStore(ObjectStore):
    """Filesystem-backed object store for local development and temp directories."""

    def __init__(self, base_path: str | Path) -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        path = (self.base_path / key).resolve()
        if not str(path).startswith(str(self.base_path.resolve())):
            raise ValueError(f"Invalid key: {key}")
        return path

    def put(self, key: str, local_path: str | Path) -> str:
        dest = self._resolve(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        return str(dest)

    def get(self, key: str, local_path: str | Path) -> Path:
        src = self._resolve(key)
        if not src.exists():
            raise FileNotFoundError(key)
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()
