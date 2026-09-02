"""Markdown rendering.

Takes the same ``DocSpec`` as the Word renderer. That is deliberate: the block types
already describe a document — headings, paragraphs, lists, tables, images, code — and a
second spec would only give the model a second thing to learn and this codebase a second
thing to keep in step. What differs is the output, not the content model.

Where Markdown has no equivalent, the mapping is chosen rather than invented:

* **Page break** becomes a thematic break (``---``). Markdown has no pagination, but the
  author meant "a division here", and that is what a rule means.
* **Table of contents** becomes a list of links to the headings that follow it, generated
  at render time. Word gets a field it updates itself; Markdown has no such mechanism, so
  the entries are written out. Anchors follow GitHub's slug rules, which is what almost
  every renderer that supports them implements.
* **Paragraph style** ``quote`` becomes a blockquote. ``caption`` and ``intense`` become
  italic and bold — a lossy but honest reading, since Markdown has no styles.
* **Alignment** is dropped. Markdown has no way to express it without falling back to raw
  HTML, and emitting HTML would break the plain-text promise of the format.

Escaping is conservative rather than clever: only the characters that would change the
structure of the surrounding construct are escaped, so ordinary prose containing an
asterisk or an underscore comes out unchanged and readable.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from .base import MEDIA_TYPES, RenderedFile, RenderError
from .theme import ImageResolver, estimate_pages, safe_filename

# Characters that begin a construct when they open a line. Escaped only there, because
# escaping them everywhere turns readable prose into a thicket of backslashes.
_LINE_START = re.compile(r"^(\s*)([#>\-+*=]|\d+[.)])(\s)")

# Table cells are the one place a pipe cannot survive unescaped: it would end the cell.
_PIPE = re.compile(r"\|")

_ANCHOR_STRIP = re.compile(r"[^\w\- ]", re.UNICODE)


def escape_block(text: str) -> str:
    """Escape only what would otherwise change the block's meaning."""
    return _LINE_START.sub(r"\1\\\2\3", text or "")


def escape_cell(text: str) -> str:
    """A pipe inside a cell ends the cell, so it has to go. Newlines do too."""
    collapsed = " ".join(str(text or "").split())
    return _PIPE.sub(r"\\|", collapsed)


def anchor_for(heading: str) -> str:
    """GitHub's heading-anchor rules: lowercase, punctuation dropped, spaces to hyphens.

    Reimplemented rather than guessed at: every renderer that supports anchors follows
    this, and a table of contents whose links do not resolve is worse than none.
    """
    lowered = heading.strip().lower()
    return _ANCHOR_STRIP.sub("", lowered).replace(" ", "-")


class _DefaultToc:
    """Stand-in for a ``toc`` block when ``include_toc`` asked for one."""

    depth = 3


class MarkdownRenderer:
    def __init__(
        self,
        options: Any,
        image_resolver: ImageResolver | None = None,
        template: str | None = None,
    ) -> None:
        self.options = options
        self.image_resolver = image_resolver
        self.template = template
        self.warnings: list[str] = []
        self._characters = 0

    # ------------------------------------------------------------------ render

    def render(self, spec: Any) -> RenderedFile:
        lines: list[str] = []

        if getattr(self.options, "frontmatter", False):
            lines.extend(self._frontmatter(spec))

        lines.append(f"# {escape_block(spec.title)}")
        self._count(spec.title)
        if getattr(spec, "subtitle", None):
            lines += ["", f"*{escape_block(spec.subtitle)}*"]
            self._count(spec.subtitle)
        lines.append("")

        # Same rule as the Word renderer: the option adds one, an explicit block wins so
        # a spec that placed one deliberately does not end up with two.
        if getattr(self.options, "include_toc", False) and not any(
            getattr(block, "type", None) == "toc" for block in spec.blocks
        ):
            lines.extend(self._block_toc(_DefaultToc(), spec))
            lines.append("")

        for index, block in enumerate(spec.blocks):
            handler = getattr(self, f"_block_{block.type}", None)
            if handler is None:
                raise RenderError(f"block {index + 1}: unknown type {block.type!r}")
            try:
                produced = handler(block, spec)
            except RenderError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad block must name itself
                raise RenderError(
                    f"block {index + 1} ({block.type}) could not be rendered: {exc}"
                ) from exc
            lines.extend(produced)
            lines.append("")

        body = "\n".join(lines).rstrip() + "\n"
        body = self._apply_template(body, spec)
        # Three or more blank lines read as an accident in a diff, and every Markdown
        # renderer treats two the same as five.
        body = re.sub(r"\n{3,}", "\n\n", body)

        return RenderedFile(
            data=body.encode("utf-8"),
            filename=safe_filename(
                getattr(self.options, "filename", None), spec.title, "md"
            ),
            media_type=MEDIA_TYPES["md"],
            warnings=list(self.warnings),
            page_estimate=estimate_pages(
                self._characters, getattr(self.options, "density", "normal")
            ),
        )

    # -------------------------------------------------------------- front matter

    def _frontmatter(self, spec: Any) -> list[str]:
        """YAML front matter, quoted defensively.

        Values come from a model and land in a block that static-site generators parse
        strictly. A colon in a title would otherwise produce a mapping where a string was
        meant, and the build fails somewhere far from here.
        """
        entries = [("title", spec.title)]
        if getattr(spec, "subtitle", None):
            entries.append(("description", spec.subtitle))
        if getattr(spec, "author", None):
            entries.append(("author", spec.author))
        language = getattr(self.options, "language", None)
        if language:
            entries.append(("lang", language))

        lines = ["---"]
        for key, value in entries:
            escaped = str(value).replace('"', '\\"')
            lines.append(f'{key}: "{escaped}"')
        lines += ["---", ""]
        return lines

    # ------------------------------------------------------------------ blocks

    def _block_heading(self, block: Any, spec: Any) -> list[str]:
        self._count(block.text)
        return [f"{'#' * min(block.level + 1, 6)} {escape_block(block.text)}"]

    def _block_paragraph(self, block: Any, spec: Any) -> list[str]:
        self._count(block.text)
        text = escape_block(block.text)
        if block.style == "quote":
            return [f"> {line}" for line in text.splitlines() or [""]]
        if block.style == "caption":
            return [f"*{text}*"]
        if block.style == "intense":
            return [f"**{text}**"]
        return [text]

    def _block_bullet_list(self, block: Any, spec: Any) -> list[str]:
        return self._list(block.items, ordered=False)

    def _block_numbered_list(self, block: Any, spec: Any) -> list[str]:
        return self._list(block.items, ordered=True)

    def _list(self, items: list[Any], *, ordered: bool, level: int = 0) -> list[str]:
        lines: list[str] = []
        for number, item in enumerate(items, start=1):
            marker = f"{number}." if ordered else "-"
            text = escape_block(item.text)
            if item.bold:
                text = f"**{text}**"
            if item.italic:
                text = f"*{text}*"
            lines.append(f"{'  ' * level}{marker} {text}")
            self._count(item.text)
            if item.children:
                # Nested lists keep the parent's kind, matching how the other renderers
                # treat children: a level is an indent, not a change of list type.
                lines.extend(self._list(item.children, ordered=ordered, level=level + 1))
        return lines

    def _block_table(self, block: Any, spec: Any) -> list[str]:
        data = block.data
        header = [escape_cell(cell) for cell in data.headers]
        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
        ]
        for row in data.rows:
            cells = [escape_cell(cell) for cell in row]
            # The model validator already enforces the width; this only guards the
            # rendering against a spec built in code rather than through the schema.
            cells += [""] * (len(header) - len(cells))
            lines.append("| " + " | ".join(cells[: len(header)]) + " |")
            self._count(" ".join(str(cell) for cell in row))
        if block.caption:
            lines += ["", f"*{escape_block(block.caption)}*"]
            self._count(block.caption)
        return lines

    def _block_image(self, block: Any, spec: Any) -> list[str]:
        image = block.image
        alt = escape_cell(getattr(image, "alt_text", None) or block.caption or "image")

        if getattr(image, "data_base64", None):
            # Inline as a data URI. Markdown has nowhere to put a companion file, and a
            # relative path to something this server does not publish would be a broken
            # link. Large images make an unwieldy file, hence the warning.
            try:
                raw = base64.b64decode(image.data_base64, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise RenderError(f"the inline image could not be decoded: {exc}") from exc
            if len(raw) > 512 * 1024:
                self.warnings.append(
                    f"An image of {len(raw) // 1024} KB was embedded as a data URI, which "
                    "makes the Markdown file large and hard to read. Consider linking to "
                    "a hosted image instead."
                )
            source = f"data:image/png;base64,{image.data_base64}"
        elif getattr(image, "file_id", None):
            if self.image_resolver is None:
                raise RenderError(
                    "an image referenced a file_id, but this render has no way to look "
                    "files up. Pass the image inline as data_base64 instead."
                )
            source = f"data:image/png;base64,{base64.b64encode(self.image_resolver(image.file_id)).decode('ascii')}"
        else:
            raise RenderError("an image block carries neither file_id nor data_base64")

        lines = [f"![{alt}]({source})"]
        if block.caption:
            lines += ["", f"*{escape_block(block.caption)}*"]
        return lines

    def _block_page_break(self, block: Any, spec: Any) -> list[str]:
        return ["---"]

    def _block_code(self, block: Any, spec: Any) -> list[str]:
        language = (block.language or "").strip()
        # A fence longer than any run of backticks inside, or the block ends early.
        longest = max((len(run) for run in re.findall(r"`+", block.text)), default=0)
        fence = "`" * max(3, longest + 1)
        self._count(block.text)
        return [f"{fence}{language}", block.text, fence]

    def _block_toc(self, block: Any, spec: Any) -> list[str]:
        """Written out, not a field.

        Word inserts a TOC field and computes the entries itself. Markdown has no such
        mechanism, so the entries are generated here from the headings that follow.
        """
        entries: list[str] = []
        for other in spec.blocks:
            if getattr(other, "type", None) != "heading" or other.level > block.depth:
                continue
            indent = "  " * (other.level - 1)
            entries.append(f"{indent}- [{escape_cell(other.text)}](#{anchor_for(other.text)})")
        return entries or ["*(no headings to list)*"]

    def _count(self, text: str | None) -> None:
        if text:
            self._characters += len(str(text))

    # ---------------------------------------------------------------- template

    def _apply_template(self, body: str, spec: Any) -> str:
        """Fill a text template's placeholders and insert the rendered body.

        A Markdown template is a plain ``.md`` file, not an archive: there is no layout to
        inspect, so it carries ``{{placeholders}}`` and one ``{{content}}`` marker saying
        where the generated document goes. A template without that marker gets the body
        appended, which is more useful than refusing.
        """
        if self.template is None:
            return body

        filled = self.template
        values = {
            "title": spec.title,
            "subtitle": getattr(spec, "subtitle", "") or "",
            "author": getattr(spec, "author", "") or "",
            **(getattr(spec, "placeholders", None) or {}),
        }
        for key, value in values.items():
            filled = filled.replace("{{" + key + "}}", str(value))

        if "{{content}}" in filled:
            return filled.replace("{{content}}", body)
        return filled.rstrip() + "\n\n" + body

    # -------------------------------------------------------------- warnings

    def note_unsupported(self, spec: Any) -> None:
        """Warn about options that cannot survive the format.

        Silently ignoring them is what made ``include_toc`` a dead checkbox for months.
        """
        for name, label in (
            ("font_family", "font"),
            ("font_size_base", "font size"),
            ("page_size", "page size"),
            ("orientation", "orientation"),
        ):
            value = getattr(self.options, name, None)
            if value and value not in ("A4", "portrait"):
                self.warnings.append(
                    f"Markdown carries no {label}, so that setting was ignored. It is "
                    "plain text; the renderer that displays it decides the appearance."
                )


def render_markdown(
    spec: Any,
    options: Any,
    image_resolver: ImageResolver | None = None,
    template_path: Path | None = None,
) -> RenderedFile:
    template = None
    if template_path is not None:
        try:
            template = template_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RenderError(
                f"template {template_path.name!r} could not be read as UTF-8 text. A "
                "Markdown template is a plain .md file."
            ) from exc

    renderer = MarkdownRenderer(options, image_resolver, template)
    renderer.note_unsupported(spec)
    return renderer.render(spec)
