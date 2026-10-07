"""Раздел «Проекты»: сводки из других чатов с Claude, их этапы и доступность сайтов."""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import sync
from ..projects import REPORT_PROMPT, STATUSES, Projects
from ..storage import Storage


def _repo_slug(text: str | None) -> str:
    """«0oda0/OBDCheker (приватный, https://github.com/…)» → «0oda0/obdcheker»."""
    text = (text or "").strip()
    m = re.search(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?(?=[/\s),]|$)", text) \
        or re.match(r"([\w.-]+/[\w.-]+?)(?:\.git)?(?=[\s(,]|$)", text)
    return m.group(1).lower() if m else ""


def _first_path(text: str | None) -> str:
    m = re.search(r"(/[\w.-]+(?:/[\w.-]+)*)", text or "")
    return m.group(1).rstrip("/") if m else ""


def _server_info(storage: Storage, project: dict[str, Any]) -> dict[str, Any] | None:
    """Связывает проект со своим git-репозиторием на сервере (раздел «Сервер»)."""
    server_projects = (storage.get_snapshot("server_projects")["data"] or {}).get("projects", [])
    location = project.get("location") or {}
    slug = _repo_slug(location.get("repo"))
    deploy = _first_path(location.get("deploy_path"))
    for sp in server_projects:
        if slug and (sp.get("github") or "").lower() == slug:
            return sp
        if deploy and sp.get("path", "").rstrip("/") == deploy:
            return sp
    return None


def register(app: FastAPI, storage: Storage, templates: Jinja2Templates, auth: Callable[..., Any]) -> None:
    guard = [Depends(auth)]
    projects = Projects(storage)
    templates.env.globals["PROJECT_STATUSES"] = STATUSES

    @app.get("/projects", response_class=HTMLResponse, dependencies=guard)
    def projects_page(request: Request) -> Any:
        items = projects.all()
        for p in items:
            p["server"] = _server_info(storage, p)
            p["health_url"] = projects.health_url(p)
        counts: dict[str, int] = {}
        for p in items:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        q = request.query_params
        return templates.TemplateResponse(request, "projects.html", {
            "projects": items, "counts": counts, "prompt": REPORT_PROMPT,
            "down": [p for p in items if (p.get("health") or {}).get("ok") is False],
            "result": {k: q.get(k) for k in ("added", "updated", "redacted", "error", "deleted", "checked")},
        })

    @app.post("/projects/import", dependencies=guard)
    async def projects_import(request: Request) -> Any:
        form = await request.form()
        try:
            result = await run_in_threadpool(projects.import_text, str(form.get("report", "")))
        except ValueError as exc:
            return RedirectResponse("/projects?" + urlencode({"error": str(exc)}), status_code=303)
        threading.Thread(target=sync.check_projects, args=(storage, False), daemon=True).start()
        return RedirectResponse("/projects?" + urlencode({
            "added": ", ".join(result["added"]), "updated": ", ".join(result["updated"]),
            "redacted": result["redacted"] or ""}), status_code=303)

    @app.post("/projects/check", dependencies=guard)
    async def projects_check() -> Any:
        await run_in_threadpool(sync.check_projects, storage, True)
        return RedirectResponse("/projects?checked=1", status_code=303)

    @app.get("/projects/{project_id}", response_class=HTMLResponse, dependencies=guard)
    def project_page(request: Request, project_id: str) -> Any:
        project = projects.get(project_id)
        if not project:
            raise HTTPException(404, "Проект не найден")
        project["server"] = _server_info(storage, project)
        project["health_url"] = projects.health_url(project)
        editable = {k: v for k, v in project.items()
                    if k not in {"server", "health_url", "health", "history", "created_at", "updated_at",
                                 "source_chat", "report_date", "stale"}}
        if project.get("progress") is not None:
            editable["progress_percent"] = editable.pop("progress")
        q = request.query_params
        return templates.TemplateResponse(request, "project.html", {
            "p": project, "editable": json.dumps(editable, ensure_ascii=False, indent=2),
            "error": q.get("error"), "saved": q.get("saved"),
        })

    @app.post("/projects/{project_id}/edit", dependencies=guard)
    async def project_edit(request: Request, project_id: str) -> Any:
        if not projects.get(project_id):
            raise HTTPException(404, "Проект не найден")
        form = await request.form()
        try:
            await run_in_threadpool(projects.import_text, str(form.get("report", "")), project_id)
        except ValueError as exc:
            return RedirectResponse(f"/projects/{project_id}?" + urlencode({"error": str(exc)}), status_code=303)
        return RedirectResponse(f"/projects/{project_id}?saved=1", status_code=303)

    @app.post("/projects/{project_id}/delete", dependencies=guard)
    def project_delete(project_id: str) -> Any:
        project = projects.get(project_id)
        projects.delete(project_id)
        return RedirectResponse("/projects?" + urlencode({"deleted": (project or {}).get("name", "")}),
                                status_code=303)

    @app.get("/api/projects", dependencies=guard)
    def api_projects() -> Any:
        return {"projects": projects.all()}
