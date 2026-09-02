"""Theme synchronisation shared by both Rich UI cards.

Two mechanisms, in that order, because neither is sufficient alone.

**Primary: read the parent's ``dark`` class.** OpenWebUI marks its own theme by putting
``class="dark"`` on ``<html>``. A card that reads that reads the *visibly active* theme,
including a manual choice, and a ``MutationObserver`` on the ``class`` attribute makes a
switch arrive without reloading the card.

**Fallback: ``prefers-color-scheme``.** ``parent.document`` throws when the iframe is
cross-origin or sandboxed without ``allow-same-origin``, and that is a normal
configuration rather than an error — so it is caught and nothing is reported to the user.
Inside an iframe the media query reports the *embedding element's* colour scheme, which
the CSS Working Group resolved deliberately, so it is a decent second answer.

Why the fallback is second and not first: it only reflects OpenWebUI when OpenWebUI
declares a ``color-scheme`` on the iframe element. When it does not, the query falls
through to the operating system — and someone running a dark OpenWebUI on a light desktop
then gets a white card in a dark chat. That is the exact failure this module exists to
remove.

The two must not fight. Once the parent has been read, the script records the answer in
``data-parent-theme`` and the media query is written to stand down whenever that attribute
is present. Without that, a parent correctly detected as *light* would still be painted
dark by a dark OS.
"""

from __future__ import annotations

from markupsafe import Markup

# Applied by both cards. Shared so the two never drift into looking different.
DARK_VARIABLES = """color-scheme: dark;
    --fg: #ececf1; --muted: #9ca3af; --border: #3f4147;
    --field-bg: #2a2b30; --accent: #6366f1; --accent-fg: #ffffff;"""

DOWNLOAD_DARK_VARIABLES = """color-scheme: dark;
    --fg: #ececf1; --muted: #9ca3af; --border: #3f4147;
    --surface: rgba(255,255,255,.06); --warn-bg: rgba(250,190,80,.12); --warn-fg: #f0c070;"""

# Markup, not a plain string: both cards render through an autoescaping environment, and
# an escaped apostrophe arrives as `&#39;` and breaks the script rather than protecting
# it. Same reason js_literal exists for values.
#
# ``var`` and ``function`` rather than ``const`` and arrows: this runs in whatever engine
# the reader's browser provides, and the card is not worth a transpiler.
PARENT_THEME_SYNC = Markup("""\
  // Theme, primary path: read OpenWebUI's own `dark` class off the parent document and
  // mirror it. That is the visibly active theme, including a manual override, which is
  // what `prefers-color-scheme` cannot see when OpenWebUI declares no color-scheme on
  // the iframe and the query falls through to the operating system.
  //
  // `data-parent-theme` records that this succeeded. The stylesheet's media query is
  // written to stand down when it is present — otherwise a parent correctly read as
  // *light* would still be painted dark by a dark OS.
  (function () {
    var root = document.documentElement;
    try {
      var parentRoot = parent.document.documentElement;
      var sync = function () {
        var dark = parentRoot.classList.contains('dark');
        root.classList.toggle('dark', dark);
        root.dataset.parentTheme = dark ? 'dark' : 'light';
      };
      sync();
      // Only the class attribute: OpenWebUI mutates its root element for many reasons,
      // and observing all of them would run this on every unrelated change.
      new MutationObserver(sync).observe(parentRoot, {
        attributes: true,
        attributeFilter: ['class']
      });
    } catch (e) {
      // Cross-origin or a sandbox without allow-same-origin. Expected, not a fault:
      // stay silent and let the CSS media query below decide.
    }
  })();
""")


def dark_css(variables: str = DARK_VARIABLES) -> Markup:
    """The dark rules: once for the synced class, once for the fallback.

    The media query is deliberately narrowed to ``:root:not([data-parent-theme])``. That
    single selector is what keeps the two mechanisms from contradicting each other: the
    moment the script has read the parent, the query stops having an opinion.
    """
    return Markup(
        f":root.dark {{ {variables} }}\n"
        f"  @media (prefers-color-scheme: dark) {{\n"
        f"    :root:not([data-parent-theme]) {{ {variables} }}\n"
        f"  }}"
    )


__all__ = ["DARK_VARIABLES", "DOWNLOAD_DARK_VARIABLES", "PARENT_THEME_SYNC", "dark_css"]
