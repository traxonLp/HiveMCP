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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth import Identity, require_caller
from ..core.models import DeckSpec, DocSpec, RenderOptions, RenderResult, SheetSpec
from ..core.service import DocumentService, ToolError

router = APIRouter(prefix="/tools", tags=["documents"])


def get_service(request: Request) -> DocumentService:
    return request.app.state.service


ServiceDep = Annotated[DocumentService, Depends(get_service)]
CallerDep = Annotated[Identity, Depends(require_caller)]


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


def _translate(exc: ToolError) -> HTTPException:
    # 422 rather than 400 or 500: the request was well-formed HTTP but semantically
    # wrong, and the model can fix it from the message and retry.
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))


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
