"""Plain-Markdown delivery of the bundled skills.

The third channel from §6 of the plan. Its job is different from the tool's: an
administrator opens these URLs in a browser to paste the guide into a model's system
prompt, or points OpenWebUI's knowledge base at them.

So it serves ``text/markdown`` rather than JSON, and it is left out of the OpenAPI schema
on purpose — every operation in that schema becomes a tool the model sees, and a second
tool that returns the same guide as ``hive_usage_guide`` would only spend context and
invite the model to pick the wrong one.

Unauthenticated, like the tool: this is documentation that ships inside the image.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

from ..core.skills import SkillError, SkillRegistry

router = APIRouter(prefix="/skills", tags=["skills"])


def _registry(request: Request) -> SkillRegistry:
    return request.app.state.skills


@router.get("", include_in_schema=False)
async def list_skills(request: Request) -> dict[str, object]:
    registry = _registry(request)
    return {
        "skills": [
            {
                "name": skill.name,
                "title": skill.title,
                "description": skill.description,
                "url": f"/skills/{skill.name}",
            }
            for skill in registry.all()
        ]
    }


@router.get("/{name}", include_in_schema=False, response_class=PlainTextResponse)
async def get_skill(name: str, request: Request) -> PlainTextResponse:
    try:
        skill = _registry(request).get(name)
    except SkillError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return PlainTextResponse(
        skill.body,
        media_type="text/markdown; charset=utf-8",
        # Shown in the browser rather than downloaded: the point is to read and copy it.
        headers={"Content-Disposition": "inline"},
    )
