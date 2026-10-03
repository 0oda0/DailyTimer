"""Клиент к бесплатным LLM через OpenAI-совместимый API (Gemini, Groq, OpenRouter, Ollama)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

PROVIDERS: dict[str, dict[str, str]] = {
    # Бесплатный тариф Google AI Studio: ключ на https://aistudio.google.com/apikey
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.5-flash",
    },
    # Бесплатный тариф Groq: ключ на https://console.groq.com/keys
    "groq": {"base_url": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile"},
    # Бесплатные модели OpenRouter (суффикс :free): https://openrouter.ai/keys
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
    },
    # Полностью локально и бесплатно: https://ollama.com, ключ не нужен
    "ollama": {"base_url": "http://localhost:11434/v1", "model": "qwen2.5:7b"},
}


class AIError(RuntimeError):
    pass


class AIClient:
    def __init__(self, provider: str, api_key: str = "", model: str = "", base_url: str = ""):
        preset = PROVIDERS.get(provider, PROVIDERS["gemini"])
        self.provider = provider
        self.base_url = (base_url or preset["base_url"]).rstrip("/")
        self.model = model or preset["model"]
        self.api_key = api_key

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "AIClient | None":
        provider = settings.get("ai_provider") or "gemini"
        if provider != "ollama" and not settings.get("ai_api_key"):
            return None
        return cls(provider, settings.get("ai_api_key", ""), settings.get("ai_model", ""))

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    def chat(self, system: str, user: str, temperature: float = 0.4, timeout: float = 90) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            resp = httpx.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise AIError(f"Не удалось связаться с ИИ ({self.provider}): {exc}") from exc
        if resp.status_code >= 400:
            raise AIError(f"ИИ ответил ошибкой {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise AIError(f"Неожиданный ответ ИИ: {resp.text[:300]}") from exc

    def chat_json(self, system: str, user: str) -> Any:
        return extract_json(self.chat(system, user, temperature=0))


def extract_json(text: str) -> Any:
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise AIError("В ответе ИИ нет JSON")
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        end = max(text.rfind("}"), text.rfind("]"))
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise AIError(f"Не удалось разобрать JSON от ИИ: {exc}") from exc
