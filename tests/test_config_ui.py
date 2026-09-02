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
from hivemcp.surfaces.config_ui import js_literal, render_config_page
from hivemcp.surfaces.download_ui import render_download_card
from hivemcp.surfaces.theme_ui import dark_css

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


def _card_script(html: str) -> str:
    return SCRIPT_BLOCK.search(html).group(1)


def _card_css(html: str) -> str:
    return STYLE_BLOCK.search(html).group(1)


CARDS = {
    "config": lambda: render_config_page("pptx", TEMPLATES),
    "download": lambda: render_download_card(
        url="https://example.test/d/abc", filename="Bericht.pptx", kind="pptx"
    ),
}


@pytest.mark.parametrize("card", list(CARDS))
def test_the_card_reads_the_parents_dark_class(card: str) -> None:
    """The primary mechanism, and the reason it is primary.

    OpenWebUI marks its theme with `class="dark"` on <html>. Reading that reads the
    *visibly active* theme, including a manual choice. `prefers-color-scheme` cannot:
    inside an iframe it reports the embedding element's colour scheme, and when OpenWebUI
    declares none it falls through to the operating system — so a dark OpenWebUI on a
    light desktop produced a white card in a dark chat.
    """
    script = _card_script(CARDS[card]())

    assert "parent.document.documentElement" in script
    assert "classList.contains('dark')" in script


@pytest.mark.parametrize("card", list(CARDS))
def test_a_theme_switch_arrives_without_a_reload(card: str) -> None:
    script = _card_script(CARDS[card]())

    assert "MutationObserver" in script
    # Only the class attribute: OpenWebUI mutates its root for many reasons, and watching
    # all of them would run the handler on every unrelated change.
    assert "attributeFilter: ['class']" in script


@pytest.mark.parametrize("card", list(CARDS))
def test_an_unreachable_parent_is_silent(card: str) -> None:
    """Cross-origin or a sandbox without allow-same-origin is a normal configuration.

    It must not surface as an error to the user, and it must not stop the rest of the
    script — the height reporting lives in the same block.
    """
    script = _card_script(CARDS[card]())

    assert "try {" in script
    assert "catch" in script
    assert "console.error" not in script
    assert "localStorage" not in script, "reading OpenWebUI's storage is a non-goal"


@pytest.mark.parametrize("card", list(CARDS))
def test_the_sync_script_is_not_html_escaped(card: str) -> None:
    """It travels through an autoescaping template.

    An escaped apostrophe arrives as `&#39;` and breaks the script rather than protecting
    it — the same trap that js_literal exists for.
    """
    script = _card_script(CARDS[card]())

    assert "&#39;" not in script
    assert "&#34;" not in script
    assert "&amp;" not in script


@pytest.mark.parametrize("card", list(CARDS))
def test_dark_rules_respond_to_the_synced_class(card: str) -> None:
    assert ":root.dark" in _card_css(CARDS[card]())


@pytest.mark.parametrize("card", list(CARDS))
def test_the_media_query_fallback_survives(card: str) -> None:
    assert "@media (prefers-color-scheme: dark)" in _card_css(CARDS[card]())


@pytest.mark.parametrize("card", list(CARDS))
def test_the_fallback_stands_down_once_the_parent_is_known(card: str) -> None:
    """The two mechanisms must not contradict each other.

    Without this selector a parent correctly read as *light* would still be painted dark
    by a dark operating system — the media query would simply overrule the better answer.
    """
    css = _card_css(CARDS[card]())

    assert ":root:not([data-parent-theme])" in css
    assert "@media (prefers-color-scheme: dark) {\n    :root {" not in css


@pytest.mark.parametrize("card", list(CARDS))
def test_the_root_accepts_both_schemes(card: str) -> None:
    """Declaring a single scheme is what breaks embedding.

    A document whose used scheme differs from its embedder gets an *opaque* canvas, so a
    card pinned to light becomes a white rectangle in a dark chat.
    """
    assert "color-scheme: light dark" in _card_css(CARDS[card]())


@pytest.mark.parametrize("card", list(CARDS))
def test_the_stylesheet_stays_balanced(card: str) -> None:
    css = _card_css(CARDS[card]())

    assert css.count("{") == css.count("}")


def test_no_toggle_button_is_rendered() -> None:
    html = render_config_page("pptx", TEMPLATES)

    assert "theme-toggle" not in html
    assert "toggle_theme" not in html


def test_the_theme_no_longer_depends_on_stored_preferences() -> None:
    """Two instances that differ only in the stored theme must render identically.

    OpenWebUI keeps the interface theme in the browser, so the server-side lookup was
    usually empty — and when it did answer, it froze the card at render time.
    """
    dark = render_config_page("pptx", TEMPLATES, preferences=UserPreferences(theme="dark"))
    light = render_config_page("pptx", TEMPLATES, preferences=UserPreferences(theme="light"))

    assert dark == light


def test_the_download_card_has_its_own_dark_values() -> None:
    """It defines --surface, --warn-bg and --warn-fg, which the config card does not.

    Both cards used to share one set of dark variables, so in dark mode the download
    card's warning box kept its light values: dark brown text on a dark panel.
    """
    css = _card_css(CARDS["download"]())
    dark_block = css.split(":root.dark")[1]

    assert "--warn-fg" in dark_block
    assert "--surface" in dark_block


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


def test_open_config_follows_the_users_openwebui_locale(client: TestClient) -> None:
    """Language still comes from settings. The theme deliberately no longer does."""
    client.app.state.preferences = FakePreferences(
        UserPreferences(theme="dark", locale="de-DE")
    )

    html = client.get("/tools/open_config?kind=pptx", headers=SESSION).text

    assert 'lang="de"' in html
    assert dark_css() in html


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
