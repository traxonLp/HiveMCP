"""Skills: usage guidance shipped with the server and served over three channels.

A skill is one Markdown file with a small frontmatter block, living at
``hivemcp/skills/<name>/SKILL.md``. It is the single source of truth; the MCP prompt, the
``hive_usage_guide`` tool and ``GET /skills/{name}`` all read from this registry rather
than restating anything.

Three channels rather than one because OpenWebUI does not reliably surface MCP prompts,
and a guide the model cannot reach is worth nothing:

1. ``prompts/list`` / ``prompts/get`` — protocol-correct, works in other MCP clients.
2. The ``hive_usage_guide`` tool — always works, because a model that can call tools at
   all can call this one.
3. ``GET /skills/{name}`` — Markdown, for pasting into a model's system prompt or adding
   as a knowledge document.

Files are read once at startup and held in memory. They ship inside the image and cannot
change under a running process, so re-reading per request would buy nothing.

The frontmatter parser deliberately handles only ``key: value`` — no YAML dependency for
three fields, and no arbitrary YAML evaluation over files that could one day come from
somewhere less trusted than the image.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"

DEFAULT_SKILL = "hivemcp-usage"

# The name is used in a URL path and to build a filesystem path, so it is validated on
# shape and rejected rather than sanitised.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", re.DOTALL)


class SkillError(Exception):
    """A skill could not be found or read."""


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    description: str
    body: str
    """The Markdown with the frontmatter stripped — what a model should be given."""

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "content": self.body,
        }


def _parse(raw: str, fallback_name: str) -> Skill:
    match = _FRONTMATTER.match(raw)
    if match is None:
        # No frontmatter is not fatal: the body is still the useful part, and failing to
        # start the server over a missing title would be a poor trade.
        logger.warning("skill %s has no frontmatter block", fallback_name)
        return Skill(
            name=fallback_name, title=fallback_name, description="", body=raw.strip()
        )

    header, body = match.group(1), match.group(2)
    fields: dict[str, str] = {}
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()

    return Skill(
        name=fields.get("name") or fallback_name,
        title=fields.get("title") or fallback_name,
        description=fields.get("description", ""),
        body=body.strip(),
    )


class SkillRegistry:
    """Every skill bundled with the server, loaded once."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or SKILLS_ROOT
        self._skills: dict[str, Skill] = {}
        self._load()

    def _load(self) -> None:
        if not self.root.is_dir():
            logger.warning("no skills directory at %s", self.root)
            return

        for directory in sorted(self.root.iterdir()):
            path = directory / "SKILL.md"
            if not path.is_file():
                continue
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                logger.warning("could not read %s", path, exc_info=True)
                continue
            skill = _parse(raw, directory.name)
            self._skills[skill.name] = skill

        logger.info(
            "loaded %d skill(s): %s",
            len(self._skills),
            ", ".join(sorted(self._skills)) or "none",
        )

    def names(self) -> list[str]:
        return sorted(self._skills)

    def all(self) -> list[Skill]:
        return [self._skills[name] for name in self.names()]

    def get(self, name: str) -> Skill:
        if not _SAFE_NAME.match(name):
            raise SkillError(f"{name!r} is not a valid skill name")
        skill = self._skills.get(name)
        if skill is None:
            raise SkillError(
                f"there is no skill {name!r}. Available: "
                f"{', '.join(self.names()) or 'none'}."
            )
        return skill

    @property
    def default(self) -> Skill | None:
        """The guide to hand out when no name was asked for."""
        return self._skills.get(DEFAULT_SKILL) or (self.all()[0] if self._skills else None)
