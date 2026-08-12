"""Artifact storage on the PVC.

Rendered files land in ``$HIVE_DATA_DIR/tmp/<artifact_id>/`` and are swept after a TTL.
The directory-per-artifact layout keeps the original filename intact (so the browser's
Save dialog shows something sensible) without having to escape it into a flat key.
"""

from __future__ import annotations

import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    path: Path

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def size_bytes(self) -> int:
        return self.path.stat().st_size


class ArtifactStore:
    def __init__(self, root: Path, ttl_minutes: int = 60) -> None:
        self.root = root
        self.ttl_seconds = ttl_minutes * 60

    def put(self, data: bytes, filename: str) -> StoredArtifact:
        artifact_id = uuid.uuid4().hex
        directory = self.root / artifact_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_bytes(data)
        return StoredArtifact(artifact_id=artifact_id, path=path)

    def get(self, artifact_id: str) -> StoredArtifact | None:
        # A caller-supplied id must never escape the root, no matter what it contains.
        if not artifact_id.isalnum():
            return None
        directory = self.root / artifact_id
        if not directory.is_dir():
            return None
        files = [entry for entry in directory.iterdir() if entry.is_file()]
        if not files:
            return None
        return StoredArtifact(artifact_id=artifact_id, path=files[0])

    def sweep(self, now: float | None = None) -> int:
        """Delete artifact directories older than the TTL. Returns how many went."""
        if not self.root.is_dir():
            return 0
        cutoff = (now or time.time()) - self.ttl_seconds
        removed = 0
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            try:
                if directory.stat().st_mtime < cutoff:
                    shutil.rmtree(directory, ignore_errors=True)
                    removed += 1
            except OSError:
                # Another replica sweeping the same RWX volume may have won the race.
                # Losing it is the expected outcome, not an error worth logging loudly.
                logger.debug("could not sweep %s", directory, exc_info=True)
        return removed
