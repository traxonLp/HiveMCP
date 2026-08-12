"""Round-trip tests: render, reopen with python-pptx, assert on what is actually inside.

Byte comparison against a golden file is deliberately avoided. OOXML output is not
deterministic (timestamps, zip entry order, revision ids), so a golden test would fail
for reasons unrelated to the change being made.
"""

from __future__ import annotations

import io

import pytest
from pptx import Presentation
from pptx.util import Pt

from hivemcp.core.models import Bullet, DeckSpec, ImageRef, RenderOptions, Slide, TableData
from hivemcp.core.render.base import RenderError
from hivemcp.core.render.pptx import render_presentation


def reopen(data: bytes) -> Presentation:
    return Presentation(io.BytesIO(data))


def body_shapes(slide) -> list:
    return [
        shape
        for shape in slide.shapes
        if shape.has_text_frame
        and not (shape.is_placeholder and shape.placeholder_format.idx == 0)
    ]


def test_renders_every_layout(deck: DeckSpec, options: RenderOptions) -> None:
    result = render_presentation(deck, options)

    assert result.slide_count == len(deck.slides)
    assert result.media_type.endswith("presentationml.presentation")
    assert result.filename == "Quartalsbericht.pptx"

    presentation = reopen(result.data)
    assert len(presentation.slides) == len(deck.slides)
    assert presentation.slides[0].shapes.title.text == "Q3 2026"


def test_bullet_nesting_becomes_indent_levels(options: RenderOptions) -> None:
    spec = DeckSpec(
        title="T",
        slides=[
            Slide(
                title="Nesting",
                bullets=[
                    Bullet(text="L0", children=[Bullet(text="L1", children=[Bullet(text="L2")])])
                ],
            )
        ],
    )
    presentation = reopen(render_presentation(spec, options).data)
    body = body_shapes(presentation.slides[0])[0]

    assert [(p.text, p.level) for p in body.text_frame.paragraphs] == [
        ("L0", 0),
        ("L1", 1),
        ("L2", 2),
    ]


def test_speaker_notes_survive(deck: DeckSpec, options: RenderOptions) -> None:
    presentation = reopen(render_presentation(deck, options).data)
    notes = presentation.slides[2].notes_slide.notes_text_frame.text
    assert notes == "Langsam sprechen."


def test_table_and_chart_and_picture_are_present(deck: DeckSpec, options: RenderOptions) -> None:
    presentation = reopen(render_presentation(deck, options).data)

    table = next(s.table for s in presentation.slides[4].shapes if s.has_table)
    assert [cell.text for cell in table.rows[0].cells] == ["Region", "Umsatz"]

    chart = next(s.chart for s in presentation.slides[5].shapes if s.has_chart)
    assert [series.name for series in chart.plots[0].series] == ["2026"]

    pictures = [s for s in presentation.slides[6].shapes if s.shape_type == 13]
    assert len(pictures) == 1
    assert round(pictures[0].width.cm, 1) == 6.0


def test_two_content_fills_both_columns(deck: DeckSpec, options: RenderOptions) -> None:
    presentation = reopen(render_presentation(deck, options).data)
    texts = [shape.text_frame.text for shape in body_shapes(presentation.slides[3])]
    assert "Manuell" in texts
    assert "Automatisiert" in texts


def test_empty_placeholders_are_removed(options: RenderOptions) -> None:
    """A slide with only a title must not leave 'Click to add text' prompts behind."""
    spec = DeckSpec(title="T", slides=[Slide(layout="title_content", title="Nur Titel")])
    presentation = reopen(render_presentation(spec, options).data)
    assert body_shapes(presentation.slides[0]) == []


def test_unknown_font_warns_but_still_applies(options: RenderOptions) -> None:
    spec = DeckSpec(title="T", slides=[Slide(title="X", bullets=[Bullet(text="hi")])])
    result = render_presentation(
        spec, options.model_copy(update={"font_family": "Comic Sans MS", "font_size_base": 20})
    )

    assert any("Comic Sans MS" in warning for warning in result.warnings)

    presentation = reopen(result.data)
    runs = [
        run
        for shape in presentation.slides[0].shapes
        if shape.has_text_frame
        for paragraph in shape.text_frame.paragraphs
        for run in paragraph.runs
    ]
    assert runs, "expected at least one run"
    assert all(run.font.name == "Comic Sans MS" for run in runs)
    body = body_shapes(presentation.slides[0])[0]
    assert body.text_frame.paragraphs[0].runs[0].font.size == Pt(20)


def test_safe_font_produces_no_warning(options: RenderOptions) -> None:
    spec = DeckSpec(title="T", slides=[Slide(title="X")])
    result = render_presentation(spec, options.model_copy(update={"font_family": "Calibri"}))
    assert result.warnings == []


def test_filename_is_sanitised(options: RenderOptions) -> None:
    spec = DeckSpec(title="../../etc/passwd", slides=[Slide(title="X")])
    result = render_presentation(spec, options)
    assert "/" not in result.filename
    assert ".." not in result.filename


def test_layout_requiring_content_fails_loudly(options: RenderOptions) -> None:
    spec = DeckSpec(title="T", slides=[Slide(layout="chart", title="Ohne Daten")])
    with pytest.raises(RenderError, match="slide 1"):
        render_presentation(spec, options)


def test_image_without_resolver_names_the_problem(options: RenderOptions) -> None:
    spec = DeckSpec(
        title="T", slides=[Slide(layout="image", title="X", image=ImageRef(file_id="abc"))]
    )
    with pytest.raises(RenderError, match="resolver"):
        render_presentation(spec, options)


def test_image_resolver_is_used(options: RenderOptions, tiny_png: str) -> None:
    import base64

    calls: list[str] = []

    def resolver(file_id: str) -> bytes:
        calls.append(file_id)
        return base64.b64decode(tiny_png)

    spec = DeckSpec(
        title="T", slides=[Slide(layout="image", title="X", image=ImageRef(file_id="abc"))]
    )
    result = render_presentation(spec, options, image_resolver=resolver)

    assert calls == ["abc"]
    presentation = reopen(result.data)
    assert [s for s in presentation.slides[0].shapes if s.shape_type == 13]


def test_named_placeholders_are_substituted(options: RenderOptions) -> None:
    spec = DeckSpec(
        title="T",
        slides=[Slide(title="Angebot fuer {{kunde}}", placeholders={"kunde": "ACME"})],
    )
    presentation = reopen(render_presentation(spec, options).data)
    assert presentation.slides[0].shapes.title.text == "Angebot fuer ACME"


def test_table_column_widths_are_applied(options: RenderOptions) -> None:
    spec = DeckSpec(
        title="T",
        slides=[
            Slide(
                layout="table",
                title="X",
                table=TableData(
                    headers=["A", "B"], rows=[["1", "2"]], column_widths_cm=[6.0, 4.0]
                ),
            )
        ],
    )
    presentation = reopen(render_presentation(spec, options).data)
    table = next(s.table for s in presentation.slides[0].shapes if s.has_table)
    assert [round(column.width.cm, 1) for column in table.columns] == [6.0, 4.0]
