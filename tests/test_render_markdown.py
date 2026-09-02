"""Markdown rendering.

Unlike the Office renderers there is no library to trust here — the output is a string
this code builds character by character. So these tests read the string, and they
concentrate on the two things that make Markdown fragile: escaping that changes a
construct's meaning, and mappings for blocks the format has no equivalent for.
"""

from __future__ import annotations

import pytest

from hivemcp.core.models import (
    Bullet,
    BulletList,
    CodeBlock,
    DocSpec,
    Heading,
    ImageBlock,
    ImageRef,
    NumberedList,
    PageBreak,
    Paragraph,
    RenderOptions,
    TableBlock,
    TableData,
    TableOfContents,
)
from hivemcp.core.render.base import MEDIA_TYPES, RenderError
from hivemcp.core.render.markdown import anchor_for, escape_block, escape_cell, render_markdown


def text_of(spec: DocSpec, options: RenderOptions | None = None) -> str:
    return render_markdown(spec, options or RenderOptions()).data.decode("utf-8")


def doc(*blocks, **kwargs) -> DocSpec:  # noqa: ANN002, ANN003
    return DocSpec(title=kwargs.pop("title", "T"), blocks=list(blocks) or [Paragraph(text="x")], **kwargs)


# --------------------------------------------------------------------------- #
# Shape of the file
# --------------------------------------------------------------------------- #


def test_the_result_is_utf8_markdown() -> None:
    rendered = render_markdown(doc(Paragraph(text="Grüße")), RenderOptions())

    assert rendered.media_type == MEDIA_TYPES["md"]
    assert rendered.filename.endswith(".md")
    assert "charset=utf-8" in rendered.media_type
    assert "Grüße" in rendered.data.decode("utf-8")


def test_the_title_becomes_the_only_h1() -> None:
    """Headings shift down one level because the title occupies h1.

    Two h1s in one document is a lint error in every Markdown checker and reads wrong in
    a table of contents.
    """
    body = text_of(doc(Heading(text="Kapitel", level=1)))

    assert body.count("\n# ") + body.startswith("# ") == 1
    assert "## Kapitel" in body


def test_blank_lines_never_pile_up() -> None:
    body = text_of(doc(Paragraph(text="a"), PageBreak(), Paragraph(text="b")))

    assert "\n\n\n" not in body


# --------------------------------------------------------------------------- #
# Escaping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("- kein Listeneintrag", "\\- kein Listeneintrag"),
        ("# keine Überschrift", "\\# keine Überschrift"),
        ("> kein Zitat", "\\> kein Zitat"),
        ("1. keine Nummer", "\\1. keine Nummer"),
    ],
)
def test_line_openers_are_escaped(raw: str, expected: str) -> None:
    """Prose that happens to start with a marker must not become that construct."""
    assert escape_block(raw) == expected


@pytest.mark.parametrize(
    "raw", ["ein * Stern", "snake_case_wort", "Preis: 5 - 3", "a #hashtag mitten drin"]
)
def test_ordinary_prose_is_left_alone(raw: str) -> None:
    """Escaping everything would turn readable text into a thicket of backslashes.

    Only a marker that *opens a line* changes the block's meaning; the same character
    inside a sentence does not.
    """
    assert escape_block(raw) == raw


def test_a_pipe_in_a_cell_is_escaped() -> None:
    """An unescaped pipe ends the cell and shifts every column after it."""
    body = text_of(doc(TableBlock(data=TableData(headers=["A|B"], rows=[["x|y"]]))))

    assert "A\\|B" in body
    assert body.count("|") % 2 == 0


def test_a_newline_in_a_cell_is_flattened() -> None:
    """A table row is one line. A newline inside a cell would end the table."""
    assert escape_cell("zwei\nzeilen") == "zwei zeilen"


# --------------------------------------------------------------------------- #
# Blocks without an equivalent
# --------------------------------------------------------------------------- #


def test_a_page_break_becomes_a_rule() -> None:
    """Markdown has no pagination, but the author meant "a division here"."""
    assert "\n---\n" in text_of(doc(Paragraph(text="a"), PageBreak(), Paragraph(text="b")))


def test_the_toc_is_written_out_not_deferred() -> None:
    """Word inserts a field it fills in itself; Markdown has no such mechanism."""
    body = text_of(
        doc(TableOfContents(depth=3), Heading(text="Erstes Kapitel", level=1))
    )

    assert "- [Erstes Kapitel](#erstes-kapitel)" in body


def test_toc_entries_respect_the_depth() -> None:
    body = text_of(
        doc(
            TableOfContents(depth=1),
            Heading(text="Oben", level=1),
            Heading(text="Tiefer", level=2),
        )
    )

    assert "Oben" in body.split("## Oben")[0]
    assert "(#tiefer)" not in body


@pytest.mark.parametrize(
    ("heading", "anchor"),
    [
        ("Einleitung", "einleitung"),
        ("Anhang & Co.", "anhang--co"),
        ("Zwei  Wörter", "zwei--wörter"),
        ("1. Kapitel", "1-kapitel"),
    ],
)
def test_anchors_follow_githubs_rules(heading: str, anchor: str) -> None:
    """A table of contents whose links do not resolve is worse than none."""
    assert anchor_for(heading) == anchor


def test_include_toc_adds_one_without_a_block() -> None:
    body = text_of(doc(Heading(text="Kapitel", level=1)), RenderOptions(include_toc=True))

    assert "(#kapitel)" in body


def test_include_toc_does_not_duplicate_an_explicit_block() -> None:
    body = text_of(
        doc(TableOfContents(depth=2), Heading(text="Kapitel", level=1)),
        RenderOptions(include_toc=True),
    )

    assert body.count("(#kapitel)") == 1


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #


def test_nested_lists_indent_and_keep_their_kind() -> None:
    body = text_of(
        doc(NumberedList(items=[Bullet(text="oben", children=[Bullet(text="unten")])]))
    )

    assert "1. oben" in body
    assert "  1. unten" in body


def test_bold_and_italic_survive() -> None:
    body = text_of(doc(BulletList(items=[Bullet(text="fett", bold=True)])))

    assert "- **fett**" in body


def test_a_quote_paragraph_becomes_a_blockquote() -> None:
    assert "> Achtung" in text_of(doc(Paragraph(text="Achtung", style="quote")))


def test_a_code_fence_outgrows_its_content() -> None:
    """A fence no longer than the backticks inside it ends the block early."""
    body = text_of(doc(CodeBlock(text="print('``` hier')", language="python")))

    assert "````python" in body
    assert body.count("````") == 2


def test_a_table_keeps_its_columns_aligned(tiny_png: str) -> None:
    body = text_of(
        doc(TableBlock(data=TableData(headers=["A", "B"], rows=[["1", "2"]])))
    )

    assert "| A | B |" in body
    assert "| --- | --- |" in body
    assert "| 1 | 2 |" in body


def test_an_inline_image_becomes_a_data_uri(tiny_png: str) -> None:
    """Markdown has nowhere to put a companion file, and a relative path to something
    this server does not publish would be a broken link."""
    body = text_of(doc(ImageBlock(image=ImageRef(data_base64=tiny_png, alt_text="Bild"))))

    assert body.startswith("# T") or "![Bild](data:image/png;base64," in body
    assert "![Bild](data:image/png;base64," in body


def test_a_broken_image_names_the_block() -> None:
    with pytest.raises(RenderError) as caught:
        text_of(doc(Paragraph(text="ok"), ImageBlock(image=ImageRef(data_base64="not-base64"))))

    assert "2" in str(caught.value)


# --------------------------------------------------------------------------- #
# Front matter
# --------------------------------------------------------------------------- #


def test_front_matter_is_off_by_default() -> None:
    """A plain README should not open with a YAML block nobody asked for."""
    assert not text_of(doc(Paragraph(text="x"))).startswith("---")


def test_front_matter_carries_the_metadata() -> None:
    spec = DocSpec(
        title="Titel", subtitle="Untertitel", author="Team", blocks=[Paragraph(text="x")]
    )
    body = text_of(spec, RenderOptions(frontmatter=True, language="de"))

    assert body.startswith("---\n")
    assert 'title: "Titel"' in body
    assert 'author: "Team"' in body
    assert 'lang: "de"' in body


def test_front_matter_quotes_defensively() -> None:
    """A colon in a title turns an unquoted value into a mapping, and the build fails
    somewhere far from here."""
    body = text_of(
        DocSpec(title="Bericht: Q3", blocks=[Paragraph(text="x")]),
        RenderOptions(frontmatter=True),
    )

    assert 'title: "Bericht: Q3"' in body


# --------------------------------------------------------------------------- #
# Options the format cannot honour
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "options",
    [
        RenderOptions(font_family="Arial"),
        RenderOptions(font_size_base=14),
        RenderOptions(page_size="Letter"),
        RenderOptions(orientation="landscape"),
    ],
)
def test_settings_the_format_ignores_are_warned_about(options: RenderOptions) -> None:
    """Silently ignoring an option is how include_toc stayed a dead checkbox for months."""
    rendered = render_markdown(doc(Paragraph(text="x")), options)

    assert rendered.warnings
    assert "Markdown carries no" in rendered.warnings[0]


def test_a_plain_render_warns_about_nothing() -> None:
    assert render_markdown(doc(Paragraph(text="x")), RenderOptions()).warnings == []
