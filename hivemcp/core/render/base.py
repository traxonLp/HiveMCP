"""Shared renderer plumbing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

MEDIA_TYPES = {
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass
class RenderedFile:
    """The in-memory result of a render, before it is delivered anywhere."""

    data: bytes
    filename: str
    media_type: str
    warnings: list[str] = field(default_factory=list)
    slide_count: int | None = None
    page_estimate: int | None = None
    sheet_names: list[str] | None = None

    @property
    def size_bytes(self) -> int:
        return len(self.data)

    def write_to(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self.filename
        path.write_bytes(self.data)
        return path


class RenderError(Exception):
    """A spec could not be rendered.

    Surfaces translate this into a message the model can act on, so the text should
    say what to change rather than what went wrong internally.
    """
