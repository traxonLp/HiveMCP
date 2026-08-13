"""Template storage and inspection.

Two things get the most attention here. Path safety, because template ids come from a
model and reach the filesystem. And the layout mapping, because it is what lets a model
connect a template's own vocabulary to the spec's `layout` enum — an earlier version
mislabelled every placeholder type and mapped "Title and Content" to a two-column
layout, which no test would have caught by checking that a file was produced.
"""

from __future__ import annotations

import zipfile

import pytest

from hivemcp.auth import Identity
from hivemcp.core.models import Bullet, DeckSpec, DocSpec, Heading, RenderOptions, Slide
from hivemcp.core.render.docx import render_document
from hivemcp.core.render.pptx import render_presentation
from hivemcp.core.templates.inspect import (
    TemplateUnreadable,
    assert_safe_archive,
    find_placeholders,
    inspect_template,
    placeholder_type_name,
)
from hivemcp.core.templates.store import (
    TemplateError,
    TemplateStore,
    kind_for,
    slugify,
)

ALICE = Identity(user_id="u-alice", role="user")
BOB = Identity(user_id="u-bob", role="user")
ADMIN = Identity(user_id="u-admin", role="admin")


@pytest.fixture
def store(tmp_path) -> TemplateStore:
    return TemplateStore(tmp_path / "templates")


@pytest.fixture
def deck_bytes() -> bytes:
    spec = DeckSpec(
        title="Corporate",
        slides=[
            Slide(layout="title", title="{{kunde}}", subtitle="{{datum}}"),
            Slide(layout="title_content", title="Agenda", bullets=[Bullet(text="{{thema}}")]),
        ],
    )
    return render_presentation(spec, RenderOptions()).data


@pytest.fixture
def doc_bytes() -> bytes:
    spec = DocSpec(title="Angebot", blocks=[Heading(text="Fuer {{kunde}}", level=1)])
    return render_document(spec, RenderOptions()).data


# --------------------------------------------------------------------------- #
# Store: identity and visibility
# --------------------------------------------------------------------------- #


def test_private_templates_are_not_visible_to_others(
    store: TemplateStore, deck_bytes: bytes
) -> None:
    meta = store.put(deck_bytes, name="Alice Deck", filename="a.pptx", identity=ALICE)

    assert [item.template_id for item in store.list(ALICE)] == [meta.template_id]
    assert store.list(BOB) == []
    with pytest.raises(TemplateError, match="no template"):
        store.get(meta.template_id, BOB)


def test_global_templates_are_visible_to_everyone(
    store: TemplateStore, deck_bytes: bytes
) -> None:
    meta = store.put(
        deck_bytes, name="Corp", filename="a.pptx", identity=ADMIN, visibility="global"
    )
    assert [item.template_id for item in store.list(BOB)] == [meta.template_id]


def test_only_admins_publish_globally(store: TemplateStore, deck_bytes: bytes) -> None:
    with pytest.raises(TemplateError, match="administrators"):
        store.put(
            deck_bytes, name="Corp", filename="a.pptx", identity=ALICE, visibility="global"
        )


def test_you_cannot_delete_someone_elses_template(
    store: TemplateStore, deck_bytes: bytes
) -> None:
    meta = store.put(deck_bytes, name="Alice Deck", filename="a.pptx", identity=ALICE)
    store.put(deck_bytes, name="Bob Deck", filename="b.pptx", identity=BOB)

    with pytest.raises(TemplateError, match="no template"):
        store.delete(meta.template_id, BOB)
    store.delete(meta.template_id, ALICE)
    assert store.list(ALICE) == []


def test_global_templates_sort_first(store: TemplateStore, deck_bytes: bytes) -> None:
    """A shared corporate design is the more likely intent than a personal one."""
    store.put(deck_bytes, name="Zzz Personal", filename="a.pptx", identity=ADMIN)
    store.put(
        deck_bytes, name="Aaa Corp", filename="b.pptx", identity=ADMIN, visibility="global"
    )
    assert [item.visibility for item in store.list(ADMIN)] == ["global", "private"]


def test_duplicate_names_get_distinct_ids(store: TemplateStore, deck_bytes: bytes) -> None:
    first = store.put(deck_bytes, name="Deck", filename="a.pptx", identity=ALICE)
    second = store.put(deck_bytes, name="Deck", filename="a.pptx", identity=ALICE)
    assert first.template_id != second.template_id
    assert store.get(second.template_id, ALICE).path.exists()


# --------------------------------------------------------------------------- #
# Store: path safety
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "template_id", ["../global/x", "a/b", "..", "", "A-Upper", "-leading", "x" * 70]
)
def test_ids_that_could_escape_the_root_are_rejected_by_shape(
    store: TemplateStore, template_id: str
) -> None:
    """Rejected on shape rather than sanitised: an id that has to be cleaned up is an id
    that was never valid."""
    with pytest.raises(TemplateError, match="not a valid template id"):
        store.get(template_id, ALICE)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Corporate Deck 2026", "corporate-deck-2026"),
        ("Angebot für Kunden GmbH", "angebot-fur-kunden-gmbh"),
        ("../../etc/passwd", "etc-passwd"),
        ("   ...   ", "template"),
        ("ÄÖÜ Vorlage", "aou-vorlage"),
    ],
)
def test_slugify_produces_readable_and_safe_ids(name: str, expected: str) -> None:
    assert slugify(name) == expected


def test_a_hostile_user_id_cannot_escape_its_directory(
    store: TemplateStore, deck_bytes: bytes
) -> None:
    hostile = Identity(user_id="../../../etc", role="user")
    meta = store.put(deck_bytes, name="Deck", filename="a.pptx", identity=hostile)

    stored = store.get(meta.template_id, hostile).path.resolve()
    assert store.root.resolve() in stored.parents


@pytest.mark.parametrize("filename", ["a.txt", "a.pdf", "a", "a.pptx.exe"])
def test_unsupported_extensions_are_refused(filename: str) -> None:
    with pytest.raises(TemplateError, match="not a supported template type"):
        kind_for(filename)


@pytest.mark.parametrize(
    ("filename", "kind"),
    [("a.pptx", "pptx"), ("a.potx", "pptx"), ("a.dotx", "docx"), ("a.xltx", "xlsx")],
)
def test_template_variants_are_accepted_too(filename: str, kind: str) -> None:
    """People usually have a normal document to hand, not the .potx variant."""
    assert kind_for(filename) == kind


# --------------------------------------------------------------------------- #
# Inspection: archive safety
# --------------------------------------------------------------------------- #


def test_zip_bomb_is_refused(tmp_path) -> None:
    """An uploaded template is an untrusted archive, and python-pptx expands whatever it
    is given, so the check has to happen before any parser sees it."""
    bomb = tmp_path / "bomb.pptx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.xml", b"\0" * (50 * 1024 * 1024))

    with pytest.raises(TemplateUnreadable, match="decompression bomb"):
        assert_safe_archive(bomb)


def test_too_many_members_is_refused(tmp_path) -> None:
    many = tmp_path / "many.pptx"
    with zipfile.ZipFile(many, "w") as archive:
        for index in range(6000):
            archive.writestr(f"f{index}.xml", b"x")

    with pytest.raises(TemplateUnreadable, match="entries"):
        assert_safe_archive(many)


def test_a_file_that_is_not_a_zip_is_refused(tmp_path) -> None:
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"definitely not a zip")

    with pytest.raises(TemplateUnreadable, match="not a valid Office document"):
        assert_safe_archive(broken)


def test_the_wrong_kind_fails_with_a_readable_message(tmp_path, deck_bytes: bytes) -> None:
    path = tmp_path / "a.pptx"
    path.write_bytes(deck_bytes)

    with pytest.raises(TemplateUnreadable, match="could not be read as a docx"):
        inspect_template(path, "docx")


# --------------------------------------------------------------------------- #
# Inspection: what the model is told
# --------------------------------------------------------------------------- #


def test_pptx_layouts_map_onto_the_spec_enum(tmp_path, deck_bytes: bytes) -> None:
    """The mapping is what lets a model translate a template's vocabulary into the spec's.

    Regression guard: an earlier version derived this from placeholder counts using a
    hand-copied type table, and mapped 'Title and Content' to 'two_content' — so every
    ordinary content slide would have been filled as two columns.
    """
    path = tmp_path / "a.pptx"
    path.write_bytes(deck_bytes)

    report = inspect_template(path, "pptx")
    by_name = {item["name"]: item["maps_to_spec_layout"] for item in report["layouts"]}

    assert by_name["Title Slide"] == "title"
    assert by_name["Title and Content"] == "title_content"
    assert by_name["Two Content"] == "two_content"
    assert by_name["Section Header"] == "section"
    assert by_name["Blank"] == "blank"
    assert by_name["Picture with Caption"] == "image"


def test_placeholder_types_come_from_the_enum_not_a_copied_table(
    tmp_path, deck_bytes: bytes
) -> None:
    path = tmp_path / "a.pptx"
    path.write_bytes(deck_bytes)

    report = inspect_template(path, "pptx")
    title_layout = next(item for item in report["layouts"] if item["name"] == "Title Slide")
    types = {item["type"] for item in title_layout["placeholders"]}

    assert "center_title" in types
    assert "subtitle" in types


def test_pptx_inspection_reports_placeholders_and_size(tmp_path, deck_bytes: bytes) -> None:
    path = tmp_path / "a.pptx"
    path.write_bytes(deck_bytes)

    report = inspect_template(path, "pptx")

    assert report["placeholders"] == ["datum", "kunde", "thema"]
    assert report["slide_size"]["width_cm"] > 0


def test_docx_inspection_reports_styles_the_renderer_relies_on(
    tmp_path, doc_bytes: bytes
) -> None:
    """The renderer falls back with a warning when a list style is missing, so a caller
    should be able to see that coming."""
    path = tmp_path / "a.docx"
    path.write_bytes(doc_bytes)

    report = inspect_template(path, "docx")

    assert "List Bullet" in report["list_styles_present"]
    assert "Heading 1" in report["styles"]["paragraph"]
    assert report["placeholders"] == ["kunde"]
    assert report["page"]["orientation"] == "portrait"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{{a}}", ["a"]),
        ("{{ b }}", ["b"]),
        ("{{c.d}}", ["c.d"]),
        ("{{e-f}}", ["e-f"]),
        ("{{}}", []),
        ("{{ unclosed", []),
        ("{{" + "y" * 80 + "}}", []),
        ("twice {{z}} and {{z}}", ["z"]),
    ],
)
def test_placeholder_pattern(text: str, expected: list[str]) -> None:
    assert find_placeholders([text]) == expected


def test_placeholder_type_name_survives_an_unknown_type() -> None:
    class Fake:
        name = "VERTICAL_BODY"

    assert placeholder_type_name(Fake()) == "vertical_body"
    assert placeholder_type_name("SOMETHING (99)") == "something"
