"""Utility helpers for interacting with the Taiga REST API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping, Sequence

import httpx
from httpx import Response
from httpx._types import QueryParamTypes


def _safe_json(response: Response) -> Any | None:
    try:
        return response.json()
    except ValueError:  # pragma: no cover - defensive fallback
        text = response.text
        return text if text else None


def _extract_pagination(headers: Mapping[str, str]) -> dict[str, Any]:
    mapping = {
        "x-pagination-page": "page",
        "x-pagination-page-size": "page_size",
        "x-pagination-total": "total",
        "x-pagination-pages": "total_pages",
    }
    pagination: dict[str, Any] = {}
    for header_name, field in mapping.items():
        value = headers.get(header_name)
        if value is None:
            continue
        try:
            pagination[field] = int(value)
        except ValueError:  # pragma: no cover - leave raw value for diagnostics
            pagination[field] = value
    return pagination

__all__ = [
    "TaigaAPIError",
    "get_taiga_client",
]


class TaigaAPIError(RuntimeError):
    """Raised when the Taiga API responds with an error."""

    def __init__(self, message: str, *, status_code: int | None = None, payload: Any | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise TaigaAPIError(f"Environment variable {name} must be configured")
    return value


class TaigaClient:
    """Thin async wrapper around Taiga's REST API."""

    def __init__(self) -> None:
        base_url = _require_env("TAIGA_BASE_URL")
        # Normalise base URL to avoid eventual double slashes.
        base_url = base_url.rstrip("/")

        self._username = _require_env("TAIGA_USERNAME")
        self._password = _require_env("TAIGA_PASSWORD")

        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=30.0,
            headers={"Accept": "application/json"},
        )
        self._auth_token: str | None = None
        self._user_id: int | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def authenticate(self) -> None:
        if self._auth_token:
            return
        payload = {
            "type": "normal",
            "username": self._username,
            "password": self._password,
        }
        response = await self._client.post("auth", json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - error details for humans
            detail = exc.response.text
            raise TaigaAPIError(
                f"Taiga authentication failed with status {exc.response.status_code}: {detail}",
                status_code=exc.response.status_code,
                payload=_safe_json(exc.response),
            ) from exc

        try:
            data = response.json()
            token = data["auth_token"]
        except (ValueError, KeyError) as exc:  # pragma: no cover - error details for humans
            raise TaigaAPIError("Taiga authentication response did not contain auth_token") from exc

        self._auth_token = token
        self._client.headers["Authorization"] = f"Bearer {token}"
        user_id = data.get("id") if isinstance(data, dict) else None
        if user_id is not None:
            try:
                self._user_id = int(user_id)
            except (TypeError, ValueError):  # pragma: no cover - defensive default
                self._user_id = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: QueryParamTypes | None = None,
        json: Mapping[str, Any] | None = None,
    ) -> Any:
        path = path.lstrip("/")
        response = await self._client.request(method, path, params=params, json=json)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - error details for humans
            detail = exc.response.text
            raise TaigaAPIError(
                f"Taiga API request failed with status {exc.response.status_code}: {detail}",
                status_code=exc.response.status_code,
                payload=_safe_json(exc.response),
            ) from exc
        if response.content:
            return response.json()
        return None

    async def list_projects(
        self,
        *,
        params: QueryParamTypes | None = None,
    ) -> list[dict[str, Any]]:
        data = await self._request("GET", "/projects", params=params)
        return list(data)

    async def get_current_user_id(self) -> int:
        if self._user_id is not None:
            return self._user_id

        data = await self._request("GET", "/users/me")
        if not isinstance(data, dict):
            raise TaigaAPIError("Taiga API did not return user details")
        try:
            user_id = int(data["id"])
        except (KeyError, TypeError, ValueError) as exc:  # pragma: no cover - defensive default
            raise TaigaAPIError("Taiga API did not provide the authenticated user id") from exc

        self._user_id = user_id
        return user_id

    async def get_project(self, project_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"/projects/{project_id}")
        return dict(data)

    async def get_project_by_slug(self, slug: str) -> dict[str, Any]:
        params = {"slug": slug}
        data = await self._request("GET", "/projects/by_slug", params=params)
        return dict(data)

    async def list_epics(self, project_id: int) -> list[dict[str, Any]]:
        params = {"project": project_id}
        data = await self._request("GET", "/epics", params=params)
        return list(data)

    async def list_user_stories(
        self,
        project_id: int,
        *,
        epic: int | None = None,
        q: str | None = None,
        tags: Sequence[str] | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [("project", project_id)]
        if epic is not None:
            params.append(("epic", epic))
        if q:
            params.append(("q", q))
        if tags:
            for tag in tags:
                params.append(("tags", tag))
        if page is not None:
            params.append(("page", page))
        if page_size is not None:
            params.append(("page_size", page_size))

        data = await self._request("GET", "/userstories", params=params)
        return list(data)

    async def list_user_story_statuses(self, project_id: int) -> list[dict[str, Any]]:
        params = {"project": project_id}
        data = await self._request("GET", "/userstory-statuses", params=params)
        return list(data)

    async def list_task_statuses(self, project_id: int) -> list[dict[str, Any]]:
        params = {"project": project_id}
        data = await self._request("GET", "/task-statuses", params=params)
        return list(data)

    async def create_user_story(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", "/userstories", json=payload)
        return dict(data)

    async def get_user_story(self, story_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"/userstories/{story_id}")
        return dict(data)

    async def update_user_story(self, story_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._request("PATCH", f"/userstories/{story_id}", json=payload)
        return dict(data)

    async def delete_user_story(self, story_id: int) -> None:
        await self._request("DELETE", f"/userstories/{story_id}")

    async def link_epic_user_story(self, epic_id: int, user_story_id: int) -> dict[str, Any] | None:
        payload = {
            "epic": epic_id,
            "user_story": user_story_id,
        }
        data = await self._request(
            "POST",
            f"/epics/{epic_id}/related_userstories",
            json=payload,
        )
        return dict(data) if data else None

    async def create_epic(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", "/epics", json=payload)
        return dict(data)

    async def get_epic(self, epic_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"/epics/{epic_id}")
        return dict(data)

    async def update_epic(self, epic_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._request("PATCH", f"/epics/{epic_id}", json=payload)
        return dict(data)

    async def delete_epic(self, epic_id: int) -> None:
        await self._request("DELETE", f"/epics/{epic_id}")

    async def create_task(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", "/tasks", json=payload)
        return dict(data)

    async def get_task(self, task_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"/tasks/{task_id}")
        return dict(data)

    async def update_task(self, task_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._request("PATCH", f"/tasks/{task_id}", json=payload)
        return dict(data)

    async def delete_task(self, task_id: int) -> None:
        await self._request("DELETE", f"/tasks/{task_id}")

    async def list_tasks(
        self,
        *,
        project_id: int | None = None,
        user_story_id: int | None = None,
        assigned_to: int | None = None,
        search: str | None = None,
        status: int | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        params: list[tuple[str, Any]] = []
        if project_id is not None:
            params.append(("project", project_id))
        if user_story_id is not None:
            params.append(("user_story", user_story_id))
        if assigned_to is not None:
            params.append(("assigned_to", assigned_to))
        if search:
            params.append(("q", search))
        if status is not None:
            params.append(("status", status))
        if page is not None:
            params.append(("page", page))
        if page_size is not None:
            params.append(("page_size", page_size))

        response = await self._client.get("/tasks", params=params or None)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - error details for humans
            raise TaigaAPIError(
                f"Taiga API request failed with status {exc.response.status_code}: {exc.response.text}",
                status_code=exc.response.status_code,
                payload=_safe_json(exc.response),
            ) from exc

        pagination = _extract_pagination(response.headers)
        data = response.json() if response.content else []
        return list(data), pagination

    async def create_issue(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", "/issues", json=payload)
        return dict(data)

    async def get_issue(self, issue_id: int) -> dict[str, Any]:
        data = await self._request("GET", f"/issues/{issue_id}")
        return dict(data)

    async def update_issue(self, issue_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
        data = await self._request("PATCH", f"/issues/{issue_id}", json=payload)
        return dict(data)

    async def delete_issue(self, issue_id: int) -> None:
        await self._request("DELETE", f"/issues/{issue_id}")

    async def list_users(self, *, search: str | None = None) -> list[dict[str, Any]]:
        params = {"search": search} if search else None
        data = await self._request("GET", "/users", params=params)
        return list(data)

    async def list_milestones(self, project_id: int) -> list[dict[str, Any]]:
        params = {"project": project_id}
        data = await self._request("GET", "/milestones", params=params)
        return list(data)


@asynccontextmanager
async def get_taiga_client() -> AsyncIterator[TaigaClient]:
    client = TaigaClient()
    try:
        await client.authenticate()
        yield client
    finally:
        await client.close()
