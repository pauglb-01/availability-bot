# packages/clients/openai_client.py
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Literal, overload

from openai import APIError, APITimeoutError, BadRequestError, OpenAI
from pydantic import BaseModel, ValidationError

from .settings import get_settings


@lru_cache(maxsize=1)
def get_openai() -> OpenAI:
    s = get_settings()
    return OpenAI(api_key=s.OPENAI_API_KEY)


def chat_completion(
    messages: list[dict],
    *,
    model: str = "gpt-4o-mini",
    timeout_s: int = 60,
    temperature: float | None = None,
) -> str:
    client = get_openai()
    resp = client.responses.create(
        model=model,
        input=messages,
        timeout=timeout_s,
        temperature=temperature,
    )
    return resp.output_text


# ---------- Structured Outputs helper ----------


class LLMProviderError(RuntimeError):
    """Fallo del proveedor (no timeout)."""


class LLMTimeoutError(TimeoutError):
    """Timeout hablando con el proveedor."""


class LLMBadOutputError(ValueError):
    """El modelo devolvió salida inválida/no parseable/no conforme."""


def _detect_refusal_or_incomplete(resp: Any) -> None:
    status = getattr(resp, "status", None)
    if status == "incomplete":
        details = getattr(resp, "incomplete_details", None)
        reason = getattr(details, "reason", None) if details else None
        raise LLMBadOutputError(f"LLM incomplete response (reason={reason})")

    output = getattr(resp, "output", None)
    if not isinstance(output, list):
        return

    for item in output:
        # item puede ser dict o objeto del SDK
        content = getattr(item, "content", None)
        if content is None and isinstance(item, dict):
            content = item.get("content")

        if not isinstance(content, list):
            continue

        for c in content:
            ctype = (
                getattr(c, "type", None) if not isinstance(c, dict) else c.get("type")
            )
            if ctype == "refusal":
                raise LLMBadOutputError("LLM refused to produce structured output")


@overload
def responses_structured[
    T: BaseModel
](
    messages: list[dict] | str,
    *,
    model: str = "gpt-4o-mini",
    timeout_s: int = 60,
    temperature: float | None = None,
    pydantic_model: type[T],
    return_model: Literal[True] = True,
    json_schema: None = None,
    schema_name: str | None = None,
) -> T: ...


@overload
def responses_structured[
    T: BaseModel
](
    messages: list[dict] | str,
    *,
    model: str = "gpt-4o-mini",
    timeout_s: int = 60,
    temperature: float | None = None,
    pydantic_model: type[T],
    return_model: Literal[False] = False,
    json_schema: None = None,
    schema_name: str | None = None,
) -> dict[str, Any]: ...


@overload
def responses_structured(
    messages: list[dict] | str,
    *,
    model: str = "gpt-4o-mini",
    timeout_s: int = 60,
    temperature: float | None = None,
    pydantic_model: None = None,
    json_schema: dict[str, Any],
    schema_name: str,
) -> dict[str, Any]: ...


def responses_structured[
    T: BaseModel
](
    messages: list[dict] | str,
    *,
    model: str = "gpt-4o-mini",
    timeout_s: int = 60,
    temperature: float | None = None,
    # Elige UNO:
    # Opcion A
    pydantic_model: type[T] | None = None,
    return_model: bool = True,
    # Opción B
    json_schema: dict[str, Any] | None = None,
    schema_name: str | None = None,
) -> (T | dict[str, Any]):
    """
    Structured Outputs con Responses API.

    Opción A (recomendada): pydantic_model=TuModelo
      -> usa client.responses.parse(..., text_format=TuModelo) y devuelve output_parsed.

    Opción B: json_schema={...}, schema_name="..."
      -> usa client.responses.create(..., text={"format": {"type":"json_schema","strict":true,"schema":...}}). :contentReference[oaicite:5]{index=5}
         Devuelve dict parseado con json.loads(resp.output_text)

    Nota: si pasas pydantic_model, puedes pedir return_model=True para recibir la instancia.
    """
    if (pydantic_model is None) == (json_schema is None):
        raise ValueError("Pass exactly one of pydantic_model or json_schema")

    client = get_openai()

    # Construye kwargs comunes SIN temperature si None
    base_kwargs: dict[str, Any] = {
        "model": model,
        "input": messages,
        "timeout": timeout_s,
    }
    if temperature is not None:
        base_kwargs["temperature"] = temperature

    try:
        if pydantic_model is not None:
            # SDK recomendado: parse + Pydantic => resp.output_parsed
            resp = client.responses.parse(text_format=pydantic_model, **base_kwargs)
            _detect_refusal_or_incomplete(resp)

            parsed = getattr(resp, "output_parsed", None)
            if parsed is None:
                raise LLMBadOutputError("No output_parsed returned by SDK")

            if return_model:
                return parsed

            # dict JSON-compatible
            if isinstance(parsed, BaseModel):
                return parsed.model_dump(mode="json")
            raise LLMBadOutputError("output_parsed is not a BaseModel instance")

        # json_schema manual
        if not schema_name:
            raise ValueError("schema_name is required when using json_schema")

        resp = client.responses.create(
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": json_schema,
                }
            },
            **base_kwargs,
        )
        _detect_refusal_or_incomplete(resp)

        txt = getattr(resp, "output_text", None)
        if not txt:
            raise LLMBadOutputError("Empty output_text")

        try:
            return json.loads(txt)
        except json.JSONDecodeError as e:
            raise LLMBadOutputError(f"JSON decode error: {e}") from e

    except APITimeoutError as e:
        raise LLMTimeoutError("LLM request timed out") from e
    except (BadRequestError, APIError) as e:
        raise LLMProviderError(f"LLM provider error: {type(e).__name__}") from e
    except ValidationError as e:
        # Si parse() valida internamente y falla.
        raise LLMBadOutputError("LLM output failed Pydantic validation") from e
