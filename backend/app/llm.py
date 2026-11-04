"""OpenAI-compatible chat providers, plus a local extractive fallback."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import Settings
from app.rag.tokenize import content_tokens

CAPTURED: list[str] = []


@dataclass
class Generation:
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str
    estimated: bool


class LLM(Protocol):
    model: str
    provider: str

    def generate(self, question: str, evidence: list[str]) -> AsyncIterator[str]: ...

    def usage(self) -> tuple[int, int, bool]: ...


def best_sentence(question: str, text: str) -> str:
    query = set(content_tokens(question))
    pieces = []
    for part in _split_sentences(text):
        stripped = part.strip().lstrip("#").strip()
        if not stripped or stripped.startswith("#"):
            continue
        pieces.append(stripped)
    if not pieces:
        return text.strip()[:180]

    def score(sentence: str) -> tuple[int, int]:
        overlap = len(query & set(content_tokens(sentence)))
        return (overlap, len(sentence))

    ranked = sorted(pieces, key=score, reverse=True)
    return ranked[0][:400]


def _split_sentences(text: str) -> list[str]:
    current: list[str] = []
    sentences: list[str] = []
    for char in text:
        current.append(char)
        if char in "。！？\n":
            sentences.append("".join(current))
            current = []
    if current:
        sentences.append("".join(current))
    return sentences


def compose_extractive(question: str, titles: list[str], headings: list[str], evidence: list[str]) -> str:
    lines = ["根据实验室文档中可核对的段落，回答如下。"]
    for index, (title, heading, text) in enumerate(zip(titles, headings, evidence, strict=True), start=1):
        sentence = best_sentence(question, text)
        where = f"《{title}》"
        if heading:
            where += f"·{heading}"
        lines.append(f"[{index}] {sentence}（{where}）")
    lines.append("以上内容只复述检索到的原文，没有使用这些段落之外的信息。")
    return "\n".join(lines)


class FakeLLM:
    provider = "fake"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.llm_model
        self._prompt_tokens = 0
        self._completion_tokens = 0

    async def generate(self, question: str, evidence: list[str]) -> AsyncIterator[str]:
        if self.settings.llm_capture:
            CAPTURED.append(question + "\n" + "\n".join(evidence))
        # Titles and headings are passed inside evidence as plain strings by the caller.
        text = "\n".join(evidence).strip()
        self._prompt_tokens = max(1, (len(question) + len(text)) // 2)
        self._completion_tokens = max(1, len(text) // 2)
        step = 16
        for index in range(0, len(text), step):
            yield text[index : index + step]

    def usage(self) -> tuple[int, int, bool]:
        return self._prompt_tokens, self._completion_tokens, True


class OpenAICompatibleLLM:
    provider: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.provider = settings.llm_provider
        self.model = settings.llm_model
        self._prompt_tokens = 0
        self._completion_tokens = 0

    async def generate(self, question: str, evidence: list[str]) -> AsyncIterator[str]:
        if self.settings.llm_capture:
            CAPTURED.append(question + "\n" + "\n".join(evidence))
        system = (
            "你是实验室知识助手。只根据给定证据回答，用 [1]、[2] 标注证据编号。"
            "证据没有写到的事实不要补充。如果证据不够，就明确说无法回答。"
        )
        numbered = "\n\n".join(f"[{index}] {item}" for index, item in enumerate(evidence, start=1))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"问题：{question}\n\n证据：\n{numbered}"},
        ]
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key.get_secret_value()}"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        url = f"{(self.settings.llm_base_url or '').rstrip('/')}/chat/completions"
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        parsed = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage = parsed.get("usage") or {}
                    if usage:
                        self._prompt_tokens = int(usage.get("prompt_tokens") or 0)
                        self._completion_tokens = int(usage.get("completion_tokens") or 0)
                    choices = parsed.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    if delta:
                        yield delta
        if self._prompt_tokens == 0:
            self._prompt_tokens = max(1, (len(question) + sum(len(item) for item in evidence)) // 2)

    def usage(self) -> tuple[int, int, bool]:
        return self._prompt_tokens, self._completion_tokens, self.provider == "fake"


def build_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "fake":
        return FakeLLM(settings)
    if settings.llm_model == "fake-grounded-v1":
        settings.llm_model = {
            "deepseek": "deepseek-chat",
            "openai": "gpt-4o-mini",
            "ollama": "qwen2.5:7b",
            "vllm": "default",
        }.get(settings.llm_provider, settings.llm_model)
    return OpenAICompatibleLLM(settings)
