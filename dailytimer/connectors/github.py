"""GitHub: уведомления, назначенные задачи, запросы на ревью, свои открытые PR.

GitHub с 2021 года не пускает в API по паролю, поэтому нужен Personal Access Token
(Settings → Developer settings → Tokens). Хватает прав: repo (read), notifications, read:user.
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def _issue(item: dict[str, Any]) -> dict[str, Any]:
    repo = item.get("repository_url", "").removeprefix(f"{API}/repos/")
    return {
        "title": item.get("title"),
        "url": item.get("html_url"),
        "repo": repo,
        "number": item.get("number"),
        "is_pr": "pull_request" in item,
        "updated_at": item.get("updated_at"),
        "labels": [label.get("name") for label in item.get("labels", [])],
    }


def fetch(token: str, client: httpx.Client | None = None) -> dict[str, Any]:
    if not token:
        raise GitHubError("Не указан GitHub токен")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    own_client = client is None
    client = client or httpx.Client(timeout=30)
    try:
        def get(path: str, **params: Any) -> Any:
            resp = client.get(f"{API}{path}", headers=headers, params=params)
            if resp.status_code == 401:
                raise GitHubError("GitHub отклонил токен (401) — проверь, что он не истёк")
            resp.raise_for_status()
            return resp.json()

        def search(query: str) -> list[dict[str, Any]]:
            return [_issue(i) for i in get("/search/issues", q=query, per_page=30, sort="updated").get("items", [])]

        user = get("/user")
        login = user["login"]
        notifications = [
            {
                "title": n["subject"]["title"],
                "type": n["subject"]["type"],
                "reason": n["reason"],
                "repo": n["repository"]["full_name"],
                "updated_at": n["updated_at"],
            }
            for n in get("/notifications", per_page=30)
        ]
        return {
            "login": login,
            "notifications": notifications,
            "review_requests": search(f"is:open is:pr review-requested:{login} archived:false"),
            "assigned": search(f"is:open assignee:{login} archived:false"),
            "my_prs": search(f"is:open is:pr author:{login} archived:false"),
        }
    except httpx.HTTPError as exc:
        raise GitHubError(f"Ошибка GitHub API: {exc}") from exc
    finally:
        if own_client:
            client.close()
