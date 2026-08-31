<h1 align="center">HiveMCP</h1>

<p align="center">
  <img src="assets/HiveMCP_Banner.png" alt="HiveMCP">
</p>

<p align="center"><strong>1.0.4</strong></p>

---

Generates and edits PowerPoint, Word and Excel files for [OpenWebUI](https://openwebui.com).
Runs as a container, exposed both as an **MCP Streamable HTTP** server and as an
**OpenAPI tool server** from the same FastAPI app.

Why both: OpenWebUI renders inline iframes only for tool results carrying
`Content-Disposition: inline`, and that path exists for OpenAPI tool servers but not for
the native MCP surface. MCP gives protocol compatibility, OpenAPI gives the configuration
GUI.

## What it does

| Tool | Who can use it |
|---|---|
| `hive_create_presentation` · `hive_create_document` · `hive_create_spreadsheet` | everyone |
| `hive_read_document` · `hive_edit_document` — patch a file from the chat | everyone |
| `hive_show_download` — download card with a real button | everyone |
| `hive_list_templates` · `hive_inspect_template` | everyone |
| `hive_upload_template` · `hive_delete_template` | administrators |
| `hive_open_config` — settings card rendered inline in the chat | everyone |

Generated files are uploaded with the caller's own session token, so they appear in that
person's OpenWebUI file list, and a signed download link is attached as well.

## Status

| Milestone | Scope | State |
|---|---|---|
| M0 | Integration spikes: session auth, file ownership, Rich UI | **done** |
| M1 | Skeleton: config, auth, health, download, Dockerfile | **done** |
| M2 | Render core: pptx / docx / xlsx from a spec | **done** |
| M3 | MCP + OpenAPI surfaces, OpenWebUI file delivery | **done** |
| M4 | Templates: admin-curated pool, upload, inspect | **done** |
| M5 | Configuration GUI as an iframe | **done** |
| M6 | Editing files uploaded to the chat | **done** |
| M7 | Skill, K8s manifests, hardening | open |

## Quick start

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env

pytest
python -m hivemcp          # http://localhost:8080/healthz
```

## Docker

```bash
make up      # build, start, wait for healthy
make smoke   # verify it actually works
make logs
make down
```

`make owui` additionally starts an OpenWebUI instance on `http://localhost:3000`, which
is what the M0 integration spikes need.

Without make:

```bash
docker compose -f deploy/docker-compose.yml up --build -d --wait
./deploy/smoke.sh
```

Two things the compose file does deliberately:

- **Named volume, not a bind mount.** The container runs as uid 10001; a host directory
  would arrive owned by your user and every write would fail with `Permission denied`.
- **`HIVE_SIGNING_KEY` is fixed.** Left unset, each start generates a new key and every
  download link minted before the restart stops working.

## Connecting OpenWebUI

Both surfaces are added under **Admin Settings → Integrations → +**. Adding MCP servers
is admin-only; regular users may only add OpenAPI servers.

| | MCP | OpenAPI |
|---|---|---|
| Type | `MCP (Streamable HTTP)` | `OpenAPI` |
| URL | `http://hivemcp:8080/mcp/` | `http://hivemcp:8080` |
| Auth | **`Session`** | **`Session`** |

Use `http://host.docker.internal:8080` instead when OpenWebUI runs in a container and
HiveMCP does not.

**Auth must be `Session`.** HiveMCP has no shared secret and no service account: it
authenticates each caller by validating their own OpenWebUI session token, then reuses
that token against the Files API so generated documents belong to the person who asked
for them. Any other setting means no caller can be identified and every tool call is
refused.

Paste this into the connection's **Headers** field. These are context, not credentials —
the identity comes from the validated token:

```json
{
  "X-Hive-Chat-Id": "{{CHAT_ID}}"
}
```

`X-Hive-Chat-Id` becomes required as soon as `HIVE_LLM_ENABLED=true`: it is the only way
HiveMCP can find out which model you selected, since OpenWebUI offers no `{{MODEL}}`
token and `__model__` reaches native Python tools only.

Both surfaces can be registered at once. MCP gives protocol compatibility with other
clients; OpenAPI is the surface that can return Rich UI embeds, which the configuration
GUI needs in M5.

## Layout

```
hivemcp/
  app.py                    FastAPI factory: health, download, both surfaces
  config.py                 HIVE_* settings; refuses to start without OpenWebUI in prod
  auth.py                   session-token validation, HMAC-signed download links
  surfaces/
    mcp_server.py           MCP Streamable HTTP, stateless, mounted at /mcp
    openapi_tools.py        /tools/* with explicit operation ids
    config_ui.py            the settings card, rendered inline in the chat
    debug.py                dev-only diagnostics under /_debug
  core/
    service.py              shared logic: validation, render semaphore, delivery
    models.py               DeckSpec / DocSpec / SheetSpec / RenderOptions
    delivery.py             OpenWebUI upload plus a signed link
    preferences.py          interface theme and locale, and how to read them
    render/                 pptx.py, docx.py, xlsx.py, theme.py
    templates/              admin-curated pool, inspection, upload validation
    llm/                    brief expansion through the user's selected model
    files/owui_client.py    OpenWebUI Files API
    files/workdir.py        artifact store, with TTL sweep
deploy/                     Dockerfile, docker-compose.yml, smoke.sh, k8s/, PORTAINER.md
docs/                       OPENWEBUI_SETUP.md
tests/
```

The renderers know nothing about FastAPI or MCP. Both surfaces are thin adapters over
`core/`, which keeps them from drifting apart and makes the interesting logic testable
without HTTP.

## Design notes worth knowing

**Specs are strict.** Every model sets `extra="forbid"`. A model that invents a field gets
a validation error it can correct on the next turn rather than a silently dropped value
and a wrong-looking document.

**Untrusted text never becomes an Excel formula.** Cell values starting with `=`, `+`, `-`
or `@` are prefixed with `'` unless the column is explicitly typed `formula`. Spreadsheet
content originates from model output and uploaded documents, so this is the formula
injection boundary.

**Fonts are a name, not a file.** OOXML stores only the font name; if the reader's machine
lacks it, their viewer substitutes silently. Fonts outside the set that ships with Office
produce a warning on the render result.

**Page counts are estimates.** Word paginates at open time using the installed fonts and
printer driver, so no library can know the real count. The result field is called
`page_estimate` for that reason.

**Errors name their location.** A failure on slide 7 says so, because the model needs to
know where to fix the spec, not just what was wrong.

**The settings card matches OpenWebUI's theme through CSS, not through an API.** Nothing
tells it: postMessage carries no appearance message, `window.args` needs `allowSameOrigin`,
and OpenWebUI keeps the theme in the browser's localStorage rather than server-side. It
does not need to be told. `prefers-color-scheme` evaluated *inside an iframe* reports the
colour scheme of the embedding element, cross-origin included — resolved deliberately by
the CSS Working Group. The one thing that makes it work is declaring `color-scheme: light
dark`: pinning a single scheme both stops the query tracking the embedder and, per CSS
Color Adjust, replaces the transparent canvas with an opaque one, turning the card into a
white rectangle inside a dark chat. A toggle remains for instances that declare no scheme.

**Language comes from the model, not the server.** The interface locale is client-side too,
so `hive_open_config` takes a `language` argument and the tool description asks the model
to pass the language the conversation is in. English otherwise. Simplified and Traditional
Chinese are kept apart by script (`zh-Hant`, `zh-TW/HK/MO` → `zh-TW`) rather than collapsed
onto `zh`, because serving one to a reader of the other is a visible error.

**The settings card travels inside the tool result.** OpenWebUI embeds HTML returned by a
tool when the response carries `Content-Disposition: inline`, so no separate URL is
fetched and no signed link is needed. The card is fully sandboxed in return: nothing
inside can call back, so everything it needs is inlined at render time and the only way
out is `postMessage`.

**Templates are an admin-curated pool.** Administrators add and remove them; everyone
else lists, inspects and uses them. There are no private or per-group templates: a
template is a corporate design, and central curation is the point. That also removed a
question the earlier per-group layout could not answer, since group membership is not part
of the validated identity. The pool lives on its own volume (`HIVE_TEMPLATES_DIR`) because
it has the opposite lifecycle to rendered artifacts — rarely written, constantly read, and
worth keeping when the artifact volume is cleared.

**A URL in a tool result is not a link.** OpenWebUI renders tool results as JSON and only
the assistant's own message as markdown, so a `download_url` sitting in the result is
plain text. Every result therefore also carries `download_markdown`, a finished link the
model can paste, and `hive_show_download` turns it into a Rich UI card with a real button,
the file's size and any warnings that would otherwise be buried in the JSON. The button
opens in a new tab rather than using the `download` attribute: sandboxed iframes carry
`allow-downloads`, but triggering a download from inside one is unreliable and on iOS
impossible, so the server's `Content-Disposition: attachment` does the work instead.

**Editing patches, it does not rebuild.** `hive_edit_document` opens the uploaded file and
changes specific things in it. Parsing a document into a spec and re-rendering would be
simpler and would discard every piece of formatting the spec cannot express, which is most
of them. Two consequences worth knowing: operations are all-or-nothing, because a
half-applied edit hands back a file that looks finished and is not; and an operation that
matched nothing is reported rather than swallowed, because `replace_text` for a string
that does not occur succeeds trivially and changes nothing. The original file is never
modified — the edit comes back as a new upload.

**Uploads are identified by their contents.** An OpenWebUI file id carries no extension and
an uploaded filename is a claim, so the format is read from the zip's own directory
(`ppt/presentation.xml`, `word/document.xml`, `xl/workbook.xml`).

**Templates are inspected, not guessed at.** `hive_inspect_template` reports each layout
along with which value of the spec's `layout` enum it corresponds to, so a model does not
have to bridge the vocabulary gap itself. Uploaded templates are validated by reading
them, and rejected as archives first: an Office file is a zip, and an uploaded one is
untrusted input.

## Configuration

See [`.env.example`](.env.example). The container holds no third-party credentials:
`HIVE_OWUI_URL` and `HIVE_SIGNING_KEY` are all it needs.

- `HIVE_OWUI_URL` — required. Session tokens are validated here, so nothing can
  authenticate without it. It is a *server-to-server* URL: what works in your browser
  usually does not work from inside the container.
- `HIVE_SIGNING_KEY` — **must** be set explicitly when running more than one replica.
  Each pod otherwise generates its own key at start-up, and download links break whenever
  a request lands on a different pod than the one that signed the link.
- `HIVE_TEMPLATES_DIR` — the shared template pool. Defaults to a subdirectory of
  `HIVE_DATA_DIR`; give it its own volume so the curated templates survive clearing the
  artifact volume. See [`deploy/k8s/templates-pvc.yaml`](deploy/k8s/templates-pvc.yaml).
- `HIVE_ENVIRONMENT=dev` — mounts the diagnostic endpoints under `/_debug`. They reflect
  request headers back to the caller, so set `prod` anywhere that is reachable by others.

## Adding a template

Attach the file in the chat as an administrator and say what it should be called:

> Save this as a template called "Corporate Deck 2026"

HiveMCP validates it by opening it, stores it in the shared pool, and returns the same
report `hive_inspect_template` gives — layouts, styles and `{{placeholders}}` — so no
second call is needed. Everyone can then use it by passing `template_id` in
`RenderOptions`. Non-administrators get a 403 that says so rather than a validation error
they would try to fix by retrying.
