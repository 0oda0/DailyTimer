"""Сервер (VPS): клиент к агенту на хосте + статус проектов на GitHub."""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_URL = os.environ.get("VPS_AGENT_URL", "http://host.docker.internal:9137")


class AgentError(RuntimeError):
    pass


class Agent:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    @classmethod
    def from_settings(cls, settings: dict[str, Any]) -> "Agent | None":
        token = settings.get("server_agent_token") or os.environ.get("VPS_AGENT_TOKEN", "")
        if not token:
            return None
        return cls(settings.get("server_agent_url") or DEFAULT_URL, token)

    def _request(self, method: str, path: str, timeout: float = 60, **kwargs: Any) -> Any:
        try:
            resp = httpx.request(method, f"{self.url}{path}", headers={"X-Agent-Token": self.token},
                                 timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise AgentError(f"Агент сервера недоступен ({self.url}): {exc}") from exc
        if resp.status_code == 401:
            raise AgentError("Агент отклонил токен — проверь его в настройках раздела «Сервер»")
        try:
            data = resp.json()
        except ValueError as exc:
            raise AgentError(f"Агент ответил не JSON: {resp.text[:200]}") from exc
        if resp.status_code >= 400:
            raise AgentError(data.get("error") or f"Ошибка агента {resp.status_code}")
        return data

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/status")

    def projects(self, fetch: bool = False) -> list[dict[str, Any]]:
        return self._request("GET", "/projects" + ("?fetch=1" if fetch else ""), timeout=300)["projects"]

    def update(self, path: str) -> str:
        return self._request("POST", "/projects/update", json={"path": path})["job"]

    def restart(self, name: str) -> str:
        return self._request("POST", "/containers/restart", json={"name": name})["job"]

    def job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/jobs/{job_id}")


def github_status(slug: str, branch: str, token: str) -> dict[str, Any]:
    """Последний прогон CI и открытые PR репозитория на GitHub."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    info: dict[str, Any] = {}
    try:
        with httpx.Client(timeout=20, headers=headers) as client:
            runs = client.get(f"https://api.github.com/repos/{slug}/actions/runs",
                              params={"branch": branch, "per_page": 1})
            if runs.status_code == 200 and runs.json().get("workflow_runs"):
                run = runs.json()["workflow_runs"][0]
                info["ci"] = {"id": run["id"], "name": run.get("name"), "status": run.get("status"),
                              "conclusion": run.get("conclusion"), "url": run.get("html_url"),
                              "sha": (run.get("head_sha") or "")[:7]}
            pulls = client.get(f"https://api.github.com/repos/{slug}/pulls", params={"state": "open", "per_page": 30})
            if pulls.status_code == 200:
                info["open_prs"] = len(pulls.json())
    except httpx.HTTPError:
        pass
    return info


def human_bytes(value: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(value) < 1024 or unit == "ТБ":
            return f"{value:.1f} {unit}" if unit != "Б" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def human_uptime(seconds: int) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return (f"{days} д " if days else "") + f"{hours} ч {minutes} мин"
