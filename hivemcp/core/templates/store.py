"""Template storage on the PVC.

One shared, curated pool::

    <templates_dir>/<template_id>/{template.pptx, meta.json}

**Administrators write, everyone reads.** There are no per-user or per-group templates:
a template is a corporate design, and the point of curating one centrally is defeated if
every user can add their own. That also removes a question the earlier layout could not
answer — group membership is not in the validated identity, so group-scoped folders were
readable by everyone anyway.

No index database. Metadata lives in a ``meta.json`` beside each file, cached against its
mtime. SQLite was the obvious alternative and was rejected: file locking over NFS is
unreliable, and with several replicas on one ReadWriteMany volume that is a corruption
risk for no benefit at this size.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

TemplateKind = Literal["pptx", "docx", "xlsx", "md"]

# The template variants (.potx/.dotx/.xltx) are the proper ones, but people
# overwhelmingly have a normal document to hand, so both are accepted.
ALLOWED_EXTENSIONS: dict[str, TemplateKind] = {
    ".pptx": "pptx",
    ".potx": "pptx",
    ".docx": "docx",
    ".dotx": "docx",
    ".xlsx": "xlsx",
    ".xltx": "xlsx",
    ".xlsm": "xlsx",
    # Markdown templates are plain text, not archives. Everything the store does —
    # ownership, ids, metadata — is format-agnostic; only inspection differs, and that
    # branches in inspect.py.
    ".md": "md",
    ".markdown": "md",
}

_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class Identity(Protocol):
    """The two things the store needs from a caller.

    Structural rather than an import of ``auth.Identity``: that module pulls in httpx for
    token validation, and template storage has no business depending on an HTTP client to
    decide which directory to write to.
    """

    @property
    def user_id(self) -> str: ...

    @property
    def is_admin(self) -> bool: ...


class TemplateError(Exception):
    """A template could not be stored, found, or read."""


class NotPermitted(TemplateError):
    """The caller may read templates but not change them."""


@dataclass(frozen=True)
class TemplateMeta:
    template_id: str
    name: str
    kind: TemplateKind
    created_by: str
    filename: str
    size_bytes: int
    created_at: float
    description: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "kind": self.kind,
            "created_by": self.created_by,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> TemplateMeta:
        return cls(
            template_id=str(raw["template_id"]),
            name=str(raw.get("name") or raw["template_id"]),
            kind=raw.get("kind", "pptx"),
            # Older records used `owner_id`; read either so an existing volume keeps
            # working across the change to an admin-curated pool.
            created_by=str(raw.get("created_by") or raw.get("owner_id") or ""),
            filename=str(raw.get("filename", "template")),
            size_bytes=int(raw.get("size_bytes", 0)),
            created_at=float(raw.get("created_at", 0.0)),
            description=raw.get("description"),
        )


@dataclass
class StoredTemplate:
    meta: TemplateMeta
    path: Path


def slugify(name: str, fallback: str = "template") -> str:
    """Build a readable, path-safe id from a display name.

    Readability matters here in a way it does not for artifact ids: a model has to pass
    this string back, and ``corporate-deck-2026`` is far less error-prone than a UUID it
    must copy exactly.
    """
    normalised = unicodedata.normalize("NFKD", name)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")[:48]
    return slug or fallback


class TemplateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[Path, tuple[float, TemplateMeta]] = {}

    # ------------------------------------------------------------------ write

    @staticmethod
    def _require_admin(identity: Identity, action: str) -> None:
        if not identity.is_admin:
            raise NotPermitted(
                f"Only administrators can {action} templates. You can use any of the "
                "existing ones; call hive_list_templates to see them."
            )

    def put(
        self,
        data: bytes,
        *,
        name: str,
        filename: str,
        identity: Identity,
        description: str | None = None,
    ) -> TemplateMeta:
        self._require_admin(identity, "add")
        kind = kind_for(filename)

        template_id = self._free_id(slugify(name))
        directory = self.root / template_id
        stored_name = f"template{Path(filename).suffix.lower()}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / stored_name).write_bytes(data)
        except OSError as exc:
            # Almost always a volume the container cannot write to. Saying so beats a
            # 500, because the fix is in the deployment rather than in the request.
            raise TemplateError(
                f"the template store at {self.root} is not writable ({exc}). The volume "
                "mounted there must be writable by the user the container runs as."
            ) from exc

        meta = TemplateMeta(
            template_id=template_id,
            name=name.strip() or template_id,
            kind=kind,
            created_by=identity.user_id,
            filename=stored_name,
            size_bytes=len(data),
            created_at=time.time(),
            description=description,
        )
        (directory / "meta.json").write_text(
            json.dumps(meta.to_dict(), indent=2), encoding="utf-8"
        )
        logger.info("stored template %s (%s) by admin %s", template_id, kind, identity.user_id)
        return meta

    def delete(self, template_id: str, identity: Identity) -> None:
        self._require_admin(identity, "delete")
        found = self.get(template_id)

        directory = found.path.parent
        for entry in directory.iterdir():
            entry.unlink(missing_ok=True)
        directory.rmdir()
        self._cache.pop(directory / "meta.json", None)
        logger.info("deleted template %s by admin %s", template_id, identity.user_id)

    def _free_id(self, base: str) -> str:
        if not (self.root / base).exists():
            return base
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"
            if not (self.root / candidate).exists():
                return candidate
        return f"{base}-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------- read

    def get(self, template_id: str) -> StoredTemplate:
        """Fetch a template. Readable by everyone: the pool is shared by design."""
        if not _SAFE_ID.match(template_id):
            # Rejected on shape, not sanitised: a caller-supplied id must never be able
            # to walk out of the templates root.
            raise TemplateError(
                f"{template_id!r} is not a valid template id. Call hive_list_templates "
                "to see what exists."
            )

        meta_path = self.root / template_id / "meta.json"
        if not meta_path.is_file():
            raise TemplateError(
                f"there is no template {template_id!r}. Call hive_list_templates to see "
                "what is available."
            )
        meta = self._read_meta(meta_path)
        if meta is None:
            raise TemplateError(f"template {template_id!r} has unreadable metadata")

        path = meta_path.parent / meta.filename
        if not path.is_file():
            raise TemplateError(
                f"template {template_id!r} has metadata but no file; it is corrupt"
            )
        return StoredTemplate(meta=meta, path=path)

    def list(self, kind: TemplateKind | None = None) -> list[TemplateMeta]:
        if not self.root.is_dir():
            return []
        found: list[TemplateMeta] = []
        for directory in sorted(self.root.iterdir()):
            meta_path = directory / "meta.json"
            if not meta_path.is_file():
                continue
            meta = self._read_meta(meta_path)
            if meta is None or (kind and meta.kind != kind):
                continue
            found.append(meta)
        return sorted(found, key=lambda item: item.name.lower())

    def _read_meta(self, path: Path) -> TemplateMeta | None:
        """Read metadata, cached against the file's mtime.

        No shared index, so this is the hot path for listing. Invalidating on mtime keeps
        several replicas on one RWX volume consistent without any coordination.
        """
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None

        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]

        try:
            meta = TemplateMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            logger.warning("ignoring unreadable template metadata at %s", path, exc_info=True)
            return None

        self._cache[path] = (mtime, meta)
        return meta


def kind_for(filename: str) -> TemplateKind:
    suffix = Path(filename).suffix.lower()
    kind = ALLOWED_EXTENSIONS.get(suffix)
    if kind is None:
        raise TemplateError(
            f"{suffix or filename!r} is not a supported template type. Use one of: "
            f"{', '.join(sorted(ALLOWED_EXTENSIONS))}."
        )
    return kind
