"""Diagnostics for the M0 spikes. Dev only.

Answers the questions in docs/M0_SPIKES.md by showing what actually arrives on the wire:
which headers OpenWebUI sends, whether a session token is among them, whether that token
validates, and who it belongs to.

Mounted only when ``HIVE_ENVIRONMENT=dev``. It reflects request headers back to the
caller, so on a production instance it would hand anyone with reachability a way to read
other requests' credentials from their own — harmless here, unacceptable there.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..auth import AuthError, authenticate
from ..core.preferences import SETTINGS_ENDPOINTS, UserPreferences, parse_preferences
from ..config import Settings

# Deliberately *in* the OpenAPI schema, unlike the rest of the non-tool routes. The
# whole point is for OpenWebUI to call these as tools: that is the only way the session
# token gets attached, and a token is what the spikes are about. The router is mounted
# in dev only, so these never reach a production schema.
router = APIRouter(prefix="/_debug", tags=["debug"])

# Headers that may carry a credential. Values are masked so a screenshot of the output
# can be shared without leaking a working token.
SENSITIVE = {"authorization", "x-api-key", "cookie", "proxy-authorization"}

# Endpoints that plausibly validate a session token and return the user. S2 works out
# which one actually does.
VALIDATION_CANDIDATES = ("/api/v1/auths/", "/api/v1/users/user", "/api/v1/auths/user")


def _mask(value: str) -> str:
    """Keep the shape visible, hide the secret."""
    if len(value) <= 12:
        return f"<{len(value)} chars>"
    return f"{value[:8]}…{value[-4:]} <{len(value)} chars>"


def _decode_jwt(token: str) -> dict[str, Any] | None:
    """Read a JWT payload without verifying it.

    Verification is OpenWebUI's job; this only answers S4 ("how long is it valid?") and
    tells a JWT apart from an opaque token.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def _extract_token(request: Request) -> tuple[str | None, str | None]:
    """Return (token, where it came from)."""
    header = request.headers.get("authorization")
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value, "Authorization: Bearer"
    for name in ("x-api-key", "x-openwebui-token"):
        value = request.headers.get(name)
        if value:
            return value, name
    return None, None


async def _validate(settings: Settings, token: str) -> dict[str, Any]:
    """Try each candidate endpoint and report what happened (spike S2)."""
    if not settings.owui_url:
        return {"error": "HIVE_OWUI_URL is not set, cannot validate"}

    results: dict[str, Any] = {}
    async with httpx.AsyncClient(base_url=settings.owui_url, timeout=10.0) as client:
        for path in VALIDATION_CANDIDATES:
            started = time.perf_counter()
            try:
                response = await client.get(
                    path, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as exc:
                results[path] = {"error": str(exc)}
                continue

            entry: dict[str, Any] = {
                "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000, 1),
            }
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if isinstance(payload, dict):
                    entry["identity"] = {
                        key: payload.get(key)
                        for key in ("id", "name", "email", "role")
                        if key in payload
                    }
                    entry["all_fields"] = sorted(payload)
            results[path] = entry
    return results


@router.get(
    "/whoami",
    operation_id="hive_debug_whoami",
    summary="Diagnostics: report the caller's identity and credentials",
)
async def whoami(request: Request) -> JSONResponse:
    """Report which headers, identity and session token reached this server.

    Call this to diagnose the HiveMCP connection. Show the user the full JSON result;
    credentials in it are already masked.

    (Spikes S1, S2 and S4 of docs/M0_SPIKES.md in one response.)
    """
    settings: Settings = request.app.state.settings

    headers = {
        name: (_mask(value) if name.lower() in SENSITIVE else value)
        for name, value in request.headers.items()
    }

    token, source = _extract_token(request)
    report: dict[str, Any] = {
        "spike": "S1/S2/S4 - see docs/M0_SPIKES.md",
        "client": request.client.host if request.client else None,
        "headers": dict(sorted(headers.items())),
        "hive_identity_headers": {
            name: value
            for name, value in request.headers.items()
            if name.lower().startswith(("x-hive-", "x-openwebui-", "x-open-webui-"))
        },
        "token": {"present": token is not None, "source": source},
    }

    if token:
        claims = _decode_jwt(token)
        report["token"]["kind"] = "jwt" if claims else "opaque"
        if claims:
            expires_at = claims.get("exp")
            report["token"]["claims"] = {
                key: value for key, value in claims.items() if key != "exp"
            }
            if isinstance(expires_at, int):
                remaining = expires_at - int(time.time())
                report["token"]["expires_in_seconds"] = remaining
                report["token"]["expires_in_human"] = (
                    f"{remaining // 3600}h {remaining % 3600 // 60}m"
                    if remaining > 0
                    else "ALREADY EXPIRED"
                )
        report["validation"] = await _validate(settings, token)

    return JSONResponse(report)


@router.get(
    "/settings-probe",
    operation_id="hive_debug_settings_probe",
    summary="Diagnostics: show what OpenWebUI reports about your interface settings",
)
async def settings_probe(request: Request) -> JSONResponse:
    """Show the raw settings documents OpenWebUI returns for you.

    Show the user the full JSON result.

    Used to work out where this OpenWebUI version keeps the interface theme and locale,
    so the configuration card can match the surrounding chat instead of guessing.
    """
    settings: Settings = request.app.state.settings
    validator = request.app.state.validator

    try:
        caller = await authenticate(validator, request.headers)
    except AuthError as exc:
        return JSONResponse({"probe": "settings", "error": str(exc)}, status_code=401)

    report: dict[str, Any] = {
        "probe": "settings",
        "hint": "Looking for a theme ('dark'/'light'/'system') and a locale ('de-DE').",
        "endpoints": {},
        "parsed_by_hivemcp": None,
    }

    async with httpx.AsyncClient(base_url=settings.owui_url or "", timeout=10.0) as client:
        for path in SETTINGS_ENDPOINTS:
            entry: dict[str, Any] = {}
            try:
                response = await client.get(
                    path, headers={"Authorization": f"Bearer {caller.token}"}
                )
            except httpx.HTTPError as exc:
                report["endpoints"][path] = {"error": str(exc)}
                continue

            entry["status"] = response.status_code
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    entry["body"] = response.text[:400]
                else:
                    # The whole document, not a summary: the point is to find out where
                    # this version puts things, which a filtered view would hide.
                    entry["payload"] = payload
                    parsed = parse_preferences(payload)
                    entry["hivemcp_reads"] = {
                        "theme": parsed.theme,
                        "locale": parsed.locale,
                        "language": parsed.language,
                    }
                    if report["parsed_by_hivemcp"] is None and parsed != UserPreferences():
                        report["parsed_by_hivemcp"] = path
            report["endpoints"][path] = entry

    return JSONResponse(report)


@router.get(
    "/upload-check",
    operation_id="hive_debug_upload_check",
    summary="Diagnostics: check who a file uploaded by HiveMCP belongs to",
)
async def upload_check(request: Request) -> JSONResponse:
    """Upload a tiny test file and report which OpenWebUI account owns it.

    Show the user the full JSON result.

    (Spike S3 of docs/M0_SPIKES.md. Whether the file *appears* in a file list is weak
    evidence — an admin may see everything. The owner id recorded on the file is the
    decisive answer.)
    """
    settings: Settings = request.app.state.settings
    validator = request.app.state.validator

    try:
        caller = await authenticate(validator, request.headers)
    except AuthError as exc:
        return JSONResponse({"spike": "S3", "error": str(exc)}, status_code=401)

    payload = b"HiveMCP ownership probe\n"
    report: dict[str, Any] = {
        "spike": "S3 - see docs/M0_SPIKES.md",
        "authenticated_as": caller.identity.audit_dict(),
    }

    async with httpx.AsyncClient(base_url=settings.owui_url or "", timeout=20.0) as client:
        auth = {"Authorization": f"Bearer {caller.token}"}
        try:
            upload = await client.post(
                "/api/v1/files/",
                params={"process": "false"},
                files={"file": ("hivemcp-ownership-probe.txt", payload, "text/plain")},
                headers={**auth, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            report["error"] = f"upload could not reach OpenWebUI: {exc}"
            return JSONResponse(report)

        report["upload"] = {"status": upload.status_code}
        if upload.status_code >= 400:
            report["upload"]["body"] = upload.text[:300]
            return JSONResponse(report)

        record = upload.json() if upload.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        file_id = record.get("id")
        report["upload"]["file_id"] = file_id
        # Reported straight from the upload response and again from a fresh read, since
        # the two could in principle disagree.
        report["upload"]["owner_field"] = record.get("user_id")
        report["upload"]["all_fields"] = sorted(record)

        if not file_id:
            return JSONResponse(report)

        try:
            fetched = await client.get(f"/api/v1/files/{file_id}", headers=auth)
        except httpx.HTTPError as exc:
            report["fetch"] = {"error": str(exc)}
            return JSONResponse(report)

        report["fetch"] = {"status": fetched.status_code}
        owner: Any = None
        if fetched.status_code == 200:
            try:
                detail = fetched.json()
            except ValueError:
                detail = {}
            if isinstance(detail, dict):
                owner = detail.get("user_id")
                report["fetch"]["owner_field"] = owner
                report["fetch"]["all_fields"] = sorted(detail)

    expected = caller.identity.user_id
    matches = owner == expected or report["upload"].get("owner_field") == expected
    report["verdict"] = {
        "expected_owner": expected,
        "owner_matches_caller": matches,
        "meaning": (
            "S3 PASSED - the file belongs to the calling user, so HiveMCP is a pure "
            "renderer and risk R1 is gone."
            if matches
            else "S3 FAILED or inconclusive - compare the owner fields above against "
            "expected_owner. If OpenWebUI does not expose an owner field, verify with a "
            "second, non-admin account instead."
        ),
    }
    return JSONResponse(report)


@router.get(
    "/richui",
    operation_id="hive_debug_richui",
    summary="Diagnostics: render a test card in the chat",
)
async def richui() -> HTMLResponse:
    """Render a small test card inline in the chat to verify iframe embedding works.

    (Spike S7 of docs/M0_SPIKES.md. If OpenWebUI shows a card rather than printing HTML,
    the path milestone M5 depends on works.)
    """
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
 body{font-family:system-ui,sans-serif;margin:0;padding:20px;
      background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff}
 .card{background:rgba(255,255,255,.15);border-radius:14px;padding:20px}
 h1{margin:0 0 8px;font-size:1.2rem} p{margin:0;opacity:.9}
</style></head><body>
 <div class="card">
   <h1>Rich UI works</h1>
   <p>Spike S7 passed: OpenWebUI rendered this as an iframe, so milestone M5 is viable.</p>
 </div>
 <script>
   function reportHeight(){
     parent.postMessage({type:'iframe:height',
                         height:document.documentElement.scrollHeight},'*');
   }
   window.addEventListener('load',reportHeight);
   new ResizeObserver(reportHeight).observe(document.body);
 </script>
</body></html>"""
    return HTMLResponse(
        content=html,
        headers={
            "Content-Disposition": "inline",
            # Required when OpenWebUI calls this from the browser as a direct tool
            # server: without it CORS hides the header and the HTML renders as text.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )
