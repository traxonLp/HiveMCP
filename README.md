# HiveMCP

Generates and edits PowerPoint, Word and Excel files for [OpenWebUI](https://openwebui.com).
Runs as a container, exposed both as an **MCP Streamable HTTP** server and as an
**OpenAPI tool server** from the same FastAPI app.

Why both: OpenWebUI renders inline iframes only for tool results carrying
`Content-Disposition: inline`, and that path exists for OpenAPI tool servers but not for
the native MCP surface. MCP gives protocol compatibility, OpenAPI gives the configuration
GUI. See [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for the full
reasoning and the milestone plan.

## Status

| Milestone | Scope | State |
|---|---|---|
| M0 | Integration spikes (Files-API ownership, Rich-UI render, OWUI version) | offen |
| M1 | Skeleton: config, auth, health, download, Dockerfile | **fertig** |
| M2 | Render core: pptx / docx / xlsx from a spec | **fertig** |
| M3 | MCP + OpenAPI surfaces, OpenWebUI file delivery | **fertig** |
| M4 | Templates: upload, inspect, fill | offen |
| M5 | Configuration GUI as an iframe | offen |
| M6 | Editing files uploaded to the chat | offen |
| M7 | Skill, K8s manifests, hardening | offen |

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
| URL | `http://hivemcp:8080/mcp` | `http://hivemcp:8080` |
| Auth | `None` in dev, otherwise `Bearer` | same |

Use `http://host.docker.internal:8080` instead when OpenWebUI runs in a container and
HiveMCP does not. Set **Auth** to `None` unless you configured `HIVE_AUTH_TOKEN`:
choosing `Bearer` without filling in the key sends an empty `Authorization` header, which
most servers reject outright.

Paste this into the connection's **Headers** field so tool calls carry the caller's
identity. OpenWebUI expands the tokens server-side:

```json
{
  "X-Hive-User-Id": "{{USER_ID}}",
  "X-Hive-User-Email": "{{USER_EMAIL}}",
  "X-Hive-Groups": "{{USER_GROUPS}}",
  "X-Hive-Chat-Id": "{{CHAT_ID}}"
}
```

Both can be registered at once. MCP gives protocol compatibility with other clients;
OpenAPI is the surface that can return Rich UI embeds, which the configuration GUI needs
in M5.

## Layout

```
hivemcp/
  app.py                  FastAPI factory: health, download, both surfaces
  config.py               HIVE_* settings; refuses to start unauthenticated in prod
  auth.py                 bearer check, identity headers, HMAC-signed UI links
  surfaces/
    mcp_server.py         MCP Streamable HTTP, stateless, mounted at /mcp
    openapi_tools.py      /tools/* with explicit operation ids
  core/
    service.py            shared logic: validation, render semaphore, delivery
    models.py             DeckSpec / DocSpec / SheetSpec / RenderOptions
    delivery.py           signed URL, OpenWebUI upload, composite with fallback
    render/               pptx.py, docx.py, xlsx.py, theme.py
    files/owui_client.py  OpenWebUI Files API
    files/workdir.py      artifact store on the PVC, with TTL sweep
deploy/                   Dockerfile, docker-compose.yml, smoke.sh
docs/IMPLEMENTATION_PLAN.md
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

## Configuration

See [`.env.example`](.env.example). Two settings matter more than they look:

- `HIVE_SIGNING_KEY` — **must** be set explicitly when running more than one replica.
  Each pod otherwise generates its own key at start-up, and download links break whenever
  a request lands on a different pod than the one that signed the link.
- `HIVE_ENVIRONMENT=prod` — makes the service refuse to start without an auth token and
  OpenWebUI credentials, rather than coming up silently unauthenticated.
