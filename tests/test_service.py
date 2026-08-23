from __future__ import annotations

import asyncio
import base64

import pytest

from hivemcp.auth import Caller, Identity, RateLimiter
from hivemcp.config import Settings
from hivemcp.core.delivery import DeliveryResult
from hivemcp.core.files.owui_client import OwuiError, OwuiFilesClient
from hivemcp.core.models import (
    Bullet,
    DeckSpec,
    DocSpec,
    ImageRef,
    Paragraph,
    RenderOptions,
    Slide,
)
from hivemcp.core.render.base import RenderError
from hivemcp.core.service import DocumentService, ToolError
from hivemcp.core.templates.service import TemplateService
from hivemcp.core.templates.store import TemplateStore


def a_caller(user_id: str = "u-1") -> Caller:
    return Caller(identity=Identity(user_id=user_id), token=f"tok-{user_id}")


class RecordingDelivery:
    def __init__(self) -> None:
        self.delivered: list = []

    async def deliver(self, rendered, caller):  # noqa: ANN001, ANN202
        self.delivered.append((rendered, caller))
        return DeliveryResult(file_id="owui-1", warnings=["delivery note"])


class FakeOwui(OwuiFilesClient):
    def __init__(self, content: bytes | None = None, error: str | None = None) -> None:
        super().__init__("http://owui")
        self._content = content
        self._error = error
        self.requested: list[tuple[str, str]] = []

    async def get_content(self, file_id: str, token: str) -> bytes:
        # The token is what makes the read happen as the user rather than as a service
        # account, so its absence is a bug worth failing on rather than tolerating.
        assert token, "the caller's session token must reach the Files API"
        self.requested.append((file_id, token))
        if self._error:
            raise OwuiError(self._error)
        assert self._content is not None
        return self._content


@pytest.fixture
def delivery() -> RecordingDelivery:
    return RecordingDelivery()


@pytest.fixture
def service(settings: Settings, delivery: RecordingDelivery) -> DocumentService:
    settings.ensure_dirs()
    return DocumentService(settings, delivery, FakeOwui())


async def test_create_presentation_returns_a_usable_result(
    service: DocumentService, caller: Caller, deck: DeckSpec, delivery: RecordingDelivery
) -> None:
    result = await service.create_presentation(caller, spec=deck)

    assert result.file_id == "owui-1"
    assert result.filename == "Quartalsbericht.pptx"
    assert result.slide_count == len(deck.slides)
    assert result.size_bytes > 0
    assert "delivery note" in result.warnings
    assert delivery.delivered[0][1] is caller


async def test_render_warnings_and_delivery_warnings_are_merged(
    service: DocumentService, caller: Caller
) -> None:
    spec = DeckSpec(title="T", slides=[Slide(title="X", bullets=[Bullet(text="hi")])])
    result = await service.create_presentation(
        caller, spec=spec, options=RenderOptions(font_family="Comic Sans MS")
    )

    assert any("Comic Sans MS" in warning for warning in result.warnings)
    assert "delivery note" in result.warnings


async def test_document_reports_a_page_estimate_not_a_count(
    service: DocumentService, caller: Caller, document: DocSpec
) -> None:
    result = await service.create_document(caller, spec=document)
    assert result.page_estimate is not None
    assert result.slide_count is None


async def test_neither_spec_nor_brief_explains_what_to_send(
    service: DocumentService, caller: Caller
) -> None:
    with pytest.raises(ToolError, match="DeckSpec"):
        await service.create_presentation(caller)


async def test_both_spec_and_brief_is_rejected(
    service: DocumentService, caller: Caller, deck: DeckSpec
) -> None:
    with pytest.raises(ToolError, match="not both"):
        await service.create_presentation(caller, spec=deck, brief="something")


async def test_brief_without_llm_tells_the_model_to_build_the_spec(
    service: DocumentService, caller: Caller
) -> None:
    with pytest.raises(ToolError, match="disabled"):
        await service.create_presentation(caller, brief="10 slides about pricing")


async def test_template_id_without_a_template_service_says_so(
    service: DocumentService, caller: Caller, deck: DeckSpec
) -> None:
    """A server built without template support must say that, not fail obscurely.

    The message also has to give the model a way forward — dropping ``template_id`` —
    or it retries the same call.
    """
    with pytest.raises(ToolError, match="Omit 'template_id'"):
        await service.create_presentation(
            caller, spec=deck, options=RenderOptions(template_id="corp")
        )


async def test_an_unknown_template_id_reports_the_stores_own_message(
    settings: Settings, delivery: RecordingDelivery, caller: Caller, deck: DeckSpec
) -> None:
    settings.ensure_dirs()
    service = DocumentService(
        settings,
        delivery,
        FakeOwui(),
        templates=TemplateService(settings, TemplateStore(settings.templates_dir), FakeOwui()),
    )
    with pytest.raises(ToolError, match="hive_list_templates"):
        await service.create_presentation(
            caller, spec=deck, options=RenderOptions(template_id="gibt-es-nicht")
        )


async def test_render_errors_keep_their_location(
    service: DocumentService, caller: Caller
) -> None:
    spec = DeckSpec(
        title="T", slides=[Slide(title="A"), Slide(layout="chart", title="Ohne Daten")]
    )
    with pytest.raises(RenderError, match="slide 2"):
        await service.create_presentation(caller, spec=spec)


async def test_rate_limiter_is_enforced_per_user(
    settings: Settings, delivery: RecordingDelivery, deck: DeckSpec
) -> None:
    settings.ensure_dirs()
    service = DocumentService(
        settings, delivery, FakeOwui(), limiter=RateLimiter(capacity=1, refill_per_second=0.0)
    )

    await service.create_presentation(a_caller("u-1"), spec=deck)
    with pytest.raises(ToolError, match="Too many"):
        await service.create_presentation(a_caller("u-1"), spec=deck)

    # A second user must be unaffected.
    await service.create_presentation(a_caller("u-2"), spec=deck)


async def test_image_file_ids_are_resolved_through_openwebui(
    settings: Settings, delivery: RecordingDelivery, caller: Caller, tiny_png: str
) -> None:
    """The renderers are synchronous and run in a worker thread; the resolver has to
    bridge back to the event loop to reach the async Files API."""
    settings.ensure_dirs()
    owui = FakeOwui(content=base64.b64decode(tiny_png))
    service = DocumentService(settings, delivery, owui)

    spec = DeckSpec(
        title="T",
        slides=[Slide(layout="image", title="X", image=ImageRef(file_id="f-1"))],
    )
    result = await service.create_presentation(caller, spec=spec)

    # The caller's own token, not a service credential, is what fetched the image.
    assert owui.requested == [("f-1", caller.token)]
    assert result.size_bytes > 0


async def test_unreachable_openwebui_surfaces_as_a_render_error(
    settings: Settings, delivery: RecordingDelivery, caller: Caller
) -> None:
    settings.ensure_dirs()
    service = DocumentService(settings, delivery, FakeOwui(error="connection refused"))

    spec = DeckSpec(
        title="T",
        slides=[Slide(layout="image", title="X", image=ImageRef(file_id="f-1"))],
    )
    with pytest.raises(RenderError, match="connection refused"):
        await service.create_presentation(caller, spec=spec)


async def test_concurrency_is_bounded_by_the_semaphore(
    settings: Settings, delivery: RecordingDelivery, deck: DeckSpec
) -> None:
    """Unbounded parallel rendering is the most likely route to an OOMKill."""
    settings.ensure_dirs()
    service = DocumentService(
        settings.model_copy(update={"max_render_concurrency": 2}), delivery, FakeOwui()
    )

    # Counted *inside* the semaphore, by wrapping the semaphore itself. Wrapping
    # `_render` instead — the obvious move — measures how many coroutines have entered
    # the method, and they all enter before any of them blocks on acquire. That reads as
    # a peak of 6 no matter how small the limit is, so it would fail against correct code
    # and, worse, pass against code with no semaphore at all.
    class Counting:
        def __init__(self, inner: asyncio.Semaphore) -> None:
            self.inner = inner
            self.live = 0
            self.peak = 0

        async def __aenter__(self) -> None:
            await self.inner.acquire()
            self.live += 1
            self.peak = max(self.peak, self.live)

        async def __aexit__(self, *exc: object) -> None:
            self.live -= 1
            self.inner.release()

    counting = Counting(service._semaphore)  # noqa: SLF001
    service._semaphore = counting  # noqa: SLF001

    await asyncio.gather(
        *(service.create_presentation(a_caller(f"u-{i}"), spec=deck) for i in range(6))
    )

    assert counting.peak <= 2, f"expected at most 2 concurrent renders, saw {counting.peak}"
    assert counting.peak > 0, "the semaphore was never entered, so nothing was measured"


async def test_spreadsheet_reports_sheet_names(
    service: DocumentService, caller: Caller, workbook
) -> None:
    result = await service.create_spreadsheet(caller, spec=workbook)
    assert result.sheet_names == ["Regionen"]
    assert result.filename == "Umsatz.xlsx"


async def test_paragraph_only_document_still_renders(
    service: DocumentService, caller: Caller
) -> None:
    spec = DocSpec(title="Kurz", blocks=[Paragraph(text="Ein Satz.")])
    result = await service.create_document(caller, spec=spec)
    assert result.filename == "Kurz.docx"
