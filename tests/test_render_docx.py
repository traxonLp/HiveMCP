from __future__ import annotations

import io

import pytest
from docx import Document
from docx.oxml.ns import qn

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
from hivemcp.core.render.base import RenderError
from hivemcp.core.render.docx import render_document


def reopen(data: bytes) -> Document:
    return Document(io.BytesIO(data))


def styles_of(document: Document) -> list[tuple[str, str]]:
    return [(p.style.name, p.text) for p in document.paragraphs if p.text.strip()]


def test_front_matter_and_metadata(document: DocSpec, options: RenderOptions) -> None:
    result = render_document(document, options)
    reopened = reopen(result.data)

    assert reopened.core_properties.title == "Betriebshandbuch"
    assert reopened.core_properties.author == "Plattform-Team"
    assert reopened.core_properties.language == "de"
    assert ("Title", "Betriebshandbuch") in styles_of(reopened)
    assert ("Subtitle", "Version 0.1") in styles_of(reopened)


def test_headings_and_lists_map_to_word_styles(options: RenderOptions) -> None:
    spec = DocSpec(
        title="T",
        blocks=[
            Heading(text="H1", level=1),
            Heading(text="H2", level=2),
            BulletList(
                items=[
                    Bullet(
                        text="A",
                        children=[Bullet(text="B", children=[Bullet(text="C")])],
                    )
                ]
            ),
            NumberedList(items=[Bullet(text="Eins")]),
        ],
    )
    found = styles_of(reopen(render_document(spec, options).data))

    assert ("Heading 1", "H1") in found
    assert ("Heading 2", "H2") in found
    assert ("List Bullet", "A") in found
    assert ("List Bullet 2", "B") in found
    assert ("List Bullet 3", "C") in found
    assert ("List Number", "Eins") in found


def test_toc_inserts_a_real_field_and_requests_update(options: RenderOptions) -> None:
    """The TOC must be a Word field, not a rendered list.

    A field lets Word compute page numbers and keep entries in sync; a rendered list
    would be stale the moment anyone edits the document.
    """
    spec = DocSpec(title="T", blocks=[TableOfContents(depth=2), Heading(text="H", level=1)])
    reopened = reopen(render_document(spec, options).data)

    instructions = [element.text for element in reopened.element.body.iter(qn("w:instrText"))]
    assert any('TOC \\o "1-2"' in (text or "") for text in instructions)
    assert reopened.settings.element.find(qn("w:updateFields")) is not None


def test_table_widths_are_set_on_every_cell(options: RenderOptions) -> None:
    """Word ignores a column width unless it is repeated on each cell of the column."""
    spec = DocSpec(
        title="T",
        blocks=[
            TableBlock(
                data=TableData(
                    headers=["A", "B"],
                    rows=[["1", "2"], ["3", "4"]],
                    column_widths_cm=[8.0, 3.0],
                ),
                caption="Tabelle 1",
            )
        ],
    )
    reopened = reopen(render_document(spec, options).data)
    table = reopened.tables[0]

    assert [[c.text for c in row.cells] for row in table.rows] == [
        ["A", "B"],
        ["1", "2"],
        ["3", "4"],
    ]
    assert all(round(row.cells[0].width.cm, 1) == 8.0 for row in table.rows)
    assert ("Caption", "Tabelle 1") in styles_of(reopened)


def test_image_and_page_break_and_code(options: RenderOptions, tiny_png: str) -> None:
    spec = DocSpec(
        title="T",
        blocks=[
            ImageBlock(image=ImageRef(data_base64=tiny_png, width_cm=4), caption="Abb. 1"),
            PageBreak(),
            CodeBlock(text="uvicorn hivemcp.app:app", language="bash"),
        ],
    )
    reopened = reopen(render_document(spec, options).data)

    assert len(reopened.inline_shapes) == 1
    assert round(reopened.inline_shapes[0].width.cm, 1) == 4.0
    code_runs = [
        run
        for paragraph in reopened.paragraphs
        for run in paragraph.runs
        if run.text.startswith("uvicorn")
    ]
    assert code_runs and code_runs[0].font.name == "Consolas"


def test_page_setup_respects_orientation(options: RenderOptions, document: DocSpec) -> None:
    landscape = render_document(
        document, options.model_copy(update={"orientation": "landscape"})
    )
    section = reopen(landscape.data).sections[0]
    assert round(section.page_width.cm, 1) == 29.7
    assert round(section.page_height.cm, 1) == 21.0


def test_font_lands_on_normal_style_so_it_is_inherited(
    document: DocSpec, options: RenderOptions
) -> None:
    result = render_document(
        document, options.model_copy(update={"font_family": "Cambria", "font_size_base": 11})
    )
    normal = reopen(result.data).styles["Normal"]
    assert normal.font.name == "Cambria"
    assert normal.font.size.pt == 11


def test_placeholders_are_substituted(options: RenderOptions) -> None:
    spec = DocSpec(
        title="T",
        blocks=[Paragraph(text="Angebot fuer {{kunde}}")],
        placeholders={"kunde": "ACME"},
    )
    texts = [text for _, text in styles_of(reopen(render_document(spec, options).data))]
    assert "Angebot fuer ACME" in texts


def test_page_estimate_grows_with_content(options: RenderOptions) -> None:
    short = DocSpec(title="T", blocks=[Paragraph(text="kurz")])
    long = DocSpec(title="T", blocks=[Paragraph(text="x" * 20_000)])

    assert render_document(short, options).page_estimate == 1
    assert render_document(long, options).page_estimate > 5


def test_broken_template_reports_the_filename(options: RenderOptions, tmp_path) -> None:
    broken = tmp_path / "kaputt.docx"
    broken.write_bytes(b"not a docx at all")
    spec = DocSpec(title="T", blocks=[Paragraph(text="x")])

    with pytest.raises(RenderError, match="kaputt.docx"):
        render_document(spec, options, template_path=broken)
