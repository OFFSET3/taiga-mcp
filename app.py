import asyncio
import datetime
import hashlib
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from json import JSONDecodeError
from typing import Any, Sequence

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from taiga_client import TaigaAPIError, get_taiga_client
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

mcp = FastMCP("Taiga MCP", sse_path="/", streamable_http_path="/")
# Prebuild sub-apps so we can wire their lifespans into the parent Starlette app.
sse_subapp = mcp.sse_app()


@sse_subapp.middleware("http")
async def _normalize_sse_path(request, call_next):
    # Mounted sub-apps may receive an empty path for their root routes.
    if request.scope.get("path") in ("", None):
        request.scope["path"] = "/"
        request.scope["raw_path"] = b"/"
    return await call_next(request)
streamable_http_subapp = mcp.streamable_http_app()
streamable_http_subapp.router.redirect_slashes = False


@streamable_http_subapp.middleware("http")
async def _normalize_blank_path(request, call_next):
    # Starlette mounts strip the trailing slash, leaving an empty path for "/mcp".
    # Ensure the downstream Streamable HTTP route sees the root path.
    if request.scope.get("path") == "":
        request.scope["path"] = "/"
        request.scope["raw_path"] = b"/"
    return await call_next(request)


@mcp.tool(annotations=ToolAnnotations(openWorldHint=True))
def echo(message: str) -> str:
    """Echo a message back to the caller."""
    return message


def _slice(record: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: record.get(key) for key in keys if key in record}


def _error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _expected_api_key() -> str | None:
    return os.getenv("ACTION_PROXY_API_KEY")


def _verify_api_key(request: Request) -> JSONResponse | None:
    expected = _expected_api_key()
    if not expected:
        return _error_response("Proxy API key is not configured", 503)

    provided = request.headers.get("X-Api-Key")
    if not provided:
        return _error_response("Missing X-Api-Key header", 401)

    if not secrets.compare_digest(provided, expected):
        return _error_response("Invalid API key", 401)

    return None


async def _get_json_body(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        data = await request.json()
    except JSONDecodeError:
        return None, _error_response("Request body must be valid JSON", 400)

    if not isinstance(data, dict):
        return None, _error_response("Request body must be a JSON object", 400)

    return data, None


def _parse_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer") from None


def _optional_int(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _parse_int(value, field)


ActionCall = Callable[[Any], Awaitable[Any]]


async def _call_taiga(action: ActionCall) -> Any:
    async with get_taiga_client() as client:
        return await action(client)


class _IdempotencyStore:
    def __init__(self, ttl_seconds: int = 24 * 60 * 60) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()
        self._entries: dict[str, tuple[float, dict[str, Any]]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        async with self._lock:
            self._purge_expired()
            entry = self._entries.get(key)
            if entry is None:
                return None
            _, value = entry
            return dict(value)

    async def store(self, key: str, value: dict[str, Any]) -> None:
        async with self._lock:
            self._purge_expired()
            expires_at = time.time() + self._ttl_seconds
            self._entries[key] = (expires_at, dict(value))

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [cache_key for cache_key, (expires_at, _) in self._entries.items() if expires_at <= now]
        for cache_key in expired:
            self._entries.pop(cache_key, None)


_IDEMPOTENCY_STORE = _IdempotencyStore()


class ToolInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def provided_fields(self) -> set[str]:
        return set(self.model_fields_set)


class StoryUpdateInput(ToolInputModel):
    user_story_id: int
    subject: str | None = None
    description: str | None = None
    status: int | str | None = None
    tags: list[str] | None = None
    assigned_to: int | None = None
    epic_id: int | None = None
    milestone_id: int | None = None
    custom_attributes: dict[str, Any] | None = None
    version: int | None = None


class TaskCreateInput(ToolInputModel):
    user_story_id: int
    subject: str
    description: str | None = None
    assigned_to: int | None = None
    status: int | str | None = None
    tags: list[str] | None = None
    due_date: str | None = None
    idempotency_key: str | None = None


class TaskUpdateInput(ToolInputModel):
    task_id: int
    subject: str | None = None
    description: str | None = None
    assigned_to: int | None = None
    status: int | str | None = None
    tags: list[str] | None = None
    due_date: str | None = None
    version: int | None = None


class TaskListInput(ToolInputModel):
    project_id: int | None = None
    user_story_id: int | None = None
    assigned_to: int | None = None
    search: str | None = None
    status: int | str | None = None
    page: int | None = None
    page_size: int | None = None


async def _list_projects_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    raw_params = list(request.query_params.multi_items())
    search: str | None = None
    filtered_params: dict[str, str] = {}
    for key, value in raw_params:
        if key == "search":
            search = value
            continue
        filtered_params[key] = value

    try:
        async with get_taiga_client() as client:
            params: dict[str, str] = dict(filtered_params)
            if "member" not in params:
                member_id = await client.get_current_user_id()
                params["member"] = str(member_id)
            projects = await client.list_projects(params=params)
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while listing projects")
        return _error_response("Internal server error", 500)

    keep = ("id", "name", "slug", "description", "is_private")
    filtered = []
    for project in projects:
        if search:
            name = project.get("name", "")
            if search.lower() not in name.lower():
                continue
        filtered.append(_slice(project, keep))

    return JSONResponse({"projects": filtered})


async def _get_project_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    project_id_param = request.query_params.get("project_id")
    if not project_id_param:
        return _error_response("project_id is required", 400)

    try:
        project_id = int(project_id_param)
    except ValueError:
        return _error_response("project_id must be an integer", 400)

    try:
        project = await _call_taiga(lambda client: client.get_project(project_id))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while retrieving project", exc_info=True)
        return _error_response("Internal server error", 500)

    return JSONResponse({"project": project})


async def _get_project_by_slug_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    slug = request.query_params.get("slug")
    if not slug:
        return _error_response("slug is required", 400)

    try:
        project = await _call_taiga(lambda client: client.get_project_by_slug(slug))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while retrieving project by slug", exc_info=True)
        return _error_response("Internal server error", 500)

    return JSONResponse({"project": project})


async def _list_epics_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    project_ids = request.query_params.getlist("project_id")
    if not project_ids:
        return _error_response("At least one project_id is required", 400)

    try:
        parsed_ids = [int(value) for value in project_ids]
    except ValueError:
        return _error_response("project_id must be an integer", 400)

    keep = ("id", "ref", "subject", "created_date", "modified_date", "status")
    epics: list[dict[str, Any]] = []

    try:
        for project_id in parsed_ids:
            project_epics = await _call_taiga(lambda client, pid=project_id: client.list_epics(pid))
            for epic in project_epics:
                data = _slice(epic, keep)
                data["project_id"] = project_id
                epics.append(data)
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while listing epics")
        return _error_response("Internal server error", 500)

    return JSONResponse({"epics": epics})


async def _list_user_stories_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    project_id_param = request.query_params.get("project_id")
    if not project_id_param:
        return _error_response("project_id is required", 400)

    try:
        project_id = _parse_int(project_id_param, "project_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    epic_param = request.query_params.get("epic_id") or request.query_params.get("epic")
    try:
        epic_id = _optional_int(epic_param, "epic_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    search = request.query_params.get("search") or request.query_params.get("q")

    tags = request.query_params.getlist("tag")
    if not tags:
        tags = request.query_params.getlist("tags")

    try:
        page = _optional_int(request.query_params.get("page"), "page")
        page_size = _optional_int(request.query_params.get("page_size"), "page_size")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    try:
        stories = await _call_taiga(
            lambda client: client.list_user_stories(
                project_id,
                epic=epic_id,
                q=search,
                tags=tags or None,
                page=page,
                page_size=page_size,
            )
        )
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while listing user stories")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "description",
        "project",
        "epic",
        "epics",
        "tags",
        "status",
        "status_extra_info",
        "assigned_to",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"stories": [_slice(story, keep) for story in stories]})


async def _list_statuses_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    project_id_param = request.query_params.get("project_id")
    if not project_id_param:
        return _error_response("project_id is required", 400)

    try:
        project_id = int(project_id_param)
    except ValueError:
        return _error_response("project_id must be an integer", 400)

    try:
        statuses = await _call_taiga(lambda client: client.list_user_story_statuses(project_id))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while listing statuses")
        return _error_response("Internal server error", 500)

    keep = ("id", "name", "slug", "is_closed", "order")
    return JSONResponse({"statuses": [_slice(status, keep) for status in statuses]})


async def _create_story_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    try:
        data = await request.json()
    except JSONDecodeError:
        return _error_response("Request body must be valid JSON", 400)

    required_fields = ("project_id", "subject")
    for field in required_fields:
        if field not in data:
            return _error_response(f"Field '{field}' is required", 400)

    try:
        project_id = int(data["project_id"])
    except (TypeError, ValueError):
        return _error_response("project_id must be an integer", 400)

    status = data.get("status")
    if status is not None and not isinstance(status, (int, str)):
        return _error_response("status must be an integer or string", 400)

    tags = data.get("tags")
    if tags is not None and not isinstance(tags, list):
        return _error_response("tags must be a list", 400)

    assigned_to = data.get("assigned_to")
    if assigned_to is not None:
        try:
            assigned_to = int(assigned_to)
        except (TypeError, ValueError):
            return _error_response("assigned_to must be an integer", 400)

    try:
        story = await _call_taiga(
            lambda client: _create_story_with_client(
                client,
                project_id=project_id,
                subject=str(data.get("subject", "")),
                description=data.get("description"),
                status=status,
                tags=tags,
                assigned_to=assigned_to,
            )
        )
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while creating story")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "description",
        "assigned_to",
        "tags",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"story": _slice(story, keep)})


async def _create_story_with_client(
    client,
    *,
    project_id: int,
    subject: str,
    description: str | None,
    status: int | str | None,
    tags: list[str] | None,
    assigned_to: int | None,
) -> dict[str, Any]:
    status_id = await _resolve_user_story_status_id(client, project_id, status)
    payload: dict[str, Any] = {
        "project": project_id,
        "subject": subject,
    }
    if description:
        payload["description"] = description
    if status_id is not None:
        payload["status"] = status_id
    if tags:
        payload["tags"] = tags
    if assigned_to is not None:
        payload["assigned_to"] = assigned_to
    return await client.create_user_story(payload)


async def _add_story_to_epic_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    try:
        data = await request.json()
    except JSONDecodeError:
        return _error_response("Request body must be valid JSON", 400)

    required_fields = ("epic_id", "user_story_id")
    for field in required_fields:
        if field not in data:
            return _error_response(f"Field '{field}' is required", 400)

    try:
        epic_id = int(data["epic_id"])
        user_story_id = int(data["user_story_id"])
    except (TypeError, ValueError):
        return _error_response("epic_id and user_story_id must be integers", 400)

    try:
        link = await _call_taiga(
            lambda client: client.link_epic_user_story(epic_id, user_story_id)
        )
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while linking story to epic")
        return _error_response("Internal server error", 500)

    return JSONResponse({"link": link})


async def _update_story_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    if "story_id" not in data:
        return _error_response("Field 'story_id' is required", 400)

    try:
        story_id = _parse_int(data["story_id"], "story_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    payload: dict[str, Any] = {}
    project_for_status: int | None = None

    if "project_id" in data:
        try:
            project_for_status = _parse_int(data["project_id"], "project_id")
        except ValueError as exc:
            return _error_response(str(exc), 400)
        payload["project"] = project_for_status

    if "subject" in data:
        payload["subject"] = str(data["subject"])

    if "description" in data:
        payload["description"] = data["description"]

    if "tags" in data:
        tags = data["tags"]
        if tags is not None and not isinstance(tags, list):
            return _error_response("tags must be a list", 400)
        payload["tags"] = tags

    if "assigned_to" in data:
        assigned_to = data["assigned_to"]
        if assigned_to is None:
            payload["assigned_to"] = None
        else:
            try:
                payload["assigned_to"] = _parse_int(assigned_to, "assigned_to")
            except ValueError as exc:
                return _error_response(str(exc), 400)

    status_present = "status" in data
    status_value = data.get("status") if status_present else None
    if status_present and status_value is None:
        return _error_response("status cannot be null", 400)
    if not payload and not status_present:
        return _error_response("At least one field must be provided to update", 400)

    try:
        story = await _call_taiga(
            lambda client: _update_story_with_client(
                client,
                story_id=story_id,
                project_for_status=project_for_status,
                payload=payload,
                status=status_value,
            )
        )
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while updating story")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "description",
        "assigned_to",
        "tags",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"story": _slice(story, keep)})


async def _update_story_with_client(
    client,
    *,
    story_id: int,
    project_for_status: int | None,
    payload: dict[str, Any],
    status: int | str | None,
) -> dict[str, Any]:
    existing = await client.get_user_story(story_id)

    update_payload = dict(payload)

    project_id_for_status = project_for_status or existing.get("project")
    if project_id_for_status is not None:
        try:
            project_id_for_status = int(project_id_for_status)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for story status lookup") from None

    if status is not None:
        if isinstance(status, int):
            update_payload["status"] = status
        elif isinstance(status, str):
            if project_id_for_status is None:
                raise TaigaAPIError("Unable to resolve project for story status lookup")
            status_id = await _resolve_user_story_status_id(client, project_id_for_status, status)
            update_payload["status"] = status_id
        else:
            raise TaigaAPIError("status must be an integer or string")

    version = existing.get("version")
    if version is None:
        raise TaigaAPIError("Unable to resolve version for story update")
    try:
        update_payload["version"] = int(version)
    except (TypeError, ValueError):
        raise TaigaAPIError("Unable to resolve version for story update") from None

    return await client.update_user_story(story_id, update_payload)


async def _delete_story_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    if "story_id" not in data:
        return _error_response("Field 'story_id' is required", 400)

    try:
        story_id = _parse_int(data["story_id"], "story_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    try:
        await _call_taiga(lambda client: client.delete_user_story(story_id))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while deleting story")
        return _error_response("Internal server error", 500)

    return JSONResponse({"deleted": {"story_id": story_id}})


async def _create_epic_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    for field in ("project_id", "subject"):
        if field not in data:
            return _error_response(f"Field '{field}' is required", 400)

    try:
        project_id = _parse_int(data["project_id"], "project_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    payload: dict[str, Any] = {
        "project": project_id,
        "subject": str(data["subject"]),
    }

    if "description" in data:
        payload["description"] = data["description"]

    if "status" in data:
        try:
            payload["status"] = _parse_int(data["status"], "status")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "assigned_to" in data:
        try:
            payload["assigned_to"] = _optional_int(data["assigned_to"], "assigned_to")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "tags" in data:
        tags = data["tags"]
        if tags is not None and not isinstance(tags, list):
            return _error_response("tags must be a list", 400)
        payload["tags"] = tags

    if "color" in data:
        payload["color"] = data["color"]

    try:
        epic = await _call_taiga(lambda client: client.create_epic(payload))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while creating epic")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "description",
        "assigned_to",
        "tags",
        "color",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"epic": _slice(epic, keep)})


async def _update_epic_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    if "epic_id" not in data:
        return _error_response("Field 'epic_id' is required", 400)

    try:
        epic_id = _parse_int(data["epic_id"], "epic_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    payload: dict[str, Any] = {}

    if "subject" in data:
        payload["subject"] = str(data["subject"])

    if "description" in data:
        payload["description"] = data["description"]

    if "status" in data:
        try:
            payload["status"] = _parse_int(data["status"], "status")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "assigned_to" in data:
        try:
            payload["assigned_to"] = _optional_int(data["assigned_to"], "assigned_to")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "tags" in data:
        tags = data["tags"]
        if tags is not None and not isinstance(tags, list):
            return _error_response("tags must be a list", 400)
        payload["tags"] = tags

    if "color" in data:
        payload["color"] = data["color"]

    if not payload:
        return _error_response("At least one field must be provided to update", 400)

    try:
        epic = await _call_taiga(
            lambda client: _update_epic_with_client(client, epic_id=epic_id, payload=payload)
        )
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while updating epic")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "description",
        "assigned_to",
        "tags",
        "color",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"epic": _slice(epic, keep)})


async def _update_epic_with_client(
    client,
    *,
    epic_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    update_payload = dict(payload)
    existing = await client.get_epic(epic_id)
    version = existing.get("version")
    if version is None:
        raise TaigaAPIError("Unable to resolve version for epic update")
    try:
        update_payload["version"] = int(version)
    except (TypeError, ValueError):
        raise TaigaAPIError("Unable to resolve version for epic update") from None
    return await client.update_epic(epic_id, update_payload)


async def _delete_epic_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    if "epic_id" not in data:
        return _error_response("Field 'epic_id' is required", 400)

    try:
        epic_id = _parse_int(data["epic_id"], "epic_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    try:
        await _call_taiga(lambda client: client.delete_epic(epic_id))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while deleting epic")
        return _error_response("Internal server error", 500)

    return JSONResponse({"deleted": {"epic_id": epic_id}})


async def _create_task_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    for field in ("project_id", "subject"):
        if field not in data:
            return _error_response(f"Field '{field}' is required", 400)

    try:
        project_id = _parse_int(data["project_id"], "project_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    payload: dict[str, Any] = {
        "project": project_id,
        "subject": str(data["subject"]),
    }

    if "description" in data:
        payload["description"] = data["description"]

    if "status" in data:
        try:
            payload["status"] = _parse_int(data["status"], "status")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "assigned_to" in data:
        try:
            payload["assigned_to"] = _optional_int(data["assigned_to"], "assigned_to")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "tags" in data:
        tags = data["tags"]
        if tags is not None and not isinstance(tags, list):
            return _error_response("tags must be a list", 400)
        payload["tags"] = tags

    if "user_story_id" in data:
        try:
            payload["user_story"] = _parse_int(data["user_story_id"], "user_story_id")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    try:
        task = await _call_taiga(lambda client: client.create_task(payload))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while creating task")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "description",
        "assigned_to",
        "tags",
        "user_story",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"task": _slice(task, keep)})


async def _update_task_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    if "task_id" not in data:
        return _error_response("Field 'task_id' is required", 400)

    try:
        task_id = _parse_int(data["task_id"], "task_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    payload: dict[str, Any] = {}

    if "subject" in data:
        payload["subject"] = str(data["subject"])

    if "description" in data:
        payload["description"] = data["description"]

    if "status" in data:
        try:
            payload["status"] = _parse_int(data["status"], "status")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "assigned_to" in data:
        try:
            payload["assigned_to"] = _optional_int(data["assigned_to"], "assigned_to")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "tags" in data:
        tags = data["tags"]
        if tags is not None and not isinstance(tags, list):
            return _error_response("tags must be a list", 400)
        payload["tags"] = tags

    if "user_story_id" in data:
        try:
            payload["user_story"] = _parse_int(data["user_story_id"], "user_story_id")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if not payload:
        return _error_response("At least one field must be provided to update", 400)

    try:
        task = await _call_taiga(
            lambda client: _update_task_with_client(client, task_id=task_id, payload=payload)
        )
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while updating task")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "description",
        "assigned_to",
        "tags",
        "user_story",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"task": _slice(task, keep)})


async def _update_task_with_client(
    client,
    *,
    task_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    update_payload = dict(payload)
    existing = await client.get_task(task_id)
    version = existing.get("version")
    if version is None:
        raise TaigaAPIError("Unable to resolve version for task update")
    try:
        update_payload["version"] = int(version)
    except (TypeError, ValueError):
        raise TaigaAPIError("Unable to resolve version for task update") from None
    return await client.update_task(task_id, update_payload)


async def _delete_task_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    if "task_id" not in data:
        return _error_response("Field 'task_id' is required", 400)

    try:
        task_id = _parse_int(data["task_id"], "task_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    try:
        await _call_taiga(lambda client: client.delete_task(task_id))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while deleting task")
        return _error_response("Internal server error", 500)

    return JSONResponse({"deleted": {"task_id": task_id}})


async def _create_issue_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    for field in ("project_id", "subject"):
        if field not in data:
            return _error_response(f"Field '{field}' is required", 400)

    try:
        project_id = _parse_int(data["project_id"], "project_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    payload: dict[str, Any] = {
        "project": project_id,
        "subject": str(data["subject"]),
    }

    if "description" in data:
        payload["description"] = data["description"]

    for field, key in (
        ("status", "status"),
        ("priority", "priority"),
        ("severity", "severity"),
        ("type", "type"),
    ):
        if field in data:
            try:
                payload[key if field != "type" else "issue_type"] = _parse_int(data[field], field)
            except ValueError as exc:
                return _error_response(str(exc), 400)

    if "assigned_to" in data:
        try:
            payload["assigned_to"] = _optional_int(data["assigned_to"], "assigned_to")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "tags" in data:
        tags = data["tags"]
        if tags is not None and not isinstance(tags, list):
            return _error_response("tags must be a list", 400)
        payload["tags"] = tags

    try:
        issue = await _call_taiga(lambda client: client.create_issue(payload))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while creating issue")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "priority",
        "severity",
        "issue_type",
        "description",
        "assigned_to",
        "tags",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"issue": _slice(issue, keep)})


async def _update_issue_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    if "issue_id" not in data:
        return _error_response("Field 'issue_id' is required", 400)

    try:
        issue_id = _parse_int(data["issue_id"], "issue_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    payload: dict[str, Any] = {}

    if "subject" in data:
        payload["subject"] = str(data["subject"])

    if "description" in data:
        payload["description"] = data["description"]

    for field, key in (
        ("status", "status"),
        ("priority", "priority"),
        ("severity", "severity"),
        ("type", "issue_type"),
    ):
        if field in data:
            try:
                payload[key] = _parse_int(data[field], field)
            except ValueError as exc:
                return _error_response(str(exc), 400)

    if "assigned_to" in data:
        try:
            payload["assigned_to"] = _optional_int(data["assigned_to"], "assigned_to")
        except ValueError as exc:
            return _error_response(str(exc), 400)

    if "tags" in data:
        tags = data["tags"]
        if tags is not None and not isinstance(tags, list):
            return _error_response("tags must be a list", 400)
        payload["tags"] = tags

    if not payload:
        return _error_response("At least one field must be provided to update", 400)

    try:
        issue = await _call_taiga(
            lambda client: _update_issue_with_client(client, issue_id=issue_id, payload=payload)
        )
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while updating issue")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "priority",
        "severity",
        "issue_type",
        "description",
        "assigned_to",
        "tags",
        "created_date",
        "modified_date",
    )
    return JSONResponse({"issue": _slice(issue, keep)})


async def _update_issue_with_client(
    client,
    *,
    issue_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    update_payload = dict(payload)
    existing = await client.get_issue(issue_id)
    version = existing.get("version")
    if version is None:
        raise TaigaAPIError("Unable to resolve version for issue update")
    try:
        update_payload["version"] = int(version)
    except (TypeError, ValueError):
        raise TaigaAPIError("Unable to resolve version for issue update") from None
    return await client.update_issue(issue_id, update_payload)


async def _delete_issue_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    data, parse_error = await _get_json_body(request)
    if parse_error:
        return parse_error
    assert data is not None

    if "issue_id" not in data:
        return _error_response("Field 'issue_id' is required", 400)

    try:
        issue_id = _parse_int(data["issue_id"], "issue_id")
    except ValueError as exc:
        return _error_response(str(exc), 400)

    try:
        await _call_taiga(lambda client: client.delete_issue(issue_id))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while deleting issue")
        return _error_response("Internal server error", 500)

    return JSONResponse({"deleted": {"issue_id": issue_id}})


@mcp.tool(
    name="taiga.projects.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_projects_list(search: str | None = None) -> list[dict[str, Any]]:
    """Return the Taiga projects the service account can access."""

    async with get_taiga_client() as client:
        member_id = await client.get_current_user_id()
        params: dict[str, Any] = {"member": str(member_id)}
        projects = await client.list_projects(params=params)
    keep = ("id", "name", "slug", "description", "is_private")
    filtered: list[dict[str, Any]] = []
    for project in projects:
        if search:
            name = project.get("name", "")
            if search.lower() not in name.lower():
                continue
        filtered.append(_slice(project, keep))
    return filtered


@mcp.tool(
    name="taiga.projects.get",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_projects_get(
    project_id: int | None = None,
    slug: str | None = None,
) -> dict[str, Any]:
    """Fetch project details by numeric identifier or slug."""

    if (project_id is None) == (slug is None):
        raise ValueError("Provide either project_id or slug, but not both")

    async with get_taiga_client() as client:
        if project_id is not None:
            project = await client.get_project(project_id)
        else:
            assert slug is not None
            project = await client.get_project_by_slug(slug)
    return project


@mcp.tool(
    name="taiga.epics.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_epics_list(project_id: int) -> list[dict[str, Any]]:
    """List epics for a Taiga project."""

    async with get_taiga_client() as client:
        epics = await client.list_epics(project_id)
    keep = (
        "id",
        "ref",
        "subject",
        "created_date",
        "modified_date",
        "status",
    )
    return [_slice(epic, keep) for epic in epics]


def _make_idempotency_cache_key(raw_key: str, user_story_id: int, subject: str) -> str:
    digest = hashlib.sha256(f"{user_story_id}:{subject}".encode("utf-8")).hexdigest()
    return f"{raw_key}:{digest}"


def _validate_due_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("due_date must be in YYYY-MM-DD format") from exc
    return parsed.isoformat()


async def _resolve_user_story_status_id(client, project_id: int, status: int | str | None) -> int | None:
    if status is None:
        return None
    if isinstance(status, int):
        return status

    statuses = await client.list_user_story_statuses(project_id)
    for entry in statuses:
        if entry.get("name") == status or entry.get("slug") == status:
            return entry.get("id")
    raise TaigaAPIError(f"Status '{status}' not found for project {project_id}")


async def _resolve_task_status_id(client, project_id: int, status: int | str | None) -> int | None:
    if status is None:
        return None
    if isinstance(status, int):
        return status

    statuses = await client.list_task_statuses(project_id)
    for entry in statuses:
        if entry.get("name") == status or entry.get("slug") == status:
            return entry.get("id")
    raise TaigaAPIError(f"Task status '{status}' not found for project {project_id}")


@mcp.tool(
    name="taiga.stories.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_stories_list(
    project_id: int,
    search: str | None = None,
    epic_id: int | None = None,
    tags: list[str] | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> list[dict[str, Any]]:
    """List user stories for a Taiga project with optional filters."""

    async with get_taiga_client() as client:
        stories = await client.list_user_stories(
            project_id,
            epic=epic_id,
            q=search,
            tags=tags,
            page=page,
            page_size=page_size,
        )
    keep = (
        "id",
        "ref",
        "subject",
        "description",
        "project",
        "epic",
        "epics",
        "tags",
        "status",
        "status_extra_info",
        "assigned_to",
        "created_date",
        "modified_date",
    )
    return [_slice(story, keep) for story in stories]


@mcp.tool(
    name="taiga.stories.create",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=False, destructiveHint=False),
)
async def taiga_stories_create(
    project_id: int,
    subject: str,
    description: str | None = None,
    status: int | str | None = None,
    tags: list[str] | None = None,
    assigned_to: int | None = None,
) -> dict[str, Any]:
    """Create a user story in Taiga and return the created record."""

    async with get_taiga_client() as client:
        status_id = await _resolve_user_story_status_id(client, project_id, status)
        payload: dict[str, Any] = {
            "project": project_id,
            "subject": subject,
        }
        if description:
            payload["description"] = description
        if status_id is not None:
            payload["status"] = status_id
        if tags:
            payload["tags"] = tags
        if assigned_to is not None:
            payload["assigned_to"] = assigned_to

        story = await client.create_user_story(payload)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "status",
        "description",
        "assigned_to",
        "tags",
        "created_date",
        "modified_date",
    )
    return _slice(story, keep)


@mcp.tool(
    name="taiga.stories.update",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=False, destructiveHint=False),
)
async def taiga_stories_update(params: StoryUpdateInput) -> dict[str, Any]:
    """Update a Taiga user story with partial field semantics."""

    async with get_taiga_client() as client:
        existing = await client.get_user_story(params.user_story_id)
        project_raw = existing.get("project")
        try:
            project_id = int(project_raw)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for story update") from None

        provided_fields = params.provided_fields() - {"user_story_id"}

        payload: dict[str, Any] = {}
        update_fields = provided_fields - {"version"}

        if "subject" in provided_fields:
            payload["subject"] = params.subject
        if "description" in provided_fields:
            payload["description"] = params.description
        if "tags" in provided_fields:
            payload["tags"] = [] if params.tags is None else params.tags
        if "assigned_to" in provided_fields:
            payload["assigned_to"] = params.assigned_to
        if "epic_id" in provided_fields:
            payload["epic"] = params.epic_id
        if "milestone_id" in provided_fields:
            payload["milestone"] = params.milestone_id
        if "custom_attributes" in provided_fields:
            payload["custom_attributes"] = params.custom_attributes

        if "status" in provided_fields:
            if params.status is None:
                payload["status"] = None
            else:
                status_id = await _resolve_user_story_status_id(client, project_id, params.status)
                payload["status"] = status_id

        if not update_fields:
            raise ValueError("At least one field must be provided to update the story")

        if "version" not in provided_fields or params.version is None:
            version_value = existing.get("version")
            if version_value is None:
                raise TaigaAPIError("Unable to resolve version for story update")
            try:
                payload["version"] = int(version_value)
            except (TypeError, ValueError):
                raise TaigaAPIError("Unable to resolve version for story update") from None
        else:
            payload["version"] = int(params.version)

        try:
            updated = await client.update_user_story(params.user_story_id, payload)
        except TaigaAPIError as exc:
            if exc.status_code == 409:
                latest = await client.get_user_story(params.user_story_id)
                latest_version = latest.get("version")
                raise ValueError(
                    f"Conflict updating user story {params.user_story_id}: latest version is {latest_version}"
                ) from exc
            raise

    return dict(updated)


@mcp.tool(
    name="taiga.epics.add_user_story",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=False, destructiveHint=False),
)
async def taiga_epics_add_user_story(epic_id: int, user_story_id: int) -> dict[str, Any] | None:
    """Attach a user story to an epic."""

    async with get_taiga_client() as client:
        response = await client.link_epic_user_story(epic_id, user_story_id)
    return response


@mcp.tool(
    name="taiga.tasks.create",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=False, destructiveHint=False),
)
async def taiga_tasks_create(params: TaskCreateInput) -> dict[str, Any]:
    """Create a task for a Taiga user story."""

    cache_key: str | None = None

    async with get_taiga_client() as client:
        story = await client.get_user_story(params.user_story_id)
        project_raw = story.get("project")
        try:
            project_id = int(project_raw)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for task creation") from None

        provided_fields = params.provided_fields() - {"user_story_id", "subject"}

        if "idempotency_key" in provided_fields and params.idempotency_key:
            cache_key = _make_idempotency_cache_key(
                params.idempotency_key, params.user_story_id, params.subject
            )
            cached = await _IDEMPOTENCY_STORE.get(cache_key)
            if cached is not None:
                return cached

        payload: dict[str, Any] = {
            "project": project_id,
            "user_story": params.user_story_id,
            "subject": params.subject,
        }

        if "description" in provided_fields:
            payload["description"] = params.description
        if "assigned_to" in provided_fields:
            payload["assigned_to"] = params.assigned_to
        if "tags" in provided_fields:
            payload["tags"] = [] if params.tags is None else params.tags
        if "due_date" in provided_fields:
            payload["due_date"] = _validate_due_date(params.due_date)
        if "status" in provided_fields:
            if params.status is None:
                payload["status"] = None
            else:
                status_id = await _resolve_task_status_id(client, project_id, params.status)
                payload["status"] = status_id

        task = await client.create_task(payload)

        if cache_key:
            await _IDEMPOTENCY_STORE.store(cache_key, dict(task))

    return dict(task)


@mcp.tool(
    name="taiga.tasks.update",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=False, destructiveHint=False),
)
async def taiga_tasks_update(params: TaskUpdateInput) -> dict[str, Any]:
    """Update fields on an existing Taiga task."""

    async with get_taiga_client() as client:
        existing = await client.get_task(params.task_id)
        project_raw = existing.get("project")
        try:
            project_id = int(project_raw)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for task update") from None

        provided_fields = params.provided_fields() - {"task_id"}

        payload: dict[str, Any] = {}
        update_fields = provided_fields - {"version"}

        if "subject" in provided_fields:
            payload["subject"] = params.subject
        if "description" in provided_fields:
            payload["description"] = params.description
        if "assigned_to" in provided_fields:
            payload["assigned_to"] = params.assigned_to
        if "tags" in provided_fields:
            payload["tags"] = [] if params.tags is None else params.tags
        if "due_date" in provided_fields:
            payload["due_date"] = _validate_due_date(params.due_date)
        if "status" in provided_fields:
            if params.status is None:
                payload["status"] = None
            else:
                status_id = await _resolve_task_status_id(client, project_id, params.status)
                payload["status"] = status_id

        if not update_fields:
            raise ValueError("At least one field must be provided to update the task")

        if "version" not in provided_fields or params.version is None:
            version_value = existing.get("version")
            if version_value is None:
                raise TaigaAPIError("Unable to resolve version for task update")
            try:
                payload["version"] = int(version_value)
            except (TypeError, ValueError):
                raise TaigaAPIError("Unable to resolve version for task update") from None
        else:
            payload["version"] = int(params.version)

        try:
            updated = await client.update_task(params.task_id, payload)
        except TaigaAPIError as exc:
            if exc.status_code == 409:
                latest = await client.get_task(params.task_id)
                latest_version = latest.get("version")
                raise ValueError(
                    f"Conflict updating task {params.task_id}: latest version is {latest_version}"
                ) from exc
            raise

    return dict(updated)


@mcp.tool(
    name="taiga.tasks.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_tasks_list(params: TaskListInput) -> dict[str, Any]:
    """List tasks with optional filters and pagination metadata."""

    provided_fields = params.provided_fields()

    project_filter = params.project_id if "project_id" in provided_fields else None
    user_story_filter = params.user_story_id if "user_story_id" in provided_fields else None
    assigned_filter = params.assigned_to if "assigned_to" in provided_fields else None
    search_filter = params.search if "search" in provided_fields else None
    page_filter = params.page if "page" in provided_fields else None
    page_size_filter = params.page_size if "page_size" in provided_fields else None

    async with get_taiga_client() as client:
        resolved_status: int | None = None
        if "status" in provided_fields:
            status_value = params.status
            if status_value is None:
                resolved_status = None
            elif isinstance(status_value, str):
                if project_filter is None:
                    raise ValueError("project_id is required when filtering by status name")
                try:
                    project_for_status = int(project_filter)
                except (TypeError, ValueError):
                    raise TaigaAPIError("Unable to resolve project for task status lookup") from None
                resolved_status = await _resolve_task_status_id(client, project_for_status, status_value)
            else:
                resolved_status = status_value

        tasks, pagination = await client.list_tasks(
            project_id=project_filter,
            user_story_id=user_story_filter,
            assigned_to=assigned_filter,
            search=search_filter,
            status=resolved_status,
            page=page_filter,
            page_size=page_size_filter,
        )

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "user_story",
        "status",
        "description",
        "assigned_to",
        "tags",
        "due_date",
        "created_date",
        "modified_date",
        "version",
    )
    filtered_tasks = [_slice(task, keep) for task in tasks]
    return {"tasks": filtered_tasks, "pagination": pagination}


@mcp.tool(
    name="taiga.users.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_users_list(search: str | None = None) -> list[dict[str, Any]]:
    """List Taiga users to support ID resolution."""

    async with get_taiga_client() as client:
        users = await client.list_users(search=search or None)

    keep = ("id", "full_name", "username", "email")
    results = [_slice(user, keep) for user in users]

    if search:
        lowered = search.lower()
        results = [
            user
            for user in results
            if any(
                isinstance(value, str) and lowered in value.lower()
                for value in (user.get("full_name"), user.get("username"), user.get("email"))
            )
        ]

    return results


@mcp.tool(
    name="taiga.milestones.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_milestones_list(
    project_id: int,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """List milestones for a project with optional search filtering."""

    async with get_taiga_client() as client:
        milestones = await client.list_milestones(project_id)

    keep = (
        "id",
        "name",
        "slug",
        "estimated_start",
        "estimated_finish",
        "closed",
        "project",
    )

    filtered: list[dict[str, Any]] = []
    for milestone in milestones:
        entry = _slice(milestone, keep)
        if search:
            lowered = search.lower()
            name = (entry.get("name") or "").lower()
            slug = (entry.get("slug") or "").lower()
            if lowered not in name and lowered not in slug:
                continue
        filtered.append(entry)

    return filtered


async def healthz(_):
    return PlainTextResponse("ok", status_code=200)


async def root(_):
    return PlainTextResponse("Taiga MCP up", status_code=200)

@asynccontextmanager
async def lifespan(_app):
    # The streamable HTTP transport requires its session manager task group to be running.
    async with mcp.session_manager.run():
        yield


# Mount the MCP streamable app under both /mcp and /mcp/ so proxies that normalize
# paths differently will still carry the session headers through without a redirect.
app = Starlette(
    routes=[
        Route("/", root),
        Route("/healthz", healthz),
        Route("/actions/list_projects", _list_projects_action, methods=["GET"]),
        Route("/actions/get_project", _get_project_action, methods=["GET"]),
        Route("/actions/get_project_by_slug", _get_project_by_slug_action, methods=["GET"]),
        Route("/actions/list_epics", _list_epics_action, methods=["GET"]),
    Route("/actions/list_stories", _list_user_stories_action, methods=["GET"]),
        Route("/actions/statuses", _list_statuses_action, methods=["GET"]),
        Route("/actions/create_story", _create_story_action, methods=["POST"]),
        Route("/actions/add_story_to_epic", _add_story_to_epic_action, methods=["POST"]),
        Route("/actions/update_story", _update_story_action, methods=["POST"]),
        Route("/actions/delete_story", _delete_story_action, methods=["POST"]),
        Route("/actions/create_epic", _create_epic_action, methods=["POST"]),
        Route("/actions/update_epic", _update_epic_action, methods=["POST"]),
        Route("/actions/delete_epic", _delete_epic_action, methods=["POST"]),
        Route("/actions/create_task", _create_task_action, methods=["POST"]),
        Route("/actions/update_task", _update_task_action, methods=["POST"]),
        Route("/actions/delete_task", _delete_task_action, methods=["POST"]),
        Route("/actions/create_issue", _create_issue_action, methods=["POST"]),
        Route("/actions/update_issue", _update_issue_action, methods=["POST"]),
        Route("/actions/delete_issue", _delete_issue_action, methods=["POST"]),
        Mount("/sse", app=sse_subapp),
        Mount("/mcp", app=streamable_http_subapp),
    ],
    lifespan=lifespan,
)
app.router.redirect_slashes = False


@app.middleware("http")
async def _rewrite_mcp_path(request, call_next):
    if request.scope.get("path") == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"
    return await call_next(request)


@app.middleware("http")
async def _rewrite_sse_path(request, call_next):
    if request.scope.get("path") == "/sse":
        request.scope["path"] = "/sse/"
        request.scope["raw_path"] = b"/sse/"
    return await call_next(request)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))