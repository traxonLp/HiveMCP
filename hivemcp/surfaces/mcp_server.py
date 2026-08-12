"""MCP Streamable HTTP surface.

Runs stateless (``stateless_http=True``) so several replicas can sit behind one
Kubernetes Service without session affinity at the ingress. All state lives on the PVC
or in the request.

Targets MCP Python SDK **v2**. v2.0.0 (2026-07-28) renamed ``FastMCP`` to ``MCPServer``
and moved it from ``mcp.server.fastmcp`` to ``mcp.server.mcpserver``; the decorator API
itself is unchanged. The dependency carries a ``<3`` bound so the next major cannot
break the container at import time the way this one did.

Four things here are easy to get wrong and all are load-bearing:

1. **The session manager needs a lifespan.** ``mcp.session_manager.run()`` must be
   entered by the hosting app — a mounted sub-app's own lifespan never runs, so nothing
   else starts it. Without it the mount answers 404/500 and says nothing about why.
2. **The mount path must not be doubled.** ``streamable_http_path`` defaults to
   ``/mcp``; combined with ``app.mount("/mcp", ...)`` the endpoint would land at
   ``/mcp/mcp``.
3. **Transport parameters moved in v2.** ``stateless_http``, ``json_response`` and
   friends are no longer constructor arguments; they belong on ``streamable_http_app()``.
   Passing them to ``MCPServer()`` raises.
4. **``host`` decides DNS rebinding protection.** It defaults to ``127.0.0.1``, which
   makes a containerised server reject requests arriving as ``Host: hivemcp:8080``.

Note that OpenWebUI's UI event system, and therefore Rich UI iframe embedding, is not
available on this surface. That is why the OpenAPI surface exists alongside it.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from starlette.types import ASGIApp, Receive, Scope, Send

from ..auth import Identity, identity_from_headers, verify_bearer
from ..config import Settings
from ..core.models import DeckSpec, DocSpec, RenderOptions, SheetSpec
from ..core.render.base import RenderError
from ..core.service import DocumentService, ToolError

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
HiveMCP builds PowerPoint, Word and Excel files.

Prefer passing a complete `spec`: you write the content, this server renders it
deterministically. Keep bullets short, use the enum values the schema lists rather than
inventing layout or block names, and remember that unknown fields are rejected instead
of ignored so a validation error means the spec needs fixing, not retrying.

The result carries a `warnings` list. It is worth relaying to the user - for example
when a requested font is one their viewer may not have installed.\
"""


def _identity_from_context(ctx: Context | None) -> Identity:
    """Recover the caller from the underlying HTTP request.

    OpenWebUI fills X-Hive-* headers from its own template tokens. If anything about
    the transport changes and the request is not reachable, degrade to anonymous rather
    than failing the call: identity drives template visibility and quotas, not access.
    """
    try:
        request = ctx.request_context.request  # type: ignore[union-attr]
        headers = request.headers  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        logger.debug("no HTTP request on the MCP context; treating caller as anonymous")
        return Identity()
    return identity_from_headers(headers)


def _fail(exc: Exception) -> Exception:
    """Turn an internal error into one the model can act on."""
    return ValueError(str(exc))


def build_mcp_server(service: DocumentService) -> MCPServer:
    # Name stays positional, everything else by keyword: v2 inserted `title` and
    # `description` before `instructions`, so a positional second argument silently
    # lands in `title` and the instructions never reach the model.
    mcp = MCPServer("HiveMCP", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(
        name="hive_create_presentation",
        description=(
            "Create a PowerPoint (.pptx) file from a structured slide specification and "
            "deliver it to the chat. Write the slide content yourself and pass it as "
            "'spec'."
        ),
    )
    async def create_presentation(
        spec: DeckSpec | None = None,
        brief: str | None = None,
        options: RenderOptions | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        try:
            result = await service.create_presentation(
                _identity_from_context(ctx), options=options, spec=spec, brief=brief
            )
        except (ToolError, RenderError) as exc:
            raise _fail(exc) from exc
        return result.model_dump(exclude_none=True)

    @mcp.tool(
        name="hive_create_document",
        description=(
            "Create a Word (.docx) file from a structured list of blocks (headings, "
            "paragraphs, lists, tables, images) and deliver it to the chat."
        ),
    )
    async def create_document(
        spec: DocSpec | None = None,
        brief: str | None = None,
        options: RenderOptions | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        try:
            result = await service.create_document(
                _identity_from_context(ctx), options=options, spec=spec, brief=brief
            )
        except (ToolError, RenderError) as exc:
            raise _fail(exc) from exc
        return result.model_dump(exclude_none=True)

    @mcp.tool(
        name="hive_create_spreadsheet",
        description=(
            "Create an Excel (.xlsx) workbook from a structured sheet specification, "
            "including number formats, conditional formatting and charts."
        ),
    )
    async def create_spreadsheet(
        spec: SheetSpec | None = None,
        brief: str | None = None,
        options: RenderOptions | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        try:
            result = await service.create_spreadsheet(
                _identity_from_context(ctx), options=options, spec=spec, brief=brief
            )
        except (ToolError, RenderError) as exc:
            raise _fail(exc) from exc
        return result.model_dump(exclude_none=True)

    return mcp


class BearerAuthMiddleware:
    """Bearer check for the mounted MCP app.

    FastAPI dependencies do not reach into a mounted sub-application, so the same check
    the OpenAPI surface gets from ``require_caller`` is applied here as raw ASGI.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.settings.auth_enabled:
            await self.app(scope, receive, send)
            return

        header = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                header = value.decode("latin-1")
                break

        try:
            verify_bearer(header, self.settings)
        except Exception as exc:  # noqa: BLE001 - HTTPException or anything else
            detail = getattr(exc, "detail", "Unauthorized")
            body = f'{{"error":"unauthorized","detail":"{detail}"}}'.encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def build_mcp_asgi_app(mcp: MCPServer, settings: Settings) -> ASGIApp:
    app = mcp.streamable_http_app(
        # Serve at the mount root; app.py mounts this under /mcp.
        streamable_http_path="/",
        # No session affinity needed across replicas.
        stateless_http=True,
        # Plain JSON rather than an SSE stream. Nothing here streams partial results,
        # and JSON is far easier to debug through a proxy.
        json_response=True,
        # See point 4 in the module docstring: the SDK default of 127.0.0.1 turns on
        # DNS rebinding protection, which rejects container-internal Host headers.
        host=settings.mcp_host,
        # v2 caps request bodies at 4 MiB. A spec carrying a base64 image blows past
        # that quickly (base64 inflates by 4/3), so this follows our own upload limit.
        max_request_body_size=settings.max_upload_bytes,
    )
    return BearerAuthMiddleware(app, settings)
