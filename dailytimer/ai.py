"""ИИ без ключей.

Основной движок — локальная модель через Ollama, которая крутится на том же сервере
(docker-compose поднимает её сам и скачивает модель при первом запуске).
Запасной — анонимный бесплатный endpoint Pollinations (без ключа, но с жёстким лимитом).
По желанию можно указать свой OpenAI-совместимый endpoint с ключом.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_LOCAL_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_LOCAL_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
POLLINATIONS_URL = "https://text.pollinations.ai/openai"

# Модели с нормальным русским, от лёгкой к тяжёлой (RAM на сервере).
LOCAL_MODELS = {
    "qwen2.5:1.5b": "≈2 ГБ RAM, быстро, попроще",
    "qwen2.5:3b": "≈3–4 ГБ RAM, оптимально",
    "gemma3:4b": "≈5 ГБ RAM, лучше пишет по-русски",
    "qwen2.5:7b": "≈8 ГБ RAM, лучшее качество",
}


class AIError(RuntimeError):
    pass


class Backend:
    name = "base"

    def chat(self, system: str, user: str, temperature: float, want_json: bool) -> str:
        raise NotImplementedError


class OllamaBackend(Backend):
    """Нативный API Ollama: умеет format=json и большой контекст."""

    _pull_lock = threading.Lock()

    def __init__(self, url: str, model: str, timeout: float = 600):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.name = f"local:{model}"

    def available(self) -> bool:
        try:
            tags = httpx.get(f"{self.url}/api/tags", timeout=5).json()
        except (httpx.HTTPError, ValueError):
            return False
        names = {m.get("name") for m in tags.get("models", [])}
        return self.model in names or f"{self.model}:latest" in names

    def ensure_model(self) -> bool:
        """Скачивает модель, если её ещё нет. Вызывается в фоне при старте."""
        if not self._pull_lock.acquire(blocking=False):
            return False  # уже качается в другом потоке — не дублируем
        try:
            if self.available():
                return True
            try:
                log.info("Скачиваю локальную модель %s (один раз, несколько минут)…", self.model)
                resp = httpx.post(
                    f"{self.url}/api/pull", json={"model": self.model, "stream": False}, timeout=3600
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Не удалось скачать модель %s: %s", self.model, exc)
                return False
            return self.available()
        finally:
            self._pull_lock.release()

    def chat(self, system: str, user: str, temperature: float, want_json: bool) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": temperature, "num_ctx": 8192},
        }
        if want_json:
            body["format"] = "json"
        try:
            resp = httpx.post(f"{self.url}/api/chat", json=body, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise AIError(f"Локальная модель недоступна: {exc}") from exc
        if resp.status_code >= 400:
            raise AIError(f"Локальная модель: {resp.status_code} {resp.text[:200]}")
        return resp.json()["message"]["content"].strip()


class OpenAICompatBackend(Backend):
    def __init__(self, base_url: str, model: str, api_key: str = "", name: str = "", timeout: float = 120):
        self.url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.name = name or f"api:{model}"

    def chat(self, system: str, user: str, temperature: float, want_json: bool) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": self.model,
            "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        endpoint = self.url if self.url.endswith(("/openai", "/completions")) else f"{self.url}/chat/completions"
        try:
            resp = httpx.post(endpoint, json=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise AIError(f"{self.name} недоступен: {exc}") from exc
        if resp.status_code >= 400:
            raise AIError(f"{self.name}: {resp.status_code} {resp.text[:200]}")
        try:
            return resp.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise AIError(f"{self.name}: неожиданный ответ {resp.text[:200]}") from exc


class AIClient:
    """Перебирает движки по очереди, пока какой-то не ответит."""

    def __init__(self, backends: list[Backend]):
        if not backends:
            raise ValueError("нужен хотя бы один движок")
        self.backends = backends
        self.last_used = backends[0].name

    @property
    def name(self) -> str:
        return self.last_used

    @property
    def local(self) -> OllamaBackend | None:
        return next((b for b in self.backends if isinstance(b, OllamaBackend)), None)

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "AIClient | None":
        mode = settings.get("ai_mode") or "auto"
        if mode == "off":
            return None
        backends: list[Backend] = []
        if settings.get("ai_custom_url"):
            backends.append(
                OpenAICompatBackend(
                    settings["ai_custom_url"], settings.get("ai_custom_model") or "default",
                    settings.get("ai_api_key", ""), name="custom",
                )
            )
        if mode in {"auto", "local"}:
            backends.append(
                OllamaBackend(
                    settings.get("ai_local_url") or DEFAULT_LOCAL_URL,
                    settings.get("ai_local_model") or DEFAULT_LOCAL_MODEL,
                )
            )
        if mode in {"auto", "cloud"}:
            backends.append(OpenAICompatBackend(POLLINATIONS_URL, "openai", name="pollinations (без ключа)"))
        return cls(backends) if backends else None

    def chat(self, system: str, user: str, temperature: float = 0.4, want_json: bool = False) -> str:
        errors = []
        for backend in self.backends:
            try:
                answer = backend.chat(system, user, temperature, want_json)
                self.last_used = backend.name
                return answer
            except (AIError, KeyError, ValueError) as exc:
                errors.append(str(exc))
        raise AIError("; ".join(errors))

    def chat_json(self, system: str, user: str) -> Any:
        return extract_json(self.chat(system, user, temperature=0, want_json=True))


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
