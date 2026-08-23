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
from mcp.server.mcpserver.prompts import Prompt
from starlette.types import ASGIApp

from .. import __version__
from ..auth import AuthError, Caller, SessionValidator, authenticate
from ..config import Settings
from ..core.editing.read import ReadMode
from ..core.models import DeckSpec, DocSpec, EditOp, RenderOptions, SheetSpec
from ..core.render.base import RenderError
from ..core.service import DocumentService, ToolError
from ..core.skills import SkillError, SkillRegistry
from ..core.templates.service import TemplateService
from ..core.templates.store import TemplateError, TemplateKind

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


async def _caller_from_context(validator: SessionValidator, ctx: Context | None) -> Caller:
    """Authenticate the caller from the underlying HTTP request.

    No anonymous fallback: the session token is both the proof of identity and the
    credential used to act on the user's behalf, so a call without one cannot be served
    at all.
    """
    try:
        request = ctx.request_context.request  # type: ignore[union-attr]
        headers = request.headers  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            "This tool needs an OpenWebUI chat session, and no HTTP request context was "
            "available on this call."
        ) from exc
    try:
        return await authenticate(validator, headers)
    except AuthError as exc:
        raise ValueError(str(exc)) from exc


def _fail(exc: Exception) -> Exception:
    """Turn an internal error into one the model can act on."""
    return ValueError(str(exc))


def _register_skills(mcp: MCPServer, skills: SkillRegistry) -> None:
    """Expose every bundled skill as an MCP prompt and as one always-available tool.

    Neither is authenticated, and that is deliberate. These return documentation that
    ships inside the image — no user data, no side effects, nothing that varies by
    caller. Gating them behind a session token would make the guide unavailable in
    exactly the situation where it is most useful: a connection whose authentication is
    misconfigured, where every other tool is already failing and the model needs to be
    told why.

    A prompt returning a plain string becomes a single user message; that is the SDK's
    documented behaviour and avoids hand-building message objects here.
    """
    for skill in skills.all():
        def make(body: str, description: str):  # noqa: ANN202 - closure over the loop var
            def prompt() -> str:
                return body

            prompt.__doc__ = description
            return prompt

        mcp.add_prompt(
            Prompt.from_function(
                make(skill.body, skill.description),
                name=skill.name.replace("-", "_"),
                title=skill.title,
                description=skill.description,
            )
        )

    @mcp.tool(
        name="hive_usage_guide",
        description=(
            "Read the guide to using HiveMCP: which tool to call for which request, how "
            "to write a spec that renders correctly, how templates and editing work. "
            "Call this first if you are unsure how to build a document here, or if a "
            "call failed validation and you want to know the correct shape."
        ),
    )
    async def usage_guide(name: str | None = None) -> dict[str, Any]:
        if name is None:
            skill = skills.default
            if skill is None:
                raise ValueError("this server ships no usage guide")
        else:
            try:
                skill = skills.get(name)
            except SkillError as exc:
                raise _fail(exc) from exc
        return {"available": skills.names(), **skill.to_dict()}


def build_mcp_server(
    service: DocumentService,
    validator: SessionValidator,
    templates: TemplateService,
    skills: SkillRegistry | None = None,
) -> MCPServer:
    # Name stays positional, everything else by keyword: v2 inserted `title` and
    # `description` before `instructions`, so a positional second argument silently
    # lands in `title` and the instructions never reach the model.
    mcp = MCPServer("HiveMCP", instructions=INSTRUCTIONS, version=__version__)

    @mcp.tool(
        name="hive_create_presentation",
        description=(
            "Create a real PowerPoint file the user can download. Use this whenever "
            "someone asks for a presentation, slides, a deck or a .pptx - do not write "
            "the slides out as text instead. You compose the content and pass it as "
            "'spec'; this server renders the file."
        ),
    )
    async def create_presentation(
        spec: DeckSpec | None = None,
        brief: str | None = None,
        options: RenderOptions | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        try:
            caller = await _caller_from_context(validator, ctx)
            result = await service.create_presentation(
                caller, options=options, spec=spec, brief=brief
            )
        except (ToolError, RenderError) as exc:
            raise _fail(exc) from exc
        return result.model_dump(exclude_none=True)

    @mcp.tool(
        name="hive_create_document",
        description=(
            "Create a real Word file the user can download. Use this whenever someone "
            "asks for a document, report, letter, memo or a .docx rather than answering "
            "with the text itself. Content is a list of blocks: headings, paragraphs, "
            "lists, tables, images."
        ),
    )
    async def create_document(
        spec: DocSpec | None = None,
        brief: str | None = None,
        options: RenderOptions | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        try:
            caller = await _caller_from_context(validator, ctx)
            result = await service.create_document(
                caller, options=options, spec=spec, brief=brief
            )
        except (ToolError, RenderError) as exc:
            raise _fail(exc) from exc
        return result.model_dump(exclude_none=True)

    @mcp.tool(
        name="hive_create_spreadsheet",
        description=(
            "Create a real Excel file the user can download. Use this whenever someone "
            "asks for a spreadsheet, workbook or an .xlsx rather than printing a markdown "
            "table. Supports number formats, conditional formatting and charts."
        ),
    )
    async def create_spreadsheet(
        spec: SheetSpec | None = None,
        brief: str | None = None,
        options: RenderOptions | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        try:
            caller = await _caller_from_context(validator, ctx)
            result = await service.create_spreadsheet(
                caller, options=options, spec=spec, brief=brief
            )
        except (ToolError, RenderError) as exc:
            raise _fail(exc) from exc
        return result.model_dump(exclude_none=True)

    @mcp.tool(
        name="hive_read_document",
        description=(
            "Read a .pptx, .docx or .xlsx the user attached to this chat and report its "
            "structure. Call this before hive_edit_document: it gives you the slide "
            "numbers, paragraph numbers, sheet names and styles the edit operations "
            "refer to. Use mode='outline' unless you need the full text."
        ),
    )
    async def read_document(
        file_id: str, mode: ReadMode = "outline", ctx: Context | None = None
    ) -> dict[str, Any]:
        caller = await _caller_from_context(validator, ctx)
        try:
            return await service.read_document(caller, file_id, mode)
        except ToolError as exc:
            raise _fail(exc) from exc

    @mcp.tool(
        name="hive_edit_document",
        description=(
            "Change specific things in a document from the chat. The file is patched, "
            "not rebuilt, so its formatting survives, and the original is left alone. "
            "Operations are applied in order, all or nothing. Check the returned "
            "'applied' list: an operation can succeed while matching nothing."
        ),
    )
    async def edit_document(
        file_id: str,
        operations: list[EditOp],
        filename: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        caller = await _caller_from_context(validator, ctx)
        try:
            result = await service.edit_document(caller, file_id, operations, filename)
        except ToolError as exc:
            raise _fail(exc) from exc
        return result.model_dump(exclude_none=True)

    @mcp.tool(
        name="hive_list_templates",
        description=(
            "List the shared document templates, optionally filtered by kind ('pptx', "
            "'docx' or 'xlsx'). Everyone can use these; only administrators add them."
        ),
    )
    async def list_templates(
        kind: TemplateKind | None = None, ctx: Context | None = None
    ) -> dict[str, Any]:
        caller = await _caller_from_context(validator, ctx)
        return {"templates": templates.list(caller, kind)}

    @mcp.tool(
        name="hive_inspect_template",
        description=(
            "Report a template's layouts, styles and {{placeholders}} so a spec can be "
            "built to match it. Call this before generating with a template: it tells "
            "you which layout names exist and which spec layout each one maps to."
        ),
    )
    async def inspect_template(
        template_id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        caller = await _caller_from_context(validator, ctx)
        try:
            return templates.inspect(caller, template_id)
        except TemplateError as exc:
            raise _fail(exc) from exc

    @mcp.tool(
        name="hive_upload_template",
        description=(
            "Save a file the user attached to this chat as a reusable template. "
            "Administrators only: templates are a shared pool everyone can use. Returns "
            "the same report as hive_inspect_template."
        ),
    )
    async def upload_template(
        file_id: str,
        name: str,
        filename: str | None = None,
        description: str | None = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        caller = await _caller_from_context(validator, ctx)
        try:
            return await templates.upload_from_chat(
                caller,
                file_id=file_id,
                name=name,
                filename=filename,
                description=description,
            )
        except TemplateError as exc:
            raise _fail(exc) from exc

    @mcp.tool(
        name="hive_delete_template",
        description="Delete a template from the shared pool. Administrators only.",
    )
    async def delete_template(
        template_id: str, ctx: Context | None = None
    ) -> dict[str, Any]:
        caller = await _caller_from_context(validator, ctx)
        try:
            templates.delete(caller, template_id)
        except TemplateError as exc:
            raise _fail(exc) from exc
        return {"deleted": template_id}

    _register_skills(mcp, skills or SkillRegistry())

    return mcp


def build_mcp_asgi_app(mcp: MCPServer, settings: Settings) -> ASGIApp:
    # No auth middleware here. Authentication happens inside each tool, because the
    # session token is not only a gate but the credential the tool acts with, so it has
    # to reach the handler rather than be checked and discarded at the edge.
    return mcp.streamable_http_app(
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
