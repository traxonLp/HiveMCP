"""FastAPI application factory.

The app hosts several surfaces over one shared core:

* ``/healthz``, ``/readyz`` — Kubernetes probes
* ``/d/{token}``           — signed artifact download (the R1 fallback delivery path)
* ``/mcp``                 — MCP Streamable HTTP  (milestone M3)
* ``/tools/*``             — OpenAPI tool server  (milestone M3)
* ``/ui/*``                — configuration GUI    (milestone M5)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse

from .auth import SignatureError, verify_ui_token
from .config import Settings, get_settings
from .core.delivery import CompositeDelivery, OwuiDelivery, SignedUrlDelivery
from .core.files.owui_client import OwuiFilesClient
from .core.files.workdir import ArtifactStore
from .core.llm.client import OwuiChatClient
from .core.render.base import RenderError
from .core.service import DocumentService
from .surfaces.mcp_server import build_mcp_asgi_app, build_mcp_server
from .surfaces.openapi_tools import router as tools_router

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 600


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


async def _sweep_loop(store: ArtifactStore) -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            removed = await asyncio.to_thread(store.sweep)
            if removed:
                logger.info("swept %d expired artifact(s)", removed)
        except Exception:  # noqa: BLE001 - a sweep failure must not kill the loop
            logger.exception("artifact sweep failed")


def _build_delivery(settings: Settings, store: ArtifactStore, owui: OwuiFilesClient):  # noqa: ANN202
    """Choose how finished documents reach the user.

    With OpenWebUI configured the upload is primary and a signed link is attached as
    well, so a document is still reachable if the upload lands on the service account
    rather than the user (plan risk R1). Without it, links are all there is.
    """
    signed = SignedUrlDelivery(settings, store)
    if not owui.configured:
        return signed
    return CompositeDelivery(OwuiDelivery(owui), signed)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    store = ArtifactStore(settings.tmp_dir, settings.tmp_ttl_minutes)
    owui = OwuiFilesClient(
        settings.owui_url,
        settings.owui_api_key,
        timeout=settings.owui_timeout_seconds,
        max_bytes=settings.max_upload_bytes,
    )
    # Separate client: completions run to a much longer timeout than file transfers,
    # and sharing one would either cut generation short or leave uploads hanging.
    chat = OwuiChatClient(
        settings.owui_url,
        settings.owui_api_key,
        timeout=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    service = DocumentService(settings, _build_delivery(settings, store, owui), owui, chat)

    # Built before the app so the session manager exists by the time the lifespan runs.
    mcp = build_mcp_server(service)
    mcp_app = build_mcp_asgi_app(mcp, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_dirs()
        app.state.settings = settings
        app.state.store = store
        app.state.owui = owui
        app.state.chat = chat
        app.state.service = service
        app.state.mcp = mcp

        async with AsyncExitStack() as stack:
            # Without this the /mcp mount answers 404 or 500 with no explanation
            # (python-sdk #1367). It is the single most common way to get this wrong.
            await stack.enter_async_context(mcp.session_manager.run())
            sweeper = asyncio.create_task(_sweep_loop(store))
            logger.info(
                "HiveMCP started (env=%s, auth=%s, owui=%s, brief_mode=%s, "
                "mcp=/mcp, tools=/tools)",
                settings.environment,
                "on" if settings.auth_enabled else "off",
                "configured" if owui.configured else "not configured",
                "on (uses the model selected in the chat)"
                if settings.llm_enabled
                else "off (spec only)",
            )
            try:
                yield
            finally:
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper
                await owui.aclose()
                await chat.aclose()

    app = FastAPI(
        title="HiveMCP",
        version="0.1.0",
        description=(
            "Generates and edits PowerPoint, Word and Excel files for OpenWebUI. "
            "Exposed both as an MCP Streamable HTTP server and as an OpenAPI tool server."
        ),
        lifespan=lifespan,
    )

    _register_error_handlers(app)
    _register_health(app, settings)
    _register_download(app)
    app.include_router(tools_router)
    app.mount("/mcp", mcp_app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RenderError)
    async def _render_error(request: Request, exc: RenderError) -> JSONResponse:
        # 422, not 500: the spec was the problem, and the model can fix it and retry.
        # The message is written to be actionable, so it is passed through verbatim.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "render_failed", "detail": str(exc)},
        )


def _register_health(app: FastAPI, settings: Settings) -> None:
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up. Deliberately does no I/O."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        """Readiness: the PVC is writable.

        A read-only or unmounted volume is the failure mode that silently breaks every
        render, so it is worth failing the probe over rather than serving errors.
        """
        checks: dict[str, str] = {}
        probe = settings.tmp_dir / ".readyz"
        try:
            settings.ensure_dirs()
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            checks["storage"] = "ok"
        except OSError as exc:
            checks["storage"] = f"unwritable: {exc}"

        checks["openwebui"] = "configured" if settings.owui_configured else "not configured"
        healthy = checks["storage"] == "ok"
        return JSONResponse(
            status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if healthy else "not ready", "checks": checks},
        )


def _register_download(app: FastAPI) -> None:
    @app.get("/d/{token}", include_in_schema=False)
    async def download(token: str, request: Request) -> FileResponse:
        """Serve a rendered artifact.

        Authenticated by the HMAC signature in the path rather than by a bearer header,
        because this URL is opened by the user's browser, which has no token to send.
        """
        settings: Settings = request.app.state.settings
        try:
            payload = verify_ui_token(token, settings)
        except SignatureError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Invalid link: {exc}") from exc

        artifact_id = payload.get("artifact_id")
        if not isinstance(artifact_id, str):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid link")

        artifact = request.app.state.store.get(artifact_id)
        if artifact is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "This file has expired. Ask for it to be generated again.",
            )
        return FileResponse(
            artifact.path,
            filename=artifact.filename,
            media_type="application/octet-stream",
        )


app = create_app()
