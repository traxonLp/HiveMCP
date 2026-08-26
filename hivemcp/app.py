"""FastAPI application factory.

The app hosts several surfaces over one shared core:

* ``/healthz``, ``/readyz`` — Kubernetes probes
* ``/d/{token}``           — signed artifact download
* ``/mcp``                 — MCP Streamable HTTP  (milestone M3)
* ``/tools/*``             — OpenAPI tool server  (milestone M3)
* ``/ui/*``                — configuration GUI    (milestone M5)

Callers authenticate with their own OpenWebUI session token; HiveMCP holds no service
credentials of its own. See ``auth.py``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .auth import SessionValidator, SignatureError, verify_ui_token
from .config import Settings, get_settings
from .core.delivery import CompositeDelivery, OwuiDelivery, SignedUrlDelivery
from .core.files.owui_client import OwuiFilesClient
from .core.files.workdir import ArtifactStore
from .core.llm.client import OwuiChatClient
from .core.preferences_client import PreferencesClient
from .core.render.base import RenderError
from .core.service import DocumentService
from .core.skills import SkillRegistry
from .core.templates.service import TemplateService
from .core.templates.store import TemplateStore
from .surfaces.mcp_server import build_mcp_asgi_app, build_mcp_server
from .surfaces.openapi_tools import router as tools_router
from .surfaces.skills_api import router as skills_router

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
    """Choose how finished documents reach the user. See Settings.delivery_mode.

    Without OpenWebUI configured there is only one option, whatever the mode says: the
    upload has nowhere to go. Failing here instead would take the whole server down over
    a setting that only matters once a document exists.
    """
    signed = SignedUrlDelivery(settings, store)
    if not owui.configured:
        if settings.delivery_mode == "owui":
            logger.warning(
                "HIVE_DELIVERY_MODE=owui needs HIVE_OWUI_URL; falling back to signed "
                "links. Nothing will be added to anyone's OpenWebUI file list."
            )
        return signed

    if settings.delivery_mode == "owui":
        # No artifact written at all: the volume stays empty and this server needs no
        # browser-reachable address. The trade is that a failed upload loses the render,
        # because there is no second copy to fall back to.
        public = settings.owui_public_url or settings.owui_url
        if not settings.owui_public_url:
            logger.warning(
                "HIVE_DELIVERY_MODE=owui builds download links from %s, because "
                "HIVE_OWUI_PUBLIC_URL is not set. That is the address this container "
                "uses, which is usually not one a browser can open — set "
                "HIVE_OWUI_PUBLIC_URL to the URL your users reach OpenWebUI at, or the "
                "download button will lead nowhere.",
                public,
            )
        return OwuiDelivery(owui, with_link=True, public_url=public)
    if settings.delivery_mode == "link":
        return signed
    return CompositeDelivery(OwuiDelivery(owui), signed)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    store = ArtifactStore(settings.tmp_dir, settings.tmp_ttl_minutes)
    validator = SessionValidator(
        settings.owui_url,
        timeout=settings.owui_timeout_seconds,
        cache_ttl=settings.session_cache_ttl_seconds,
        cache_max=settings.session_cache_max_entries,
    )
    owui = OwuiFilesClient(
        settings.owui_url,
        timeout=settings.owui_timeout_seconds,
        max_bytes=settings.max_upload_bytes,
    )
    # Separate client: completions run to a much longer timeout than file transfers,
    # and sharing one would either cut generation short or leave uploads hanging.
    chat = OwuiChatClient(
        settings.owui_url,
        timeout=settings.llm_timeout_seconds,
        max_output_tokens=settings.llm_max_output_tokens,
    )
    preferences = PreferencesClient(settings.owui_url)
    templates = TemplateService(settings, TemplateStore(settings.templates_dir), owui)
    # Read from disk once, here, so a malformed skill fails at startup rather than on the
    # first request that needs it.
    skills = SkillRegistry()
    service = DocumentService(
        settings, _build_delivery(settings, store, owui), owui, chat, templates
    )

    # Built before the app so the session manager exists by the time the lifespan runs.
    mcp = build_mcp_server(service, validator, templates, skills)
    mcp_app = build_mcp_asgi_app(mcp, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_dirs()
        app.state.settings = settings
        app.state.store = store
        app.state.validator = validator
        app.state.owui = owui
        app.state.chat = chat
        app.state.preferences = preferences
        app.state.templates = templates
        app.state.skills = skills
        app.state.service = service
        app.state.mcp = mcp

        async with AsyncExitStack() as stack:
            # Without this the /mcp mount answers 404 or 500 with no explanation
            # (python-sdk #1367). It is the single most common way to get this wrong.
            await stack.enter_async_context(mcp.session_manager.run())
            sweeper = asyncio.create_task(_sweep_loop(store))
            logger.info(
                "HiveMCP started (env=%s, auth=session-token, owui=%s, delivery=%s, "
                "brief_mode=%s, skills=%s, mcp=/mcp, tools=/tools)",
                settings.environment,
                settings.owui_url or "NOT CONFIGURED - nothing can authenticate",
                settings.delivery_mode,
                "on (uses the model selected in the chat)"
                if settings.llm_enabled
                else "off (spec only)",
                # Zero here means the Markdown did not make it into the image, which
                # otherwise only shows up as a model that never learned how to call us.
                ", ".join(skills.names()) or "NONE FOUND",
            )
            try:
                yield
            finally:
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper
                await owui.aclose()
                await chat.aclose()
                await validator.aclose()
                await preferences.aclose()

    app = FastAPI(
        title="HiveMCP",
        version=__version__,
        description=(
            "Generates and edits PowerPoint, Word and Excel files for OpenWebUI. "
            "Exposed both as an MCP Streamable HTTP server and as an OpenAPI tool server."
        ),
        lifespan=lifespan,
    )

    _register_security_headers(app, settings)
    _register_error_handlers(app)
    _register_health(app, settings)
    _register_download(app)
    app.include_router(tools_router)
    app.include_router(skills_router)

    if settings.environment == "dev":
        # Reflects request headers back to the caller, which is exactly what the M0
        # spikes need and exactly what must never be reachable in production.
        from .surfaces.debug import router as debug_router

        app.include_router(debug_router)
        logger.info(
            "dev diagnostics mounted under /_debug: whoami, settings-probe, "
            "upload-check, richui"
        )

    app.mount("/mcp", mcp_app)
    return app


def _register_security_headers(app: FastAPI, settings: Settings) -> None:
    """Baseline response headers.

    Two of the usual recommendations are deliberately absent:

    * **No ``X-Frame-Options``/``frame-ancestors``.** The configuration card and the
      download card exist to be embedded in OpenWebUI's iframe. Denying framing would
      turn off requirement 5 rather than protect anything.
    * **No CORS middleware.** Nothing here is called cross-origin by a browser: the tool
      surfaces are called server-to-server by OpenWebUI, and the HTML surfaces are
      documents, not XHR targets. Adding permissive CORS would only widen what a page on
      another origin could do with a user's session token.

    The CSP is the part that carries weight, and only on HTML: the cards run inline
    script and inline style, so those have to be allowed, but ``default-src 'none'``
    means the embedded card cannot fetch, load or beacon anywhere at all. A card that
    cannot reach the network cannot exfiltrate the token it was rendered with.
    """
    html_csp = (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; "
        "img-src data:; "
        "form-action 'none'; "
        "base-uri 'none'"
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN202
        # /mcp is skipped on purpose. These headers are instructions to a browser, and
        # nothing reaches /mcp from one — it is JSON-RPC between OpenWebUI and this
        # process. Wrapping it would buy nothing and cost something: FastAPI's
        # `@app.middleware` is BaseHTTPMiddleware, which re-wraps the response through
        # its own task group and has a long history of interfering with the streaming
        # transports and anyio scoping the MCP session manager relies on.
        if request.url.path.startswith("/mcp"):
            return await call_next(request)

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # Browsers only honour this over HTTPS; harmless on the plain-HTTP dev setup and
        # correct the moment an ingress terminates TLS in front of it.
        if settings.environment == "prod":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Content-Security-Policy", html_csp)
        return response


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RenderError)
    async def _render_error(request: Request, exc: RenderError) -> JSONResponse:
        # 422, not 500: the spec was the problem, and the model can fix it and retry.
        # The message is written to be actionable, so it is passed through verbatim.
        return JSONResponse(
            # Literal, not a Starlette constant: the name for this code changed between
            # versions and either spelling warns on the other side of the rename.
            status_code=422,
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
        # Each volume separately. They are usually different mounts with different
        # ownership, and a templates volume the container cannot write to would otherwise
        # look healthy until the first administrator tried to add a template.
        #
        # In 'owui' mode no artifact is ever written, so the storage volume is not part
        # of readiness. Demanding it would leave a correctly configured pod permanently
        # unready over a mount it has no use for.
        required = (
            (("templates", settings.templates_dir),)
            if settings.delivery_mode == "owui"
            else (("storage", settings.tmp_dir), ("templates", settings.templates_dir))
        )

        checks: dict[str, str] = {}
        try:
            settings.ensure_dirs()
        except OSError as exc:
            # Attributed to whatever is actually required, so an unusable artifact
            # volume cannot fail readiness in a mode that never touches it.
            for label, _ in required:
                checks[label] = f"unwritable: {exc}"

        for label, directory in required:
            if label in checks:
                continue
            probe = directory / ".readyz"
            try:
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                checks[label] = "ok"
            except OSError as exc:
                checks[label] = f"unwritable: {exc}"

        checks["openwebui"] = "configured" if settings.owui_configured else "not configured"
        healthy = all(value == "ok" for key, value in checks.items() if key != "openwebui")
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
