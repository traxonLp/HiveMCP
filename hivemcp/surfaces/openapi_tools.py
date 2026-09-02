"""OpenAPI tool-server surface.

OpenWebUI ingests ``/openapi.json`` and turns every operation into a tool. This is also
the surface that can return Rich UI embeds (``Content-Disposition: inline``), which the
native MCP path cannot — so the configuration GUI will land here in M5.

Operation ids are set explicitly: OpenWebUI derives the tool name the model sees from
them, and FastAPI's generated ids (``create_presentation_tools_create_presentation_post``)
would waste context and read badly in the UI.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..auth import Caller, SignatureError, require_caller, verify_ui_token
from ..core.editing.read import ReadMode
from ..core.models import (
    DeckSpec,
    DocSpec,
    EditOp,
    EditResult,
    RenderOptions,
    RenderResult,
    SheetSpec,
)
from ..core.files.owui_client import OwuiError
from ..core.service import DocumentService, ToolError
from ..core.skills import SkillError, SkillRegistry
from ..core.templates.service import TemplateService
from ..core.templates.store import NotPermitted, TemplateError, TemplateKind
from .config_ui import ConfigKind, LanguageChoice, render_config_page
from .download_ui import render_download_card

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tools", tags=["documents"])


def get_service(request: Request) -> DocumentService:
    return request.app.state.service


def get_templates(request: Request) -> TemplateService:
    return request.app.state.templates


def get_skills(request: Request) -> SkillRegistry:
    return request.app.state.skills


# OpenWebUI's own file-content route, absolute or relative. Matched on shape rather than
# on the configured host, so a card still works after the instance moves — and matched at
# all because a URL pointing there carries no signed token this server could verify.
_OWUI_CONTENT = re.compile(r"/api/v1/files/(?P<file_id>[A-Za-z0-9._:-]+)/content/?$")


def _owui_content_file_id(url: str) -> str | None:
    match = _OWUI_CONTENT.search(url)
    return match.group("file_id") if match else None


ServiceDep = Annotated[DocumentService, Depends(get_service)]
TemplatesDep = Annotated[TemplateService, Depends(get_templates)]
SkillsDep = Annotated[SkillRegistry, Depends(get_skills)]
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
    summary="Create a PowerPoint presentation, slide deck or slides",
    response_model=RenderResult,
)
async def create_presentation(
    body: PresentationRequest, service: ServiceDep, caller: CallerDep
) -> RenderResult:
    """Create a real PowerPoint file the user can download.

    Use this whenever someone asks for a presentation, slides, a deck or a .pptx — do not
    write the slides out as text or a note instead. You compose the content and pass it
    as `spec`; this server renders the actual file.
    """
    try:
        return await service.create_presentation(
            caller, options=body.options, spec=body.spec, brief=body.brief
        )
    except ToolError as exc:
        raise _translate(exc) from exc


@router.post(
    "/create_document",
    operation_id="hive_create_document",
    summary="Create a Word document, report, letter or memo",
    response_model=RenderResult,
)
async def create_document(
    body: DocumentRequest, service: ServiceDep, caller: CallerDep
) -> RenderResult:
    """Create a real Word file the user can download.

    Use this whenever someone asks for a document, report, letter, memo or a .docx rather
    than answering with the text itself.
    """
    try:
        return await service.create_document(
            caller, options=body.options, spec=body.spec, brief=body.brief
        )
    except ToolError as exc:
        raise _translate(exc) from exc


@router.post(
    "/create_markdown",
    operation_id="hive_create_markdown",
    summary="Create a Markdown file, README or notes document",
    response_model=RenderResult,
)
async def create_markdown(
    body: DocumentRequest, service: ServiceDep, caller: CallerDep
) -> RenderResult:
    """Create a real .md file the user can download.

    Use this when someone asks for Markdown, a README, release notes, documentation for a
    repository, or a file for a static-site generator. Takes the same `spec` as
    hive_create_document — headings, paragraphs, lists, tables, code — so if you can build
    one you can build the other.

    Reach for Word instead when the result is meant to be printed, sent to someone who
    does not read Markdown, or styled from a corporate template.
    """
    try:
        return await service.create_markdown(
            caller, options=body.options, spec=body.spec, brief=body.brief
        )
    except ToolError as exc:
        raise _translate(exc) from exc


@router.post(
    "/create_spreadsheet",
    operation_id="hive_create_spreadsheet",
    summary="Create an Excel workbook, spreadsheet or table file",
    response_model=RenderResult,
)
async def create_spreadsheet(
    body: SpreadsheetRequest, service: ServiceDep, caller: CallerDep
) -> RenderResult:
    """Create a real Excel file the user can download.

    Use this whenever someone asks for a spreadsheet, workbook, table file or an .xlsx
    rather than printing a markdown table.
    """
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
    "/usage_guide",
    operation_id="hive_usage_guide",
    summary="Read the guide to using HiveMCP",
)
async def usage_guide(skills: SkillsDep, name: str | None = None) -> dict[str, object]:
    """Which tool to call for which request, and how to write a spec that renders.

    Call this first if you are unsure how to build a document here, or if a call failed
    validation and you want to know the correct shape.

    Deliberately the one operation on this router without a ``CallerDep``. It returns
    documentation that ships inside the image — no user data, nothing caller-specific —
    and the moment it is most needed is when authentication is misconfigured and every
    other tool is already returning 401.
    """
    if name is None:
        skill = skills.default
        if skill is None:
            raise HTTPException(404, "this server ships no usage guide")
    else:
        try:
            skill = skills.get(name)
        except SkillError as exc:
            raise HTTPException(404, str(exc)) from exc
    return {"available": skills.names(), **skill.to_dict()}


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


class EditDocumentRequest(BaseModel):
    file_id: str = Field(
        description="Id of a file the user attached to this chat. Read it with "
        "hive_read_document first so the positions in your operations are right."
    )
    operations: list[EditOp] = Field(
        min_length=1,
        description="Applied in order, all or nothing. Nothing is delivered unless every "
        "operation succeeds.",
    )
    filename: str | None = Field(
        default=None, description="Name for the edited copy. The original is left alone."
    )


@router.get(
    "/show_download",
    operation_id="hive_show_download",
    summary="Show a download button for a file this server produced",
    response_class=HTMLResponse,
)
async def show_download(
    request: Request,
    caller: CallerDep,
    download_url: str,
    filename: str | None = None,
    size_bytes: int | None = None,
    language: LanguageChoice = "auto",
) -> HTMLResponse:
    """Render a download card with a real button for a file HiveMCP just produced.

    Pass the `download_url` from a create or edit result. Call this straight after
    generating: a URL inside a tool result is plain text and cannot be clicked, and any
    warnings on the result are easy to miss. Return the card as-is.

    Passing `filename` and `size_bytes` from that same result saves a lookup, but they
    are optional: if they are missing the server works them out itself. Do not skip
    calling this tool because you do not have them.
    """
    settings = request.app.state.settings

    file_id = _owui_content_file_id(download_url)
    if file_id is not None:
        # HIVE_DELIVERY_MODE=owui: the document lives in the caller's OpenWebUI files and
        # never touched this server's volume, so there is no signed token to verify and
        # no artifact to measure.
        #
        # The name and size are asked of OpenWebUI rather than required from the caller.
        # An earlier version refused with a 422 when 'filename' was missing, which made a
        # working download button depend on the model remembering to pass a parameter —
        # and models forget. Supplied values still win, so the common path costs nothing.
        card_filename, card_size = filename, size_bytes
        if not card_filename:
            try:
                found = await request.app.state.owui.get_metadata(file_id, caller.token)
            except OwuiError:
                # Not fatal. A card with a generic name is worth more than an error, and
                # the link itself — the part that matters — is already known.
                logger.warning("could not read file metadata for %s", file_id, exc_info=True)
                found = {}
            card_filename = str(found.get("filename") or "") or card_filename
            if card_size is None and isinstance(found.get("size_bytes"), int):
                card_size = int(found["size_bytes"])  # type: ignore[arg-type]
        card_filename = card_filename or "document"
    else:
        token = download_url.rstrip("/").rsplit("/", 1)[-1]
        try:
            payload = verify_ui_token(token, settings)
        except SignatureError as exc:
            raise HTTPException(
                UNPROCESSABLE,
                f"That is not a usable download link ({exc}). Pass the download_url "
                "exactly as the create or edit tool returned it.",
            ) from exc

        artifact = request.app.state.store.get(str(payload.get("artifact_id", "")))
        if artifact is None:
            raise HTTPException(
                UNPROCESSABLE,
                "That file has expired. Generate it again to get a fresh link.",
            )
        card_filename, card_size = artifact.filename, artifact.size_bytes

    kind = card_filename.rsplit(".", 1)[-1].lower()
    if kind not in ("pptx", "docx", "xlsx"):
        kind = "pptx"

    preferences = await request.app.state.preferences.get(
        caller.identity.user_id, caller.token
    )
    html = render_download_card(
        url=download_url,
        filename=card_filename,
        kind=kind,  # type: ignore[arg-type]
        size_bytes=card_size,
        preferences=preferences,
        language=language,
    )
    return HTMLResponse(
        content=html,
        headers={
            "Content-Disposition": "inline",
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.get(
    "/read_document/{file_id}",
    operation_id="hive_read_document",
    summary="Read a document the user attached to the chat",
)
async def read_document(
    file_id: str,
    service: ServiceDep,
    caller: CallerDep,
    mode: ReadMode = "outline",
) -> dict[str, object]:
    """Read a .pptx, .docx or .xlsx from the chat and report its structure.

    Call this before hive_edit_document: it gives you the slide numbers, paragraph
    numbers, sheet names and styles that the edit operations refer to. Use 'outline'
    unless you need the full text.
    """
    try:
        return await service.read_document(caller, file_id, mode)
    except ToolError as exc:
        raise _translate(exc) from exc


@router.post(
    "/edit_document",
    operation_id="hive_edit_document",
    summary="Apply edits to a document from the chat",
    response_model=EditResult,
)
async def edit_document(
    body: EditDocumentRequest, service: ServiceDep, caller: CallerDep
) -> EditResult:
    """Change specific things in an existing document and deliver the result.

    The file is patched, not rebuilt, so its formatting survives. The original is left
    untouched and the edit comes back as a new file.

    The result's `applied` list says what each operation actually changed. An operation
    can succeed while matching nothing — worth checking before telling the user it is done.
    """
    try:
        return await service.edit_document(
            caller, body.file_id, body.operations, body.filename
        )
    except ToolError as exc:
        raise _translate(exc) from exc


@router.get(
    "/open_config",
    operation_id="hive_open_config",
    summary="Open the settings form: font, template, audience, length",
    response_class=HTMLResponse,
)
async def open_config(
    templates: TemplatesDep,
    caller: CallerDep,
    request: Request,
    kind: ConfigKind = "pptx",
    topic: str = "",
    audience: str = "",
    language: LanguageChoice = "auto",
) -> HTMLResponse:
    """Show an interactive settings form in the chat.

    Call this whenever the user wants to choose or change how a document should look
    rather than describe it in prose — "settings", "options", "configure", "Einstellungen",
    "konfigurieren", or when they ask to pick a template, font, audience or length.

    Return the result exactly as it comes back; it renders as a form. The user's choices
    arrive as a new chat message, and only then do you call a create tool.

    Set `language` to the language this conversation is in: OpenWebUI keeps the interface
    locale in the browser, so this server cannot read it and the form would otherwise be
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
