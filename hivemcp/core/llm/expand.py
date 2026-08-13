"""Turn a short brief into a validated document spec.

The spec's own JSON Schema is handed to the model, so the instructions stay in one place
(``core/models.py``) instead of being restated in a prompt that can drift away from it.

Output is parsed defensively rather than relying on provider-side structured output:
OpenWebUI fronts Ollama, OpenAI, Anthropic and more, and support for ``response_format``
varies by backend. A model that wraps its JSON in a markdown fence is the common case,
not an error.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ...auth import Caller
from ...config import Settings
from ..models import RenderOptions
from .client import LlmError, OwuiChatClient

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

SYSTEM_PROMPT = """\
You turn a short brief into a document specification for a rendering service.

Reply with a single JSON object and nothing else. No prose before or after, no markdown
code fence, no explanation. The object must validate against this JSON Schema:

{schema}

Rules that matter:
- Use only the enum values the schema lists. Do not invent layout or block names.
- Unknown properties are rejected, so add no fields the schema does not define.
- Write real content, not placeholders like "Lorem ipsum" or "TODO".
- Keep bullets to one line each. Prefer several short slides over one crowded slide.
"""

REPAIR_PROMPT = """\
That did not validate:

{errors}

Return the corrected JSON object. Only the object, nothing else.\
"""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class ExpansionError(Exception):
    """The model could not produce a spec that validates."""


def _describe_options(options: RenderOptions, kind: str) -> str:
    lines = [f"Language: {options.language}", f"Detail level: {options.density}"]
    if options.audience:
        lines.append(f"Audience: {options.audience} - pitch the tone and depth for them.")
    if options.target_length:
        unit = "slides" if kind == "presentation" else "pages"
        lines.append(f"Aim for roughly {options.target_length} {unit}.")
    if options.include_notes and kind == "presentation":
        lines.append("Write speaker notes for each slide.")
    return "\n".join(lines)


def extract_json(text: str) -> Any:
    """Recover a JSON object from a model reply.

    Handles the three things models actually do: return clean JSON, wrap it in a
    markdown fence, or surround it with a sentence of commentary.
    """
    candidate = text.strip()

    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the reply")
    try:
        return json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"reply was not valid JSON: {exc}") from exc


def _format_errors(exc: ValidationError, limit: int = 12) -> str:
    lines = []
    for error in exc.errors()[:limit]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"- {location}: {error['msg']}")
    remaining = len(exc.errors()) - limit
    if remaining > 0:
        lines.append(f"- ... and {remaining} more")
    return "\n".join(lines)


async def expand_brief(
    client: OwuiChatClient,
    settings: Settings,
    caller: Caller,
    model: str,
    brief: str,
    options: RenderOptions,
    spec_model: type[T],
    kind: str,
) -> T:
    """Ask the user's model for a spec, validate it, and repair once if needed."""
    schema = json.dumps(spec_model.model_json_schema(), indent=2, ensure_ascii=False)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(schema=schema)},
        {
            "role": "user",
            "content": f"Brief:\n{brief}\n\n{_describe_options(options, kind)}",
        },
    ]

    last_problem = "the model returned nothing usable"

    for attempt in range(settings.llm_max_repair_attempts + 1):
        try:
            reply = await client.complete(model, messages, caller.token)
        except LlmError as exc:
            raise ExpansionError(str(exc)) from exc

        try:
            return spec_model.model_validate(extract_json(reply))
        except ValueError as exc:
            problem = str(exc)
            errors = problem
        except ValidationError as exc:
            problem = f"{len(exc.errors())} schema violation(s)"
            errors = _format_errors(exc)

        last_problem = problem
        logger.info(
            "brief expansion attempt %d with %s failed for user %s: %s",
            attempt + 1,
            model,
            caller.identity.user_id,
            problem,
        )
        if attempt == settings.llm_max_repair_attempts:
            break
        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user", "content": REPAIR_PROMPT.format(errors=errors)})

    raise ExpansionError(
        f"{model!r} could not produce a valid {spec_model.__name__} after "
        f"{settings.llm_max_repair_attempts + 1} attempt(s): {last_problem}. "
        "Build the spec yourself and pass it as 'spec'."
    )
