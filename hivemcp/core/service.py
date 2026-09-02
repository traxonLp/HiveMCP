"""Application service: everything both surfaces need, in one place.

The MCP and OpenAPI surfaces are deliberately thin. All validation, concurrency
control, rendering and delivery lives here, so the two surfaces cannot drift apart
in behaviour and the interesting logic is testable without HTTP or a protocol.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from ..auth import Caller, RateLimiter
from ..config import Settings
from .delivery import Delivery
from .editing.apply import EditError, apply_edits
from .editing.read import DocumentUnreadable, read_document as _read_document_file
from .files.owui_client import OwuiError, OwuiFilesClient
from .llm.client import OwuiChatClient
from .llm.expand import ExpansionError, expand_brief
from .llm.resolver import ModelUnavailable, resolve_model
from .models import DeckSpec, DocSpec, EditResult, RenderOptions, RenderResult, SheetSpec
from .render.base import RenderedFile, RenderError
from .render.docx import render_document
from .render.markdown import render_markdown
from .render.pptx import render_presentation
from .render.theme import safe_filename
from .render.xlsx import render_spreadsheet
from .templates.service import TemplateService
from .templates.store import TemplateError

logger = logging.getLogger(__name__)

# The first bytes of an OOXML part, used to tell the three formats apart. Reading the
# zip's own directory is what makes this reliable: the container is a plain zip for all
# three, so only the parts inside distinguish them.
_OOXML_MARKERS: tuple[tuple[str, str], ...] = (
    ("ppt/presentation.xml", "pptx"),
    ("word/document.xml", "docx"),
    ("xl/workbook.xml", "xlsx"),
)


def detect_kind(data: bytes) -> str | None:
    """Identify a document by what is inside it, not by what it is called."""
    import zipfile
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        # Not an archive, so it may still be Markdown. Decoding as UTF-8 is the test:
        # arbitrary binary almost never decodes cleanly, and anything that does is text
        # this server can at least read back safely.
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "md"
    for marker, kind in _OOXML_MARKERS:
        if marker in names:
            return kind
    return None


def _read_bytes(data: bytes, kind: str, mode: str) -> dict[str, Any]:
    """Write the upload to a temporary file so the parsers can open it by path."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / f"upload.{kind}"
        path.write_bytes(data)
        return _read_document_file(path, kind, mode)  # type: ignore[arg-type]


class ToolError(Exception):
    """A tool call the caller can fix and retry.

    Distinct from RenderError, which is about the document itself. Both are reported
    back to the model as text rather than as a server fault, because in both cases the
    next useful action is the model changing its request.
    """


class DocumentService:
    def __init__(
        self,
        settings: Settings,
        delivery: Delivery,
        owui: OwuiFilesClient,
        chat: OwuiChatClient | None = None,
        templates: TemplateService | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.delivery = delivery
        self.owui = owui
        self.chat = chat or OwuiChatClient(settings.owui_url)
        self.templates = templates
        self.limiter = limiter or RateLimiter()
        # Rendering is CPU- and memory-heavy and runs in a worker thread. Without a
        # bound, a handful of concurrent large workbooks is the most likely way to get
        # the pod OOMKilled.
        self._semaphore = asyncio.Semaphore(settings.max_render_concurrency)

    # ----------------------------------------------------------------- tools

    async def create_presentation(
        self,
        caller: Caller,
        options: RenderOptions | None = None,
        spec: DeckSpec | None = None,
        brief: str | None = None,
    ) -> RenderResult:
        options = options or RenderOptions()
        notes: list[str] = []
        spec = await self._resolve_spec(
            spec, brief, options, DeckSpec, caller, "presentation", notes
        )
        template = self._template_path(options, caller)
        resolver = self._image_resolver(caller)

        rendered = await self._render(
            render_presentation, spec, options, image_resolver=resolver, template_path=template
        )
        return await self._deliver(rendered, caller, notes)

    async def create_document(
        self,
        caller: Caller,
        options: RenderOptions | None = None,
        spec: DocSpec | None = None,
        brief: str | None = None,
    ) -> RenderResult:
        options = options or RenderOptions()
        notes: list[str] = []
        spec = await self._resolve_spec(
            spec, brief, options, DocSpec, caller, "document", notes
        )
        template = self._template_path(options, caller)
        resolver = self._image_resolver(caller)

        rendered = await self._render(
            render_document, spec, options, image_resolver=resolver, template_path=template
        )
        return await self._deliver(rendered, caller, notes)

    async def create_markdown(
        self,
        caller: Caller,
        options: RenderOptions | None = None,
        spec: DocSpec | None = None,
        brief: str | None = None,
    ) -> RenderResult:
        """Render the same DocSpec as create_document, but as Markdown.

        One spec for both: the block types already describe a document, and a second
        model would only be a second thing to keep in step.
        """
        options = options or RenderOptions()
        notes: list[str] = []
        spec = await self._resolve_spec(
            spec, brief, options, DocSpec, caller, "document", notes
        )
        template = self._template_path(options, caller)
        resolver = self._image_resolver(caller)

        rendered = await self._render(
            render_markdown, spec, options, image_resolver=resolver, template_path=template
        )
        return await self._deliver(rendered, caller, notes)

    async def create_spreadsheet(
        self,
        caller: Caller,
        options: RenderOptions | None = None,
        spec: SheetSpec | None = None,
        brief: str | None = None,
    ) -> RenderResult:
        options = options or RenderOptions()
        notes: list[str] = []
        spec = await self._resolve_spec(
            spec, brief, options, SheetSpec, caller, "workbook", notes
        )
        template = self._template_path(options, caller)

        rendered = await self._render(
            render_spreadsheet, spec, options, template_path=template
        )
        return await self._deliver(rendered, caller, notes)

    # ------------------------------------------------------- editing uploads

    async def read_document(
        self, caller: Caller, file_id: str, mode: str = "outline"
    ) -> dict[str, Any]:
        """Read a file the user attached to the chat."""
        data, kind = await self._fetch_upload(caller, file_id)
        try:
            return await asyncio.to_thread(_read_bytes, data, kind, mode)
        except DocumentUnreadable as exc:
            raise ToolError(str(exc)) from exc

    async def edit_document(
        self,
        caller: Caller,
        file_id: str,
        operations: list[Any],
        filename: str | None = None,
    ) -> EditResult:
        """Apply edit operations to an uploaded file and deliver the result as a new one.

        The original is never modified: the user may well still want it, and OpenWebUI's
        Files API has no notion of a version. The edit comes back as a separate upload.
        """
        data, kind = await self._fetch_upload(caller, file_id)
        target = safe_filename(filename, f"edited-document", kind)

        async with self._semaphore:
            try:
                rendered, applied = await asyncio.to_thread(
                    apply_edits, data, kind, operations, target
                )
            except (EditError, DocumentUnreadable) as exc:
                raise ToolError(str(exc)) from exc

        result = await self.delivery.deliver(rendered, caller)
        return EditResult(
            file_id=result.file_id,
            download_url=result.download_url,
            download_markdown=self._markdown_link(result.download_url, rendered.filename),
            filename=rendered.filename,
            media_type=rendered.media_type,
            size_bytes=rendered.size_bytes,
            applied=applied,
            warnings=[*rendered.warnings, *result.warnings],
        )

    async def _fetch_upload(self, caller: Caller, file_id: str) -> tuple[bytes, str]:
        """Download a chat attachment and work out which kind of document it is.

        The kind comes from the bytes, not from a filename: OpenWebUI's file id carries
        no extension, and an uploaded name is a claim rather than a fact.
        """
        try:
            data = await self.owui.get_content(file_id, caller.token)
        except OwuiError as exc:
            raise ToolError(str(exc)) from exc

        kind = detect_kind(data)
        if kind is None:
            raise ToolError(
                "That file is not a document HiveMCP can read. It handles .pptx, .docx, "
                ".xlsx and Markdown text files."
            )
        return data, kind

    # --------------------------------------------------------------- helpers

    async def _resolve_spec(
        self,
        spec: Any,
        brief: str | None,
        options: RenderOptions,
        model: type,
        caller: Caller,
        kind: str,
        warnings: list[str],
    ) -> Any:
        if not self.limiter.allow(caller.identity.user_id):
            raise ToolError(
                "Too many document requests in a short time. Wait a moment and try again."
            )
        if spec is not None and brief is not None:
            raise ToolError("Pass either 'spec' or 'brief', not both.")
        if spec is not None:
            return spec
        if brief is None:
            raise ToolError(
                f"Nothing to render. Pass a full 'spec' ({model.__name__}) describing the "
                "content, or a 'brief' if this server has LLM expansion enabled."
            )
        if not self.settings.llm_enabled:
            raise ToolError(
                "This server renders from a structured spec only; generating content from "
                f"a brief is disabled. Build the {model.__name__} yourself and pass it as "
                "'spec'."
            )

        # Expansion runs on the model the user selected in the chat, reached through
        # OpenWebUI's own chat-completions API.
        try:
            resolved = await resolve_model(self.chat, self.settings, caller)
        except ModelUnavailable as exc:
            raise ToolError(str(exc)) from exc

        note = resolved.warning()
        if note:
            warnings.append(note)

        try:
            return await expand_brief(
                self.chat,
                self.settings,
                caller,
                resolved.model,
                brief,
                options,
                model,
                kind,
            )
        except ExpansionError as exc:
            raise ToolError(str(exc)) from exc

    def _template_path(self, options: RenderOptions, caller: Caller) -> Path | None:
        if options.template_id is None:
            return None
        if self.templates is None:
            raise ToolError(
                "Templates are not available on this server. Omit 'template_id' to "
                "render with the default theme."
            )
        try:
            return self.templates.path_for(caller, options.template_id)
        except TemplateError as exc:
            # The store's messages already say what to do next (usually: call
            # hive_list_templates), so they are passed through rather than rewrapped.
            raise ToolError(str(exc)) from exc

    def _image_resolver(self, caller: Caller):  # noqa: ANN202 - closure type is noise
        """Bridge the synchronous renderers to the async Files API.

        The renderers are deliberately synchronous so they can run in a worker thread.
        This closure is therefore called from that thread and must not touch the running
        event loop directly, hence run_coroutine_threadsafe against the captured loop.
        """
        loop = asyncio.get_running_loop()

        def resolve(file_id: str) -> bytes:
            future = asyncio.run_coroutine_threadsafe(
                self.owui.get_content(file_id, caller.token), loop
            )
            try:
                return future.result(timeout=self.settings.owui_timeout_seconds + 5)
            except OwuiError as exc:
                raise ValueError(str(exc)) from exc

        return resolve

    async def _render(self, renderer: Any, spec: Any, options: RenderOptions, **kwargs: Any):
        async with self._semaphore:
            try:
                return await asyncio.to_thread(renderer, spec, options, **kwargs)
            except RenderError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("unexpected render failure")
                raise RenderError(f"the document could not be built: {exc}") from exc

    @staticmethod
    def _markdown_link(url: str | None, filename: str) -> str | None:
        """A link the model can paste straight into its reply.

        A URL inside a tool result is plain text: OpenWebUI renders tool results as JSON,
        and only the assistant's own message goes through markdown. Handing the model a
        finished link is what makes the download clickable at all.
        """
        if not url:
            return None
        # Parentheses in a filename would end the markdown target early.
        safe = filename.replace("[", "(").replace("]", ")")
        return f"[⬇ {safe}]({url})"

    async def _deliver(
        self, rendered: RenderedFile, caller: Caller, notes: list[str] | None = None
    ) -> RenderResult:
        result = await self.delivery.deliver(rendered, caller)
        return RenderResult(
            file_id=result.file_id,
            download_url=result.download_url,
            download_markdown=self._markdown_link(result.download_url, rendered.filename),
            filename=rendered.filename,
            media_type=rendered.media_type,
            size_bytes=rendered.size_bytes,
            slide_count=rendered.slide_count,
            page_estimate=rendered.page_estimate,
            sheet_names=rendered.sheet_names,
            warnings=[*(notes or []), *rendered.warnings, *result.warnings],
        )
