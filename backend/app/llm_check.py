"""Connectivity self-check for the OpenAI-compatible LLM gateway (Magpie)."""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx

from app.config import Settings, extract_host_port, get_settings


def _format_failure(code: str, base: str) -> str:
    line = f"fail code={code} base={base}"
    if code == "http_401":
        return f"{line} Magpie 密钥无效或没有权限"
    if code == "http_404":
        return f"{line} 模型名不存在，请对照 Magpie 的 /v1/models"
    return line


async def run_llm_check(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[int, str]:
    try:
        cfg = settings if settings is not None else get_settings()
    except Exception as exc:
        msg = str(exc)
        code = "config_error"
        if "LLM_BASE_URL" in msg:
            code = "missing_base_url"
        elif "LLM_API_KEY" in msg:
            code = "missing_api_key"
        elif "LLM_MODEL" in msg:
            code = "missing_model"
        return 1, _format_failure(code, "none")

    base = cfg.llm_base_host or extract_host_port(cfg.llm_base_url) or "none"
    if not cfg.llm_base_url or not cfg.llm_base_url.strip():
        return 1, _format_failure("missing_base_url", base)
    if not cfg.llm_model or not cfg.llm_model.strip() or cfg.llm_model == "fake-grounded-v1":
        return 1, _format_failure("missing_model", base)
    if cfg.llm_provider != "ollama" and cfg.llm_api_key is None:
        return 1, _format_failure("missing_api_key", base)

    headers = {"Content-Type": "application/json"}
    if cfg.llm_api_key is not None:
        headers["Authorization"] = f"Bearer {cfg.llm_api_key.get_secret_value()}"
    payload: dict[str, object] = {
        "model": cfg.llm_model,
        "messages": [{"role": "user", "content": "回复：好"}],
        "max_tokens": 8,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    url = f"{cfg.llm_base_url.rstrip('/')}/chat/completions"
    timeout = httpx.Timeout(timeout=float(cfg.llm_timeout_seconds), connect=5.0)
    started = time.perf_counter()
    first_token_ms: int | None = None
    has_usage = False
    saw_done = False
    saw_choice = False

    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    return 1, _format_failure(f"http_{response.status_code}", base)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        saw_done = True
                        break
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    if parsed.get("error"):
                        return 1, _format_failure("bad_stream", base)
                    raw_usage = parsed.get("usage")
                    if isinstance(raw_usage, dict) and (
                        raw_usage.get("prompt_tokens") is not None
                        or raw_usage.get("completion_tokens") is not None
                    ):
                        has_usage = True
                    choices = parsed.get("choices") or []
                    if isinstance(choices, list) and choices:
                        saw_choice = True
                        first_choice = choices[0]
                        if isinstance(first_choice, dict):
                            delta = first_choice.get("delta") or {}
                            if isinstance(delta, dict) and (
                                delta.get("content") or delta.get("reasoning_content")
                            ):
                                if first_token_ms is None:
                                    first_token_ms = int(
                                        round((time.perf_counter() - started) * 1000)
                                    )
                        if first_token_ms is None:
                            first_token_ms = int(round((time.perf_counter() - started) * 1000))
    except httpx.TimeoutException:
        return 1, _format_failure("timeout", base)
    except httpx.ConnectError:
        return 1, _format_failure("conn_refused", base)
    except httpx.HTTPError:
        return 1, _format_failure("bad_stream", base)

    if not saw_done or not saw_choice:
        return 1, _format_failure("bad_stream", base)
    if first_token_ms is None:
        first_token_ms = int(round((time.perf_counter() - started) * 1000))
    usage_str = "yes" if has_usage else "no"
    return 0, f"ok model={cfg.llm_model} first_token_ms={first_token_ms} usage={usage_str}"


def main() -> int:
    code, output = asyncio.run(run_llm_check())
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
