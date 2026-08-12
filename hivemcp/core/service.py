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

from ..auth import Identity, RateLimiter
from ..config import Settings
from .delivery import Delivery
from .files.owui_client import OwuiError, OwuiFilesClient
from .llm.client import OwuiChatClient
from .llm.expand import ExpansionError, expand_brief
from .llm.resolver import ModelUnavailable, resolve_model
from .models import DeckSpec, DocSpec, RenderOptions, RenderResult, SheetSpec
from .render.base import RenderedFile, RenderError
from .render.docx import render_document
from .render.pptx import render_presentation
from .render.xlsx import render_spreadsheet

logger = logging.getLogger(__name__)


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
        limiter: RateLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.delivery = delivery
        self.owui = owui
        self.chat = chat or OwuiChatClient(settings.owui_url, settings.owui_api_key)
        self.limiter = limiter or RateLimiter()
        # Rendering is CPU- and memory-heavy and runs in a worker thread. Without a
        # bound, a handful of concurrent large workbooks is the most likely way to get
        # the pod OOMKilled.
        self._semaphore = asyncio.Semaphore(settings.max_render_concurrency)

    # ----------------------------------------------------------------- tools

    async def create_presentation(
        self,
        identity: Identity,
        options: RenderOptions | None = None,
        spec: DeckSpec | None = None,
        brief: str | None = None,
    ) -> RenderResult:
        options = options or RenderOptions()
        notes: list[str] = []
        spec = await self._resolve_spec(
            spec, brief, options, DeckSpec, identity, "presentation", notes
        )
        template = self._template_path(options)
        resolver = self._image_resolver(identity)

        rendered = await self._render(
            render_presentation, spec, options, image_resolver=resolver, template_path=template
        )
        return await self._deliver(rendered, identity, notes)

    async def create_document(
        self,
        identity: Identity,
        options: RenderOptions | None = None,
        spec: DocSpec | None = None,
        brief: str | None = None,
    ) -> RenderResult:
        options = options or RenderOptions()
        notes: list[str] = []
        spec = await self._resolve_spec(
            spec, brief, options, DocSpec, identity, "document", notes
        )
        template = self._template_path(options)
        resolver = self._image_resolver(identity)

        rendered = await self._render(
            render_document, spec, options, image_resolver=resolver, template_path=template
        )
        return await self._deliver(rendered, identity, notes)

    async def create_spreadsheet(
        self,
        identity: Identity,
        options: RenderOptions | None = None,
        spec: SheetSpec | None = None,
        brief: str | None = None,
    ) -> RenderResult:
        options = options or RenderOptions()
        notes: list[str] = []
        spec = await self._resolve_spec(
            spec, brief, options, SheetSpec, identity, "workbook", notes
        )
        template = self._template_path(options)

        rendered = await self._render(
            render_spreadsheet, spec, options, template_path=template
        )
        return await self._deliver(rendered, identity, notes)

    # --------------------------------------------------------------- helpers

    async def _resolve_spec(
        self,
        spec: Any,
        brief: str | None,
        options: RenderOptions,
        model: type,
        identity: Identity,
        kind: str,
        warnings: list[str],
    ) -> Any:
        if not self.limiter.allow(identity.user_id):
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
            resolved = await resolve_model(self.chat, self.settings, identity)
        except ModelUnavailable as exc:
            raise ToolError(str(exc)) from exc

        note = resolved.warning()
        if note:
            warnings.append(note)

        try:
            return await expand_brief(
                self.chat,
                self.settings,
                identity,
                resolved.model,
                brief,
                options,
                model,
                kind,
            )
        except ExpansionError as exc:
            raise ToolError(str(exc)) from exc

    def _template_path(self, options: RenderOptions) -> Path | None:
        if options.template_id is None:
            return None
        raise ToolError(
            f"Template {options.template_id!r} cannot be used yet: template support is "
            "milestone M4. Omit 'template_id' to render with the default theme."
        )

    def _image_resolver(self, identity: Identity):  # noqa: ANN202 - closure type is noise
        """Bridge the synchronous renderers to the async Files API.

        The renderers are deliberately synchronous so they can run in a worker thread.
        This closure is therefore called from that thread and must not touch the running
        event loop directly, hence run_coroutine_threadsafe against the captured loop.
        """
        loop = asyncio.get_running_loop()

        def resolve(file_id: str) -> bytes:
            future = asyncio.run_coroutine_threadsafe(self.owui.get_content(file_id), loop)
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

    async def _deliver(
        self, rendered: RenderedFile, identity: Identity, notes: list[str] | None = None
    ) -> RenderResult:
        result = await self.delivery.deliver(rendered, identity)
        return RenderResult(
            file_id=result.file_id,
            download_url=result.download_url,
            filename=rendered.filename,
            media_type=rendered.media_type,
            size_bytes=rendered.size_bytes,
            slide_count=rendered.slide_count,
            page_estimate=rendered.page_estimate,
            sheet_names=rendered.sheet_names,
            warnings=[*(notes or []), *rendered.warnings, *result.warnings],
        )
