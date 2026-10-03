"""Раздел «Сервер»: состояние VPS, порты, контейнеры, git-проекты и обновление по кнопке."""

from __future__ import annotations

import threading
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import sync
from ..connectors import vps
from ..storage import Storage


def register(app: FastAPI, storage: Storage, templates: Jinja2Templates, auth: Callable[..., Any]) -> None:
    guard = [Depends(auth)]
    templates.env.filters["bytes"] = vps.human_bytes
    templates.env.filters["uptime"] = vps.human_uptime
    checking = threading.Lock()

    def agent() -> vps.Agent | None:
        return vps.Agent.from_settings(storage.get_settings())

    @app.get("/server", response_class=HTMLResponse, dependencies=guard)
    def server_page(request: Request) -> Any:
        client = agent()
        status, error = None, None
        if client:
            try:
                status = client.status()
                storage.save_snapshot("server", status)
            except vps.AgentError as exc:
                error = str(exc)
                status = storage.get_snapshot("server")["data"]
        projects_snap = storage.get_snapshot("server_projects")
        projects = (projects_snap["data"] or {}).get("projects", [])
        containers = (status or {}).get("containers", [])
        by_project: dict[str, list[dict[str, Any]]] = {}
        for c in containers:
            by_project.setdefault(c.get("project") or "", []).append(c)
        for p in projects:
            p["containers"] = by_project.get(p["name"].lower()) or by_project.get(p["name"], [])
        return templates.TemplateResponse(request, "server.html", {
            "configured": client is not None, "status": status, "error": error, "projects": projects,
            "projects_at": projects_snap["updated_at"], "projects_error": projects_snap["error"],
            "checking": checking.locked(), "settings": storage.get_settings(),
        })

    @app.post("/server/check", dependencies=guard)
    def server_check() -> Any:
        client = agent()

        def work() -> None:
            if not checking.acquire(blocking=False):
                return
            try:
                sync.refresh_projects(storage, storage.get_settings(), client, fetch=True)
            except Exception as exc:  # noqa: BLE001 — показываем ошибку на странице
                storage.save_snapshot("server_projects", None, error=str(exc))
            finally:
                checking.release()

        if client:
            threading.Thread(target=work, daemon=True).start()
        return RedirectResponse("/server?checking=1", status_code=303)

    @app.post("/api/server/update", dependencies=guard)
    async def api_update(request: Request) -> Any:
        body = await request.json()
        client = agent()
        if not client:
            return JSONResponse({"error": "Агент не настроен"}, status_code=400)
        try:
            if body.get("container"):
                return {"job": await run_in_threadpool(client.restart, str(body["container"]))}
            return {"job": await run_in_threadpool(client.update, str(body.get("path", "")))}
        except vps.AgentError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/server/jobs/{job_id}", dependencies=guard)
    def api_job(job_id: str) -> Any:
        client = agent()
        if not client:
            return JSONResponse({"error": "Агент не настроен"}, status_code=400)
        try:
            job = client.job(job_id)
        except vps.AgentError as exc:
            # Во время обновления самого DailyTimer связь ненадолго пропадает — это нормально.
            return JSONResponse({"status": "unknown", "log": f"(нет связи с агентом: {exc})"})
        if job.get("status") in {"ok", "error"}:
            threading.Thread(target=lambda: _refresh_quietly(storage, client), daemon=True).start()
        return job


def _refresh_quietly(storage: Storage, client: vps.Agent) -> None:
    try:
        sync.refresh_projects(storage, storage.get_settings(), client, fetch=False)
    except Exception:  # noqa: BLE001
        pass
