---
name: hivemcp-usage
title: Using HiveMCP to build Office and Markdown documents
description: How to pick the right HiveMCP tool and write a spec it renders correctly. Read this before generating a presentation, document, spreadsheet or Markdown file.
---

# Using HiveMCP

HiveMCP turns a **spec you write** into a real `.pptx`, `.docx`, `.xlsx` or `.md` file. You
supply the content; the server does the layout. It never invents content for a spec you
passed, and it never silently drops a field it does not understand — an unknown field is
a validation error you should fix and resend.

## Pick the tool

| The user wants | Call |
|---|---|
| slides, a deck, a presentation, `.pptx` | `hive_create_presentation` |
| a report, letter, memo, manual, `.docx` | `hive_create_document` |
| a table, workbook, budget, `.xlsx` | `hive_create_spreadsheet` |
| a README, release notes, `.md` | `hive_create_markdown` |
| to change a file they attached | `hive_read_document`, then `hive_edit_document` |
| to see the corporate templates | `hive_list_templates` |
| to choose fonts, template, length | `hive_open_config` |

**Write the file. Do not write the content into the chat instead.** If someone asks for
a presentation, a message containing slide text is not what they asked for. Call the
tool, then tell them briefly what you made and hand them the download link from
`download_markdown`.

## The two cards

Two tools return HTML that OpenWebUI renders as an interactive card. Return what they
give you unchanged — do not summarise it, quote it, or describe it. Both are for the
user to look at, not for you to read.

**`hive_open_config`** shows a settings form: font, size, template, audience, length,
page size. Call it when someone wants to *choose* how a document should look rather than
describe it — "settings", "options", "configure", "Einstellungen", or when they ask about
templates or fonts. Their choices come back as a new chat message, and only then do you
call a create tool.

Also worth offering when a request is vague about presentation and the answer would
change the result: *"Willst du Schrift und Vorlage vorher festlegen?"* is often better
than guessing. Do not open it for every request — someone who says "mach mir eine
Präsentation über Kaffee" wants a presentation, not a form.

**`hive_show_download`** turns the `download_url` from a create or edit result into a
real button. Call it straight afterwards. A URL inside a tool result is plain text the
user cannot click, and warnings on the result are easy to miss.

## Markdown

`hive_create_markdown` takes the **same `spec` as `hive_create_document`** — the same
blocks, the same rules. If you can build one you can build the other.

Which to choose: Markdown for a README, release notes, repository documentation, or a
post for a static-site generator. Word when the result will be printed, sent to someone
who does not read Markdown, or styled from a corporate template.

Three things behave differently because the format has no equivalent:

- **No fonts, sizes, page size or orientation.** Setting them produces a warning and
  nothing else. Markdown is plain text; the renderer that displays it decides.
- **A `toc` block writes the entries out** as links, rather than a field the reader's
  application fills in. `page_break` becomes a horizontal rule.
- **Images are embedded as data URIs**, because Markdown has nowhere to put a companion
  file. Large images make an unwieldy file and you get a warning saying so.

`options.frontmatter` adds a YAML block with title, author and language. Turn it on for a
static-site generator, leave it off for a plain README.

A Markdown template is a `.md` file with `{{placeholders}}` and one `{{content}}` marker
saying where the generated document goes. `hive_inspect_template` reports both, including
whether the marker is missing — without it the body is appended, which is rarely what the
author intended.

## Two ways to supply content

**`spec` — preferred.** You compose every heading, bullet and cell. Deterministic, and
you can see exactly what will be in the file.

**`brief` — a sentence describing the document.** The server expands it into a spec using
the model selected in the chat. Use this only when the user gave you nothing to work
from. A brief is a fallback, not a shortcut for you: if the user described what they
want in any detail, that detail belongs in a `spec`.

Pass one or the other, never both.

## Writing a good spec

Bullets are bullets, not paragraphs. A slide with four short lines lands; a slide with
four sentences does not.

```json
{"text": "Umsatz +12% im DACH-Raum"}
```

not

```json
{"text": "Im vergangenen Quartal konnten wir den Umsatz im DACH-Raum um zwölf Prozent steigern, was vor allem auf das Neukundengeschäft zurückzuführen ist."}
```

Put the long version in `notes` if it matters — that is what speaker notes are for.

Nest with `children`, at most three levels deep. Deeper nesting is flattened by
PowerPoint anyway and reads as noise.

### Presentation layouts

`layout` must be one of: `title`, `title_content`, `two_content`, `section`, `image`,
`table`, `chart`, `blank`. Each uses a different subset of the slide fields:

- `title` — `title`, `subtitle`
- `section` — `title`, `subtitle`
- `title_content` — `title`, `bullets` or `body`
- `two_content` — `title`, `bullets` **and** `bullets_right`
- `table` — `title`, `table`
- `chart` — `title`, `chart`
- `image` — `title`, `image`
- `blank` — `body`

Setting `table` on a `title_content` slide does nothing. Match the layout to the content.

### Document blocks

`blocks` is an ordered list, each with a `type`: `heading`, `paragraph`, `bullet_list`,
`numbered_list`, `table`, `image`, `page_break`, `toc`, `code`.

A `toc` block inserts a real Word field, so the entries appear once the reader opens the
document and lets Word update fields. It will look empty in a converted preview — that is
correct behaviour, not a broken render.

### Spreadsheet columns

Every column needs a `key`, and every row is an object looked up by those keys. A row
that omits a key leaves that cell empty rather than shifting the others along.

Set `type` per column — `number`, `integer`, `currency`, `percent`, `date`, `bool`,
`formula`, `text` — so the cells are real numbers and stay sortable. Numbers written as
text are the most common thing to get wrong here.

Text beginning with `=`, `+`, `-` or `@` is escaped unless the column type is `formula`.
That is deliberate: it stops content from being executed as a formula when the file is
opened.

## Templates

Templates are a shared, admin-curated pool. Everyone can use them; only administrators
add or remove them.

The order matters:

1. `hive_list_templates` — find the id
2. `hive_inspect_template` — see what the template actually offers
3. build the spec to match, then `hive_create_*` with `options.template_id`

Step 2 is not optional. `hive_inspect_template` reports the layout names the template
defines, the spec layout each one maps to, the paragraph styles available, and any
`{{placeholders}}` the author left behind. Guessing instead produces a document that
uses the template's colours but none of its structure.

Fill placeholders through `slide.placeholders` or `spec.placeholders`, keyed without the
braces: `{"kunde": "Muster GmbH"}` fills `{{kunde}}`.

### Adding a template (administrators only)

When an administrator attaches a `.pptx`, `.potx`, `.docx`, `.dotx`, `.xlsx` or `.xltx`
or `.md` and asks to keep it as a template, call `hive_upload_template` with the attached
`file_id` and a short, human name — "Corporate Deck 2026", not "template1". The name
becomes the id, so it is what everyone will type later. `hive_delete_template` removes
one.

Both refuse for non-administrators, and the refusal is final: the pool is shared on
purpose, so a normal user cannot add their own. Tell them to ask an administrator rather
than retrying.

## Editing attached files

`hive_read_document` first, always. It returns the slide numbers, paragraph numbers and
sheet names that `hive_edit_document` expects, and those numbers are the only reliable
way to target anything. Use `mode="outline"` unless you need full text.

Operations apply in order, all or nothing — if operation four is invalid, nothing is
written at all.

**Check the `applied` list in the result.** An operation can succeed and change nothing:
`replace_text` that matched no text is not an error, and the response says so in
`warnings`. Relay that rather than reporting a change that did not happen.

The original file is never modified. Edits come back as a new file.

Markdown is addressed by **line**, not by paragraph: `hive_read_document` returns
numbered lines and a heading outline, and `set_line` replaces one of them. Everything
else — `replace_text`, `fill_placeholders`, `append_paragraph` — works the same as for
Word.

Paragraph numbers count every paragraph including empty ones. In `outline` mode empty
paragraphs are omitted from the listing but still counted, so the numbers you see remain
the numbers you pass.

## Options worth setting

`language` drives typography, not translation — it does not translate your content.

`font_family` is warned about if it is not one that ships with Office. The document still
renders; the reader's viewer substitutes. If the text is Chinese, Japanese or Korean,
pick a font with CJK coverage or expect a second, unchosen typeface in the output.

`audience` and `target_length` only influence `brief` mode. They do nothing to a spec you
wrote yourself.

## Reading the result

- `download_markdown` — paste this into your reply; it is a working link
- `warnings` — relay these, they are written for the user, not for you
- `file_id` — the file in the user's OpenWebUI file list
- `slide_count`, `page_estimate`, `sheet_names` — for confirming what you made

`page_estimate` is an estimate from character count. Do not present it as a page count.

## When something fails

Validation errors name the field and are worth fixing precisely rather than retrying the
same call. Render errors name the slide or block number that failed. A template error
usually tells you to call `hive_list_templates`, which is generally the right next step.

If a tool reports that it needs an OpenWebUI chat session, the connection's
authentication is not set to Session. That is a configuration problem an administrator
has to fix; retrying will not help.
