"""OpenAPI tool-server surface.

OpenWebUI ingests ``/openapi.json`` and turns every operation into a tool. This is also
the surface that can return Rich UI embeds (``Content-Disposition: inline``), which the
native MCP path cannot — so the configuration GUI will land here in M5.

Operation ids are set explicitly: OpenWebUI derives the tool name the model sees from
them, and FastAPI's generated ids (``create_presentation_tools_create_presentation_post``)
would waste context and read badly in the UI.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..auth import Caller, require_caller
from ..core.models import DeckSpec, DocSpec, RenderOptions, RenderResult, SheetSpec
from ..core.service import DocumentService, ToolError
from ..core.templates.service import TemplateService
from ..core.templates.store import NotPermitted, TemplateError, TemplateKind
from .config_ui import ConfigKind, LanguageChoice, ThemeChoice, render_config_page

router = APIRouter(prefix="/tools", tags=["documents"])


def get_service(request: Request) -> DocumentService:
    return request.app.state.service


def get_templates(request: Request) -> TemplateService:
    return request.app.state.templates


ServiceDep = Annotated[DocumentService, Depends(get_service)]
TemplatesDep = Annotated[TemplateService, Depends(get_templates)]
CallerDep = Annotated[Caller, Depends(require_caller)]


class PresentationRequest(BaseModel):
    spec: DeckSpec | None = Field(
        default=None, description="Full presentation content. Preferred over 'brief'."
    )
    brief: str | None = Field(
        default=None,
        description="Short description to expand into slides. Only works if this server "
        "has LLM expansion enabled; otherwise pass 'spec'.",
    )
    options: RenderOptions = Field(default_factory=RenderOptions)


class DocumentRequest(BaseModel):
    spec: DocSpec | None = Field(default=None, description="Full document content.")
    brief: str | None = None
    options: RenderOptions = Field(default_factory=RenderOptions)


class SpreadsheetRequest(BaseModel):
    spec: SheetSpec | None = Field(default=None, description="Full workbook content.")
    brief: str | None = None
    options: RenderOptions = Field(default_factory=RenderOptions)


class UploadTemplateRequest(BaseModel):
    file_id: str = Field(
        description="Id of a file the user attached to this chat. Ask the user to "
        "attach the template if you do not have one."
    )
    name: str = Field(description="Display name, e.g. 'Corporate Deck 2026'.")
    filename: str | None = Field(
        default=None,
        description="Original filename, used to determine the type. Required if the "
        "name has no extension.",
    )
    description: str | None = None


# The literal rather than a Starlette constant: the name for this code changed from
# HTTP_422_UNPROCESSABLE_ENTITY to HTTP_422_UNPROCESSABLE_CONTENT, and using either one
# emits a deprecation warning on the other side of that rename. The number does not move.
UNPROCESSABLE = 422


def _translate(exc: ToolError) -> HTTPException:
    # 422 rather than 400 or 500: the request was well-formed HTTP but semantically
    # wrong, and the model can fix it from the message and retry.
    return HTTPException(UNPROCESSABLE, str(exc))


def _translate_template(exc: TemplateError) -> HTTPException:
    # 403 for a permission problem: it tells the model this is not something to fix by
    # retrying with different arguments, unlike the 422 every other template error gets.
    if isinstance(exc, NotPermitted):
        return HTTPException(403, str(exc))
    return HTTPException(UNPROCESSABLE, str(exc))


@router.post(
    "/create_presentation",
    operation_id="hive_create_presentation",
    summary="Create a PowerPoint presentation",
    response_model=RenderResult,
)
async def create_presentation(
    body: PresentationRequest, service: ServiceDep, caller: CallerDep
) -> RenderResult:
    """Build a .pptx file from a structured slide specification and return it to the chat."""
    try:
        return await service.create_presentation(
            caller, options=body.options, spec=body.spec, brief=body.brief
        )
    except ToolError as exc:
        raise _translate(exc) from exc


@router.post(
    "/create_document",
    operation_id="hive_create_document",
    summary="Create a Word document",
    response_model=RenderResult,
)
async def create_document(
    body: DocumentRequest, service: ServiceDep, caller: CallerDep
) -> RenderResult:
    """Build a .docx file from a structured block list and return it to the chat."""
    try:
        return await service.create_document(
            caller, options=body.options, spec=body.spec, brief=body.brief
        )
    except ToolError as exc:
        raise _translate(exc) from exc


@router.post(
    "/create_spreadsheet",
    operation_id="hive_create_spreadsheet",
    summary="Create an Excel workbook",
    response_model=RenderResult,
)
async def create_spreadsheet(
    body: SpreadsheetRequest, service: ServiceDep, caller: CallerDep
) -> RenderResult:
    """Build a .xlsx file from a structured sheet specification and return it to the chat."""
    try:
        return await service.create_spreadsheet(
            caller, options=body.options, spec=body.spec, brief=body.brief
        )
    except ToolError as exc:
        raise _translate(exc) from exc


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


@router.get(
    "/list_templates",
    operation_id="hive_list_templates",
    summary="List the document templates available to you",
)
async def list_templates(
    templates: TemplatesDep,
    caller: CallerDep,
    kind: TemplateKind | None = None,
) -> dict[str, object]:
    """List the shared templates, optionally filtered by kind (pptx, docx, xlsx).

    Everyone can use these; only administrators can add or remove them.
    """
    return {"templates": templates.list(caller, kind)}


@router.get(
    "/inspect_template/{template_id}",
    operation_id="hive_inspect_template",
    summary="Read a template's layouts, styles and placeholders",
)
async def inspect_template(
    template_id: str, templates: TemplatesDep, caller: CallerDep
) -> dict[str, object]:
    """Report what a template offers, so a spec can be built to match it.

    Call this before generating with a template. The result lists which layouts exist and
    which spec layout each maps to, which paragraph styles the document defines, and
    which {{placeholders}} the template author left to be filled.
    """
    try:
        return templates.inspect(caller, template_id)
    except TemplateError as exc:
        raise _translate_template(exc) from exc


@router.post(
    "/upload_template",
    operation_id="hive_upload_template",
    summary="Store a file from this chat as a reusable template (administrators only)",
)
async def upload_template(
    body: UploadTemplateRequest, templates: TemplatesDep, caller: CallerDep
) -> dict[str, object]:
    """Save a .pptx/.potx, .docx/.dotx or .xlsx/.xltx from the chat as a template.

    Administrators only. Templates are a shared, curated pool: everyone can use them,
    only administrators can add or remove them.

    The file is validated by reading it; the result is the same report
    hive_inspect_template would give, so no second call is needed.
    """
    try:
        return await templates.upload_from_chat(
            caller,
            file_id=body.file_id,
            name=body.name,
            filename=body.filename,
            description=body.description,
        )
    except TemplateError as exc:
        raise _translate_template(exc) from exc


@router.get(
    "/open_config",
    operation_id="hive_open_config",
    summary="Show a settings form for a document in the chat",
    response_class=HTMLResponse,
)
async def open_config(
    templates: TemplatesDep,
    caller: CallerDep,
    request: Request,
    kind: ConfigKind = "pptx",
    topic: str = "",
    audience: str = "",
    theme: ThemeChoice = "auto",
    language: LanguageChoice = "auto",
) -> HTMLResponse:
    """Open an interactive settings card so the user can choose font, template, audience
    and length before generating.

    Use this when the user wants to configure a document rather than describe it in
    prose, or when they ask for "options" or "settings". Return the result as-is; it
    renders as a form in the chat. The user's choices come back as a new message.

    Set `language` to the language this conversation is in. OpenWebUI keeps the interface
    locale in the browser, so this server cannot read it, and the form would otherwise be
    English for everyone.
    """
    # Best effort, and never fatal: a card in the wrong theme is cosmetic, while a
    # failed settings lookup blocking the dialog would not be.
    preferences = await request.app.state.preferences.get(
        caller.identity.user_id, caller.token
    )
    html = render_config_page(
        kind,
        templates.list(caller),
        preferences=preferences,
        theme=theme,
        language=language,
        prefill_topic=topic,
        prefill_audience=audience,
    )
    return HTMLResponse(
        content=html,
        headers={
            # The header that turns a tool result into an embedded card rather than a
            # wall of printed markup. Exposing it matters as soon as OpenWebUI calls this
            # from the browser as a direct tool server: without it CORS hides the header
            # and the HTML is rendered as text.
            "Content-Disposition": "inline",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.delete(
    "/delete_template/{template_id}",
    operation_id="hive_delete_template",
    summary="Delete a template (administrators only)",
)
async def delete_template(
    template_id: str, templates: TemplatesDep, caller: CallerDep
) -> dict[str, object]:
    """Delete a template from the shared pool. Administrators only."""
    try:
        templates.delete(caller, template_id)
    except TemplateError as exc:
        raise _translate_template(exc) from exc
    return {"deleted": template_id}
