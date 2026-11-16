"""Thin LLM helper: OpenAI or Anthropic, whichever key is configured."""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Literal, Optional

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

Provider = Literal["openai", "anthropic"]


def resolve_provider(settings: Settings | None = None) -> tuple[Optional[Provider], Optional[str]]:
    """Prefer OpenAI when both keys exist (user's available provider)."""
    cfg = settings or get_settings()
    openai_key = (cfg.openai_api_key or "").strip() or None
    anthropic_key = (cfg.anthropic_api_key or "").strip() or None
    if openai_key:
        return "openai", openai_key
    if anthropic_key:
        return "anthropic", anthropic_key
    return None, None


def chat_text(
    prompt: str,
    *,
    settings: Settings | None = None,
    max_tokens: int = 4096,
    system: str | None = None,
) -> str:
    provider, api_key = resolve_provider(settings)
    if not provider or not api_key:
        raise RuntimeError("No LLM API key configured (set OPENAI_API_KEY or ANTHROPIC_API_KEY)")

    if provider == "openai":
        return _openai_text(api_key, prompt, max_tokens=max_tokens, system=system, settings=settings)
    return _anthropic_text(api_key, prompt, max_tokens=max_tokens, system=system, settings=settings)


def vision_pdf_or_image_json(
    path_bytes: bytes,
    prompt: str,
    *,
    media_type: str = "application/pdf",
    settings: Settings | None = None,
    max_tokens: int = 1200,
) -> dict[str, Any]:
    """OCR / extract structured JSON from a PDF or image using vision."""
    provider, api_key = resolve_provider(settings)
    if not provider or not api_key:
        raise RuntimeError("No LLM API key configured (set OPENAI_API_KEY or ANTHROPIC_API_KEY)")

    if provider == "openai":
        text = _openai_vision_text(
            api_key,
            path_bytes,
            prompt,
            media_type=media_type,
            max_tokens=max_tokens,
            settings=settings,
        )
    else:
        text = _anthropic_vision_text(
            api_key,
            path_bytes,
            prompt,
            media_type=media_type,
            max_tokens=max_tokens,
            settings=settings,
        )
    return _parse_json_object(text)


def _parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"No JSON object in model response: {text[:200]}")
    return json.loads(text[start : end + 1])


def parse_json_array(text: str) -> list[Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < 0:
        raise ValueError("No JSON array in model response")
    return json.loads(text[start : end + 1])


def _openai_model(settings: Settings | None) -> str:
    cfg = settings or get_settings()
    return (cfg.openai_model or "gpt-4o").strip()


def _anthropic_model(settings: Settings | None) -> str:
    cfg = settings or get_settings()
    return (cfg.anthropic_model or "claude-sonnet-4-20250514").strip()


def _openai_text(
    api_key: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str | None,
    settings: Settings | None,
) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    response = client.chat.completions.create(
        model=_openai_model(settings),
        max_tokens=max_tokens,
        messages=messages,
    )
    return (response.choices[0].message.content or "").strip()


def _anthropic_text(
    api_key: str,
    prompt: str,
    *,
    max_tokens: int,
    system: str | None,
    settings: Settings | None,
) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": _anthropic_model(settings),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "".join(parts).strip()


def _openai_vision_text(
    api_key: str,
    raw: bytes,
    prompt: str,
    *,
    media_type: str,
    max_tokens: int,
    settings: Settings | None,
) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    b64 = base64.standard_b64encode(raw).decode("ascii")

    # OpenAI chat vision prefers images; for PDFs rasterize when possible.
    if media_type == "application/pdf":
        try:
            from io import BytesIO

            from pdf2image import convert_from_bytes

            images = convert_from_bytes(raw, first_page=1, last_page=1, dpi=200)
            buf = BytesIO()
            images[0].save(buf, format="PNG")
            b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
            media_type = "image/png"
        except Exception as exc:  # noqa: BLE001
            logger.warning("PDF rasterize failed (%s); sending as file input unsupported — retry image", exc)
            raise RuntimeError(
                "OpenAI PAF OCR needs pdf2image/poppler to rasterize scanned PDFs, "
                f"or provide PNG. Rasterize error: {exc}"
            ) from exc

    data_url = f"data:{media_type};base64,{b64}"
    response = client.chat.completions.create(
        model=_openai_model(settings),
        max_tokens=max_tokens,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    return (response.choices[0].message.content or "").strip()


def _anthropic_vision_text(
    api_key: str,
    raw: bytes,
    prompt: str,
    *,
    media_type: str,
    max_tokens: int,
    settings: Settings | None,
) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    b64 = base64.standard_b64encode(raw).decode("ascii")
    model = _anthropic_model(settings)

    if media_type == "application/pdf":
        content: list[dict[str, Any]] = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64,
                },
            },
            {"type": "text", "text": prompt},
        ]
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
            )
        except Exception as doc_exc:  # noqa: BLE001
            logger.warning("Anthropic PDF document block failed (%s); trying image", doc_exc)
            from io import BytesIO

            from pdf2image import convert_from_bytes

            images = convert_from_bytes(raw, first_page=1, last_page=1, dpi=200)
            buf = BytesIO()
            images[0].save(buf, format="PNG")
            img_b64 = base64.standard_b64encode(buf.getvalue()).decode("ascii")
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": img_b64,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
    else:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

    parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts).strip()
