"""Document specifications.

These models are the single source of truth for both surfaces: their JSON Schema is
what OpenWebUI shows the model in ``tools/list`` and in ``openapi.json``. Field
descriptions here are read by the LLM at call time, so they are written as instructions
to the model rather than as notes to a developer.

Design rule: prefer a tight ``Literal`` over free text wherever a closed set exists.
Every enum is one fewer thing the model can invent and one fewer branch we must
defensively handle at render time.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BULLET_DEPTH = 3


class StrictModel(BaseModel):
    """Reject unknown fields.

    A model that invents ``slide.image_url`` should get a loud validation error it can
    correct on the next turn, not a silently dropped field and a wrong-looking document.
    """

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Shared building blocks
# --------------------------------------------------------------------------- #


class Bullet(StrictModel):
    text: str = Field(description="Bullet text. Plain text; no markdown syntax.")
    children: list[Bullet] = Field(
        default_factory=list,
        description=f"Nested bullets. At most {MAX_BULLET_DEPTH} levels deep in total.",
    )
    bold: bool = False
    italic: bool = False


class TableData(StrictModel):
    headers: list[str] = Field(description="Header row labels.")
    rows: list[list[str]] = Field(
        description="Body rows. Every row must have exactly as many cells as there are headers."
    )
    column_widths_cm: list[float] | None = Field(
        default=None, description="Optional explicit column widths in centimetres."
    )
    header_bold: bool = True

    @model_validator(mode="after")
    def _check_row_widths(self) -> TableData:
        width = len(self.headers)
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(
                    f"row {index} has {len(row)} cells but there are {width} headers"
                )
        if self.column_widths_cm is not None and len(self.column_widths_cm) != width:
            raise ValueError("column_widths_cm must have one entry per header")
        return self


class ChartSeries(StrictModel):
    name: str
    values: list[float]


class ChartData(StrictModel):
    chart_type: Literal["bar", "column", "line", "pie", "scatter", "area"] = "column"
    categories: list[str] = Field(description="X-axis category labels.")
    series: list[ChartSeries] = Field(description="One entry per data series.")
    title: str | None = None

    @model_validator(mode="after")
    def _check_lengths(self) -> ChartData:
        if not self.series:
            raise ValueError("chart needs at least one series")
        for entry in self.series:
            if len(entry.values) != len(self.categories):
                raise ValueError(
                    f"series {entry.name!r} has {len(entry.values)} values "
                    f"but there are {len(self.categories)} categories"
                )
        return self


class ImageRef(StrictModel):
    """A picture to place in the document.

    Exactly one source must be set. ``file_id`` refers to a file already uploaded to
    OpenWebUI; ``data_base64`` carries the bytes inline for small images.
    """

    file_id: str | None = None
    data_base64: str | None = None
    alt_text: str | None = None
    width_cm: float | None = None
    height_cm: float | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> ImageRef:
        sources = [self.file_id, self.data_base64]
        if sum(source is not None for source in sources) != 1:
            raise ValueError("set exactly one of file_id or data_base64")
        return self


# --------------------------------------------------------------------------- #
# Render options (driven by the config GUI)
# --------------------------------------------------------------------------- #


class RenderOptions(StrictModel):
    template_id: str | None = Field(
        default=None,
        description="Template to build on. Call hive_inspect_template first to learn "
        "which layouts, styles and placeholders it offers.",
    )
    font_family: str | None = Field(
        default=None,
        description="Font name, e.g. 'Calibri'. Note this only writes the name into the "
        "file; the font must be installed on the machine that opens it.",
    )
    font_size_base: int | None = Field(
        default=None, ge=6, le=72, description="Base body font size in points."
    )
    theme_colors: dict[str, str] | None = Field(
        default=None,
        description="Hex colours without '#', keyed by role: accent1..accent6, "
        "background, text.",
    )
    language: str = Field(default="de", description="BCP-47 language tag, e.g. 'de' or 'en-US'.")
    audience: str | None = Field(
        default=None,
        description="Target audience, e.g. 'Vorstand' or 'technisches Team'. Only has an "
        "effect when generating from a brief rather than a full spec.",
    )
    target_length: int | None = Field(
        default=None,
        ge=1,
        le=200,
        description="Desired number of slides or pages. Only has an effect in brief mode.",
    )
    density: Literal["sparse", "normal", "dense"] = "normal"
    include_notes: bool = Field(default=False, description="Generate speaker notes (pptx only).")
    include_toc: bool = Field(
        default=False, description="Insert a table of contents field (docx only)."
    )
    page_size: Literal["A4", "Letter"] = "A4"
    orientation: Literal["portrait", "landscape"] = "portrait"
    filename: str | None = Field(
        default=None, description="Filename without extension. Derived from the title if omitted."
    )


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #

SlideLayout = Literal[
    "title",
    "title_content",
    "two_content",
    "section",
    "image",
    "table",
    "chart",
    "blank",
]


class Slide(StrictModel):
    layout: SlideLayout = Field(
        default="title_content",
        description="Which layout to use. 'title' for the opening slide, 'section' for "
        "dividers, 'title_content' for standard bullet slides.",
    )
    title: str | None = None
    subtitle: str | None = Field(default=None, description="Used by 'title' and 'section'.")
    bullets: list[Bullet] = Field(default_factory=list)
    bullets_right: list[Bullet] = Field(
        default_factory=list, description="Right-hand column for the 'two_content' layout."
    )
    body: str | None = Field(default=None, description="Free-text paragraph instead of bullets.")
    table: TableData | None = None
    chart: ChartData | None = None
    image: ImageRef | None = None
    notes: str | None = Field(default=None, description="Speaker notes.")
    placeholders: dict[str, str] = Field(
        default_factory=dict,
        description="Values for {{placeholders}} defined by the template, keyed by name "
        "without the braces.",
    )


class DeckSpec(StrictModel):
    title: str
    subtitle: str | None = None
    author: str | None = None
    slides: list[Slide] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Word document
# --------------------------------------------------------------------------- #


class Heading(StrictModel):
    type: Literal["heading"] = "heading"
    text: str
    level: int = Field(default=1, ge=1, le=6)


class Paragraph(StrictModel):
    type: Literal["paragraph"] = "paragraph"
    text: str
    style: Literal["normal", "quote", "caption", "intense"] = "normal"
    alignment: Literal["left", "center", "right", "justify"] = "left"


class BulletList(StrictModel):
    type: Literal["bullet_list"] = "bullet_list"
    items: list[Bullet]


class NumberedList(StrictModel):
    type: Literal["numbered_list"] = "numbered_list"
    items: list[Bullet]


class TableBlock(StrictModel):
    type: Literal["table"] = "table"
    data: TableData
    caption: str | None = None


class ImageBlock(StrictModel):
    type: Literal["image"] = "image"
    image: ImageRef
    caption: str | None = None


class PageBreak(StrictModel):
    type: Literal["page_break"] = "page_break"


class TableOfContents(StrictModel):
    type: Literal["toc"] = "toc"
    depth: int = Field(default=3, ge=1, le=6)


class CodeBlock(StrictModel):
    type: Literal["code"] = "code"
    text: str
    language: str | None = None


DocBlock = Annotated[
    Heading
    | Paragraph
    | BulletList
    | NumberedList
    | TableBlock
    | ImageBlock
    | PageBreak
    | TableOfContents
    | CodeBlock,
    Field(discriminator="type"),
]


class DocSpec(StrictModel):
    title: str
    subtitle: str | None = None
    author: str | None = None
    blocks: list[DocBlock] = Field(min_length=1)
    placeholders: dict[str, str] = Field(
        default_factory=dict, description="Values for {{placeholders}} in the template."
    )


# --------------------------------------------------------------------------- #
# Spreadsheet
# --------------------------------------------------------------------------- #

CellType = Literal["text", "number", "integer", "currency", "percent", "date", "formula", "bool"]


class Column(StrictModel):
    header: str
    key: str = Field(description="Key used to look this column up in each row object.")
    type: CellType = "text"
    number_format: str | None = Field(
        default=None,
        description="Explicit Excel number format, e.g. '#,##0.00 \"€\"'. Overrides `type`.",
    )
    width: float | None = Field(default=None, ge=1, le=255)


class ConditionalFormat(StrictModel):
    range: str = Field(description="A1-style range, e.g. 'C2:C100'.")
    rule: Literal["color_scale", "data_bar", "above_average", "below_average", "duplicates"]


class SheetChart(StrictModel):
    chart_type: Literal["bar", "column", "line", "pie", "scatter", "area"] = "column"
    title: str | None = None
    categories_range: str = Field(description="A1-style range for the category labels.")
    values_range: str = Field(description="A1-style range for the values, header row included.")
    anchor: str = Field(default="H2", description="Top-left cell the chart is anchored to.")


class Sheet(StrictModel):
    name: str = Field(max_length=31, description="Sheet name. Excel caps this at 31 characters.")
    columns: list[Column] = Field(min_length=1)
    rows: list[dict[str, str | float | int | bool | None]] = Field(default_factory=list)
    freeze_header: bool = True
    autofilter: bool = True
    conditional_formats: list[ConditionalFormat] = Field(default_factory=list)
    charts: list[SheetChart] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_name(self) -> Sheet:
        forbidden = set(r"[]:*?/\\")
        if forbidden & set(self.name):
            raise ValueError(r"sheet name must not contain any of []:*?/\ ")
        if not self.name.strip():
            raise ValueError("sheet name must not be empty")
        return self


class SheetSpec(StrictModel):
    title: str
    sheets: list[Sheet] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_sheet_names(self) -> SheetSpec:
        seen = {sheet.name.lower() for sheet in self.sheets}
        if len(seen) != len(self.sheets):
            raise ValueError("sheet names must be unique (case-insensitively)")
        return self


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Editing an existing document
# --------------------------------------------------------------------------- #
#
# Patches, not a round trip. An uploaded document is opened and specific things are
# changed; it is never parsed into a spec and re-rendered, because that would discard
# every piece of formatting the spec cannot express — which is most of them.
#
# Positions are 1-based throughout. That matches what `hive_read_document` prints and
# what render errors say ("slide 2 could not be rendered"), so a model never has to
# translate between two numbering schemes mid-task.


class ReplaceText(StrictModel):
    op: Literal["replace_text"] = "replace_text"
    find: str = Field(min_length=1, description="Exact text to look for.")
    replace: str = Field(description="What to put in its place. Empty string deletes it.")
    match_case: bool = True


class FillPlaceholders(StrictModel):
    op: Literal["fill_placeholders"] = "fill_placeholders"
    values: dict[str, str] = Field(
        description="Values for {{placeholders}}, keyed without the braces."
    )


class DeleteSlide(StrictModel):
    op: Literal["delete_slide"] = "delete_slide"
    slide: int = Field(ge=1, description="1-based slide number, as hive_read_document "
                       "reports it.")


class ReorderSlides(StrictModel):
    op: Literal["reorder_slides"] = "reorder_slides"
    order: list[int] = Field(
        min_length=1,
        description="The new order as 1-based slide numbers. Must list every slide "
        "exactly once.",
    )


class SetNotes(StrictModel):
    op: Literal["set_notes"] = "set_notes"
    slide: int = Field(ge=1)
    notes: str


class SetParagraph(StrictModel):
    op: Literal["set_paragraph"] = "set_paragraph"
    paragraph: int = Field(ge=1, description="1-based paragraph number from "
                           "hive_read_document.")
    text: str


class AppendParagraph(StrictModel):
    op: Literal["append_paragraph"] = "append_paragraph"
    text: str
    style: str | None = Field(
        default=None,
        description="A paragraph style the document defines, e.g. 'Heading 1'. "
        "hive_read_document lists them.",
    )


class SetCell(StrictModel):
    op: Literal["set_cell"] = "set_cell"
    sheet: str
    # The row part excludes a leading zero on purpose: "A0" parses fine as a coordinate
    # and then fails deep inside openpyxl, where the message says nothing useful.
    cell: str = Field(
        pattern=r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}$",
        description="A1 notation, e.g. 'B7'. Rows start at 1.",
    )
    value: str | float | int | bool | None
    type: CellType = "text"


EditOp = Annotated[
    ReplaceText
    | FillPlaceholders
    | DeleteSlide
    | ReorderSlides
    | SetNotes
    | SetParagraph
    | AppendParagraph
    | SetCell,
    Field(discriminator="op"),
]


class EditResult(StrictModel):
    """What an edit changed, and where it ended up."""

    file_id: str | None = None
    download_url: str | None = None
    download_markdown: str | None = Field(
        default=None,
        description="A ready-made markdown link. Include it verbatim in your reply so the "
        "user can click it, or call hive_show_download for a card with a real button.",
    )
    filename: str
    media_type: str
    size_bytes: int
    applied: list[str] = Field(
        description="One line per operation describing what it actually changed. Worth "
        "relaying: an operation can succeed while matching nothing."
    )
    warnings: list[str] = Field(default_factory=list)


class RenderResult(StrictModel):
    """What a generation tool hands back to the model."""

    file_id: str | None = Field(
        default=None, description="OpenWebUI file id, when delivery went through the Files API."
    )
    download_url: str | None = Field(
        default=None, description="Signed, expiring direct download URL."
    )
    download_markdown: str | None = Field(
        default=None,
        description="A ready-made markdown link. Include it verbatim in your reply so the "
        "user can click it: a URL inside a tool result is plain text, but your message is "
        "rendered as markdown. For a nicer card with a real button, call "
        "hive_show_download with the download_url instead.",
    )
    filename: str
    media_type: str
    size_bytes: int
    slide_count: int | None = None
    page_estimate: int | None = Field(
        default=None,
        description="Estimated page count. Word paginates at open time, so this is an "
        "estimate rather than a fact.",
    )
    sheet_names: list[str] | None = None
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal issues, e.g. a requested font that may not be installed "
        "on the reader's machine. Worth relaying to the user.",
    )


Bullet.model_rebuild()
