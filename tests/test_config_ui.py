"""The configuration GUI.

Most of this is markup, so the tests concentrate on what can actually go wrong: escaping,
because template names are chosen by users and land in this HTML; the sandbox constraints,
because getting them wrong produces a card that silently does nothing; and the theme and
language resolution, which is the only way this card can match the instance it renders in.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from hivemcp.app import create_app
from hivemcp.auth import Identity
from hivemcp.config import Settings
from hivemcp.core.models import DeckSpec, RenderOptions, Slide
from hivemcp.core.preferences import UserPreferences, parse_preferences
from hivemcp.core.render.pptx import render_presentation
from hivemcp.surfaces.config_ui import dark_css, js_literal, render_config_page

TEMPLATES = [
    {
        "template_id": "corporate-deck",
        "name": "Corporate Deck 2026",
        "kind": "pptx",
        "visibility": "global",
    },
    {"template_id": "brief", "name": "Briefvorlage", "kind": "docx", "visibility": "private"},
]

SESSION = {"Authorization": "Bearer session-token", "X-Hive-Chat-Id": "c-1"}

FORM_TAG = re.compile(r"<form\b[^>]*>")
SCRIPT_BLOCK = re.compile(r"<script>(.*?)</script>", re.DOTALL)
STYLE_BLOCK = re.compile(r"<style>(.*?)</style>", re.DOTALL)


class FakeValidator:
    def __init__(self, identity: Identity | None = None) -> None:
        self.identity = identity or Identity(user_id="u-1", role="admin")

    async def validate(self, token: str) -> Identity:
        return self.identity


class FakePreferences:
    def __init__(self, preferences: UserPreferences | None = None) -> None:
        self.preferences = preferences or UserPreferences()

    async def get(self, user_id: str, token: str) -> UserPreferences:
        return self.preferences


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        test_client.app.state.validator = FakeValidator()
        test_client.app.state.preferences = FakePreferences()
        yield test_client


# --------------------------------------------------------------------------- #
# Escaping
# --------------------------------------------------------------------------- #


def test_a_hostile_template_name_becomes_text_not_markup() -> None:
    """Template names come from whoever uploaded them and are rendered into this page."""
    hostile = [
        {
            "template_id": "evil",
            "name": "</option><script>alert(1)</script>",
            "kind": "pptx",
            "visibility": "private",
        }
    ]

    html = render_config_page("pptx", hostile, prefill_topic='"><script>alert(2)</script>')

    scripts = SCRIPT_BLOCK.findall(html)
    assert len(scripts) == 1, "only the page's own script block may exist"
    assert "alert" not in scripts[0], "nothing injected reached executable code"
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html, "it survives as visible text"


@pytest.mark.parametrize(
    "value",
    ['a"b', "</script><script>alert(1)</script>", "ü & <x>", "line\nbreak", "back\\slash"],
)
def test_js_literal_cannot_escape_the_script_block(value: str) -> None:
    """HTML autoescaping is the wrong tool inside <script>: an escaped quote arrives as
    the literal '&#34;' and breaks the script instead of protecting it. These values are
    escaped as JavaScript unicode sequences instead."""
    literal = str(js_literal(value))

    assert "</script>" not in literal
    assert "<" not in literal and ">" not in literal
    assert "&#" not in literal, "must not be HTML-escaped"


def test_js_literal_round_trips_through_json() -> None:
    import json

    for value in ['a"b', "</script>", "ü & <x>", "back\\slash"]:
        assert json.loads(str(js_literal(value))) == value


# --------------------------------------------------------------------------- #
# Sandbox constraints
# --------------------------------------------------------------------------- #


def test_the_card_uses_no_form_element() -> None:
    """`allow-forms` is off by default, so a form submission is blocked by the sandbox
    and the button would appear to do nothing."""
    html = render_config_page("pptx", TEMPLATES)

    assert not FORM_TAG.search(html)
    assert 'type="button"' in html


def test_the_card_reports_its_own_height() -> None:
    """Without this the iframe stays at a default height and the content is cut off."""
    html = render_config_page("pptx", TEMPLATES)

    assert "iframe:height" in html
    assert "ResizeObserver" in html


def test_values_leave_through_prompt_submission() -> None:
    """The only channel out of a cross-origin sandbox."""
    assert "input:prompt:submit" in render_config_page("pptx", TEMPLATES)


def test_the_background_is_transparent() -> None:
    """So the chat's own background shows through even when the theme is unknown."""
    assert "background: transparent" in render_config_page("pptx", TEMPLATES)


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #


def test_a_known_dark_instance_is_dark_regardless_of_the_operating_system() -> None:
    """A known theme is pinned, so the OS never gets a vote.

    Asserted against the generated rule from ``dark_css`` rather than against a substring
    of the whole page: the words "prefers-color-scheme" and "getComputedStyle" both
    appear in explanatory comments in the stylesheet and the script, so a naive
    ``not in html`` matches the prose and fails on correct output.
    """
    rules = dark_css("dark")

    assert "color-scheme: dark" in rules
    assert "@media" not in rules, "a known theme must not depend on a media query"


def test_a_known_light_instance_never_turns_dark_on_its_own() -> None:
    """Light stays light unless the user asks in the card.

    The dark variables are still emitted, but only behind ``[data-theme="dark"]`` — that
    is what keeps the manual toggle working on a light instance. What must not survive is
    any rule that could fire without that attribute.
    """
    rules = dark_css("light")

    assert "@media" not in rules
    assert '[data-theme="dark"]' in rules
    assert ':not([data-theme="light"])' not in rules


def test_the_card_accepts_both_schemes_so_the_embedder_decides() -> None:
    """This single declaration is the whole theme-matching mechanism.

    `prefers-color-scheme` inside an iframe reports the *embedding element's* colour
    scheme, cross-origin included, so the media query below asks OpenWebUI rather than
    the operating system. Declaring one scheme breaks it twice over: the query stops
    tracking the embedder, and CSS Color Adjust gives an embedded document whose used
    scheme differs from its embedder an opaque canvas — a white rectangle in a dark chat.
    """
    css = STYLE_BLOCK.search(render_config_page("pptx", TEMPLATES)).group(1)

    assert "color-scheme: light dark" in css
    assert not re.search(r"color-scheme:\s*light\s*;", css), "must not pin a single scheme"
    assert "@media (prefers-color-scheme: dark)" in css


def test_an_explicit_dark_request_pins_the_scheme_too() -> None:
    """Overriding the embedder means the canvas has to be told as well, or the form
    controls stay in the embedder's scheme."""
    css = STYLE_BLOCK.search(render_config_page("pptx", TEMPLATES, theme="dark")).group(1)

    assert "color-scheme: dark" in css


def test_the_toggle_reads_the_media_query_not_the_declaration() -> None:
    """With `color-scheme: light dark` the computed value is the declaration itself, not
    the scheme in use, so getComputedStyle would report 'light dark' and never flip."""
    script = SCRIPT_BLOCK.search(render_config_page("pptx", TEMPLATES)).group(1)
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("//")
    )

    assert "matchMedia('(prefers-color-scheme: dark)')" in code
    assert "getComputedStyle" not in code
    assert "root.style.colorScheme" in code, "an override must pin the scheme"


def test_an_explicit_theme_beats_the_stored_preference() -> None:
    html = render_config_page(
        "pptx", TEMPLATES, preferences=UserPreferences(theme="light"), theme="dark"
    )

    assert dark_css("dark") in html
    assert dark_css("light") not in html


@pytest.mark.parametrize(
    ("preferences", "theme"),
    [
        (UserPreferences(theme="dark"), "auto"),
        (UserPreferences(theme="light"), "auto"),
        (UserPreferences(), "auto"),
        (UserPreferences(), "light"),
        (UserPreferences(), "dark"),
    ],
)
def test_the_stylesheet_stays_valid_in_every_theme(
    preferences: UserPreferences, theme: str
) -> None:
    """The media-query variant needs an extra closing brace; an unbalanced stylesheet
    would take the rest of the page's styling with it."""
    css = STYLE_BLOCK.search(
        render_config_page("pptx", TEMPLATES, preferences=preferences, theme=theme)
    ).group(1)

    assert css.count("{") == css.count("}")


# --------------------------------------------------------------------------- #
# Language
# --------------------------------------------------------------------------- #


def test_english_is_the_default() -> None:
    """A general tool in the author's own language is worse than one in English."""
    html = render_config_page("pptx", TEMPLATES)

    assert 'lang="en"' in html
    assert "presentation settings" in html


def test_a_german_user_gets_german() -> None:
    html = render_config_page(
        "pptx", TEMPLATES, preferences=UserPreferences(locale="de-DE")
    )

    assert 'lang="de"' in html
    assert "Thema / Inhalt" in html


def test_an_untranslated_locale_falls_back_to_english() -> None:
    html = render_config_page("pptx", TEMPLATES, preferences=UserPreferences(locale="fr-FR"))

    assert 'lang="en"' in html


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("de-DE", "de"), ("de", "de"), ("DE_de", "de"),
        ("en-US", "en"), ("fr", "en"), ("ja", "en"), (None, "en"), ("", "en"),
        # Chinese resolves by script, not by primary subtag.
        ("zh", "zh-CN"), ("zh-CN", "zh-CN"), ("zh_CN", "zh-CN"),
        ("zh-Hans", "zh-CN"), ("zh-Hans-CN", "zh-CN"), ("zh-SG", "zh-CN"),
        ("zh-TW", "zh-TW"), ("zh_TW", "zh-TW"), ("zh-HK", "zh-TW"),
        ("zh-MO", "zh-TW"), ("zh-Hant", "zh-TW"), ("zh-Hant-HK", "zh-TW"),
    ],
)
def test_locale_maps_to_a_supported_language(locale: str | None, expected: str) -> None:
    assert UserPreferences(locale=locale).language == expected


def test_simplified_and_traditional_are_not_collapsed() -> None:
    """Two written forms, not regional spelling. Serving one to a reader of the other is
    a visible error, and taking only the primary subtag would do exactly that."""
    simplified = render_config_page(
        "pptx", TEMPLATES, preferences=UserPreferences(locale="zh-CN")
    )
    traditional = render_config_page(
        "pptx", TEMPLATES, preferences=UserPreferences(locale="zh-TW")
    )

    assert simplified != traditional
    assert "演示文稿设置" in simplified and "簡報" not in simplified
    assert "簡報設定" in traditional and "演示文稿" not in traditional


def test_every_language_defines_every_string() -> None:
    """A missing key would raise at render time, in whichever language nobody tested."""
    from hivemcp.surfaces.config_ui import STRINGS

    expected = set(STRINGS["en"])
    for language, table in STRINGS.items():
        assert set(table) == expected, f"{language} does not match the English key set"


@pytest.mark.parametrize("locale", ["zh-CN", "zh-TW"])
def test_chinese_cards_stay_structurally_intact(locale: str) -> None:
    html = render_config_page(
        "docx", TEMPLATES, preferences=UserPreferences(locale=locale, theme="dark")
    )
    css = STYLE_BLOCK.search(html).group(1)

    assert css.count("{") == css.count("}")
    assert len(SCRIPT_BLOCK.findall(html)) == 1
    assert f'lang="{locale}"' in html


def test_the_card_names_cjk_fonts_explicitly() -> None:
    """system-ui resolves to a Latin face on some platforms, which drops Chinese text to
    a fallback with visibly different metrics."""
    html = render_config_page("pptx", TEMPLATES, preferences=UserPreferences(locale="zh-CN"))

    assert "PingFang SC" in html
    assert "Microsoft YaHei" in html


def test_chinese_is_offered_as_a_document_language() -> None:
    html = render_config_page("pptx", TEMPLATES, preferences=UserPreferences(locale="zh-CN"))

    assert "简体中文" in html
    assert "繁體中文" in html
    assert 'value="zh-CN" selected' in html


@pytest.mark.parametrize(
    ("payload", "theme", "locale"),
    [
        ({"ui": {"theme": "dark", "locale": "de-DE"}}, "dark", "de-DE"),
        ({"theme": "oled-dark", "language": "en"}, "dark", "en"),
        ({"settings": {"ui": {"theme": "light", "lang": "fr"}}}, "light", "fr"),
        ({"ui": {"theme": "system"}}, None, None),
        ({"ui": {"theme": ""}}, None, None),
        ({}, None, None),
        ("not a dict", None, None),
        (None, None, None),
    ],
)
def test_preferences_are_found_wherever_openwebui_puts_them(
    payload: object, theme: str | None, locale: str | None
) -> None:
    """The settings document's shape has varied between versions, and 'system' means
    'follow the OS', which is not a theme this card can apply."""
    parsed = parse_preferences(payload)

    assert parsed.theme == theme
    assert parsed.locale == locale


# --------------------------------------------------------------------------- #
# Field set
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kind", "present", "absent"),
    [
        ("pptx", ["include_notes", "target_length", "density"], ["page_size", "include_toc"]),
        ("docx", ["include_toc", "page_size", "orientation"], ["include_notes"]),
        ("xlsx", ["template_id", "font_family"], ["target_length", "page_size"]),
    ],
)
def test_only_fields_that_apply_to_the_kind_are_shown(
    kind: str, present: list[str], absent: list[str]
) -> None:
    """Speaker notes on a spreadsheet form would be a lie about what happens next."""
    html = render_config_page(kind, TEMPLATES)

    for field in present:
        assert f'id="{field}"' in html, f"{field} should be offered for {kind}"
    for field in absent:
        assert f'id="{field}"' not in html, f"{field} should not be offered for {kind}"


def test_only_templates_of_the_matching_kind_are_offered() -> None:
    html = render_config_page("pptx", TEMPLATES)

    assert "corporate-deck" in html
    assert 'value="brief"' not in html


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #


def test_open_config_is_marked_for_inline_rendering(client: TestClient) -> None:
    """This header is the whole difference between a rendered card and printed markup."""
    response = client.get("/tools/open_config?kind=pptx", headers=SESSION)

    assert response.status_code == 200
    assert response.headers["content-disposition"] == "inline"
    assert "Content-Disposition" in response.headers["access-control-expose-headers"]
    assert response.text.lstrip().startswith("<!DOCTYPE html>")


def test_open_config_requires_a_session(client: TestClient) -> None:
    assert client.get("/tools/open_config").status_code == 401


def test_open_config_follows_the_users_openwebui_settings(client: TestClient) -> None:
    client.app.state.preferences = FakePreferences(
        UserPreferences(theme="dark", locale="de-DE")
    )

    html = client.get("/tools/open_config?kind=pptx", headers=SESSION).text

    assert dark_css("dark") in html
    assert 'lang="de"' in html


def test_open_config_still_renders_when_settings_cannot_be_read(
    client: TestClient,
) -> None:
    """A card in the wrong theme is cosmetic; a failed lookup must not block the dialog."""

    class Failing:
        async def get(self, user_id: str, token: str) -> UserPreferences:
            return UserPreferences()

    client.app.state.preferences = Failing()

    response = client.get("/tools/open_config?kind=pptx", headers=SESSION)

    assert response.status_code == 200
    assert "@media (prefers-color-scheme: dark)" in response.text


def test_open_config_lists_the_shared_templates(client: TestClient) -> None:
    """The form is built by an authenticated call, so it can show real templates rather
    than a free-text field the user has to guess into.

    The pool is shared and admin-curated: an ordinary user sees every template but put
    none of them there, so the fixture has to write as an administrator.
    """
    deck = render_presentation(
        DeckSpec(title="T", slides=[Slide(title="X")]), RenderOptions()
    ).data
    client.app.state.templates.store.put(
        deck,
        name="Meine Vorlage",
        filename="a.pptx",
        identity=Identity(user_id="admin-1", role="admin"),
    )

    html = client.get("/tools/open_config?kind=pptx", headers=SESSION).text

    assert "Meine Vorlage" in html
    assert "meine-vorlage" in html


def test_open_config_appears_in_the_tool_schema(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation_ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert "hive_open_config" in operation_ids
