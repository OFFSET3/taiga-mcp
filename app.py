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

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from taiga_client import TaigaAPIError, get_taiga_client
from pydantic import BaseModel, ConfigDict

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)


def _truthy_env(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_transport_security_settings() -> TransportSecuritySettings | None:
    # FastMCP enables DNS-rebinding protection automatically when host is localhost.
    # In container environments (Azure Container Apps ingress), the Host header will
    # be the public FQDN, so that default would reject all requests with HTTP 421.
    #
    # Default behavior here is to keep protection disabled unless explicitly enabled
    # via env vars.
    if not _truthy_env(os.getenv("MCP_ENABLE_DNS_REBINDING_PROTECTION")):
        return None

    raw_hosts = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    if not raw_hosts:
        logger.warning(
            "MCP_ENABLE_DNS_REBINDING_PROTECTION is true but MCP_ALLOWED_HOSTS is empty; "
            "disabling DNS rebinding protection to avoid rejecting all requests"
        )
        return None

    allowed_hosts = [host.strip() for host in raw_hosts.split(",") if host.strip()]
    raw_origins = os.getenv("MCP_ALLOWED_ORIGINS", "").strip()
    allowed_origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


class _NormalizeMountedRootPath:
    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") in ("", None):
            scope = dict(scope)
            scope["path"] = "/"
            scope["raw_path"] = b"/"
        await self._app(scope, receive, send)


class _RewriteMountedPaths:
    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path")
            if path == "/mcp":
                scope = dict(scope)
                scope["path"] = "/mcp/"
                scope["raw_path"] = b"/mcp/"
            elif path == "/sse":
                scope = dict(scope)
                scope["path"] = "/sse/"
                scope["raw_path"] = b"/sse/"
        await self._app(scope, receive, send)


mcp = FastMCP(
    "Taiga MCP",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    sse_path="/",
    streamable_http_path="/",
    transport_security=_get_transport_security_settings(),
)
# Prebuild sub-apps so we can wire their lifespans into the parent Starlette app.
# NOTE: Do not use Starlette's function middleware (`@app.middleware("http")`) here.
# That path uses BaseHTTPMiddleware, which breaks streaming responses (SSE).
sse_starlette_app = mcp.sse_app()
sse_subapp = _NormalizeMountedRootPath(sse_starlette_app)

streamable_http_starlette_app = mcp.streamable_http_app()
streamable_http_starlette_app.router.redirect_slashes = False
streamable_http_subapp = _NormalizeMountedRootPath(streamable_http_starlette_app)


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


def _redact_email(value: str | None) -> str | None:
    if not value:
        return None
    if "@" not in value:
        return "***"
    local, domain = value.split("@", 1)
    if len(local) <= 2:
        masked_local = "*" * len(local)
    else:
        masked_local = local[:2] + "***"
    return f"{masked_local}@{domain}"


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


class _UnsetType:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return "UNSET"


UNSET = _UnsetType()


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
    slug = request.query_params.get("slug")

    if not project_ids:
        env_project_id = os.getenv("TAIGA_PROJECT_ID")
        if env_project_id:
            try:
                project_ids = [str(int(env_project_id))]
            except ValueError:
                return _error_response("TAIGA_PROJECT_ID must be an integer", 500)
        else:
            slug_to_use = slug or os.getenv("TAIGA_PROJECT_SLUG")
            if not slug_to_use:
                return _error_response(
                    "project_id is required (or configure TAIGA_PROJECT_ID / TAIGA_PROJECT_SLUG)",
                    400,
                )

            try:
                project = await _call_taiga(lambda client: client.get_project_by_slug(slug_to_use))
            except TaigaAPIError as exc:
                return _error_response(str(exc), 400)
            except Exception:  # pragma: no cover - safety net
                logger.exception("Unexpected error while resolving project slug")
                return _error_response("Internal server error", 500)

            project_id = project.get("id")
            if not isinstance(project_id, int):
                return _error_response("Unable to resolve project_id from slug", 500)
            project_ids = [str(project_id)]

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


async def _diagnostics_action(request: Request) -> JSONResponse:
    """Return non-sensitive runtime diagnostics for debugging deployments."""

    if (error := _verify_api_key(request)) is not None:
        return error

    slug = request.query_params.get("slug")
    base_url = os.getenv("TAIGA_BASE_URL")
    username = _redact_email(os.getenv("TAIGA_USERNAME"))

    try:
        async with get_taiga_client() as client:
            user_id = await client.get_current_user_id()
            projects = await client.list_projects(params={"member": str(user_id)})
    except TaigaAPIError as exc:
        return JSONResponse(
            {
                "diagnostics": {
                    "taiga_base_url": base_url,
                    "taiga_username": username,
                    "error": str(exc),
                }
            }
        )
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while gathering diagnostics")
        return _error_response("Internal server error", 500)

    slugs = [p.get("slug") for p in projects if isinstance(p, dict) and p.get("slug")]
    slugs = slugs[:10]
    matched = None
    if slug:
        for project in projects:
            if isinstance(project, dict) and project.get("slug") == slug:
                matched = _slice(project, ("id", "name", "slug", "is_private"))
                break

    return JSONResponse(
        {
            "diagnostics": {
                "taiga_base_url": base_url,
                "taiga_username": username,
                "user_id": user_id,
                "projects_count": len(projects),
                "project_slugs_sample": slugs,
                "matched_project": matched,
            }
        }
    )


async def _get_epic_action(request: Request) -> JSONResponse:
    """Get a specific epic by ID via action proxy."""
    if (error := _verify_api_key(request)) is not None:
        return error

    epic_id_param = request.query_params.get("epic_id")
    if not epic_id_param:
        return _error_response("epic_id is required", 400)

    try:
        epic_id = int(epic_id_param)
    except ValueError:
        return _error_response("epic_id must be an integer", 400)

    try:
        epic = await _call_taiga(lambda client: client.get_epic(epic_id))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while retrieving epic")
        return _error_response("Internal server error", 500)

    return JSONResponse({"epic": epic})


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


async def _get_story_action(request: Request) -> JSONResponse:
    if (error := _verify_api_key(request)) is not None:
        return error

    story_id = request.query_params.get("story_id")
    if not story_id:
        return _error_response("Missing required parameter: story_id", 400)

    try:
        story_id_int = int(story_id)
    except ValueError:
        return _error_response("story_id must be an integer", 400)

    try:
        story = await _call_taiga(lambda client: client.get_user_story(story_id_int))
    except TaigaAPIError as exc:
        logger.error(f"Taiga API error getting story {story_id_int}: {exc}")
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while getting user story")
        return _error_response("Internal server error", 500)

    keep = (
        "id",
        "ref",
        "subject",
        "project",
        "epics",
        "tags",
        "status",
        "status_extra_info",
        "assigned_to",
        "assigned_to_extra_info",
        "description",
        "total_points",
        "created_date",
        "modified_date",
        "version",
    )
    return JSONResponse({"story": _slice(story, keep)})


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


async def _get_task_action(request: Request) -> JSONResponse:
    """Get a specific task by ID via action proxy."""
    if (error := _verify_api_key(request)) is not None:
        return error

    task_id_param = request.query_params.get("task_id")
    if not task_id_param:
        return _error_response("task_id is required", 400)

    try:
        task_id = int(task_id_param)
    except ValueError:
        return _error_response("task_id must be an integer", 400)

    try:
        task = await _call_taiga(lambda client: client.get_task(task_id))
    except TaigaAPIError as exc:
        return _error_response(str(exc), 400)
    except Exception:  # pragma: no cover - safety net
        logger.exception("Unexpected error while retrieving task")
        return _error_response("Internal server error", 500)

    return JSONResponse({"task": task})


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
    name="taiga.diagnostics",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_diagnostics(project_slug: str | None = None) -> dict[str, Any]:
    """Return non-sensitive runtime diagnostics.

    Use this to debug why a *deployed* taiga-mcp instance (e.g., used by ChatGPT)
    behaves differently than local scripts.
    """

    base_url = os.getenv("TAIGA_BASE_URL")
    username = _redact_email(os.getenv("TAIGA_USERNAME"))

    async with get_taiga_client() as client:
        user_id = await client.get_current_user_id()
        projects = await client.list_projects(params={"member": str(user_id)})

    slugs = [p.get("slug") for p in projects if isinstance(p, dict) and p.get("slug")]
    slugs = slugs[:10]
    matched = None
    if project_slug:
        for project in projects:
            if isinstance(project, dict) and project.get("slug") == project_slug:
                matched = _slice(project, ("id", "name", "slug", "is_private"))
                break

    return {
        "taiga_base_url": base_url,
        "taiga_username": username,
        "user_id": user_id,
        "projects_count": len(projects),
        "project_slugs_sample": slugs,
        "matched_project": matched,
    }


@mcp.tool(
    name="taiga.epics.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_epics_list(
    project_id: int,
    include_details: bool = False,
    page: int | None = None,
    page_size: int | None = None,
) -> list[dict[str, Any]]:
    """List epics for a Taiga project with optional pagination and field control.
    
    Args:
        project_id: Taiga project ID
        include_details: If True, include description and tags (default: False for minimal payload)
        page: Page number (1-indexed)
        page_size: Items per page (default/max: 50)
    """

    # Apply pagination defaults
    effective_page_size = min(page_size or 50, 50)
    
    async with get_taiga_client() as client:
        epics = await client.list_epics(project_id)
    
    # Minimal fields by default to avoid large payloads
    if include_details:
        keep = (
            "id",
            "ref",
            "subject",
            "description",
            "tags",
            "status",
            "created_date",
            "modified_date",
        )
    else:
        keep = (
            "id",
            "ref",
            "subject",
            "created_date",
            "modified_date",
            "status",
        )
    
    # Apply pagination
    if page is not None:
        start_idx = (page - 1) * effective_page_size
        end_idx = start_idx + effective_page_size
        epics = epics[start_idx:end_idx]
    elif page_size is not None:
        epics = epics[:effective_page_size]
    
    return [_slice(epic, keep) for epic in epics]


@mcp.tool(
    name="taiga.epics.get",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_epics_get(epic_id: int) -> dict[str, Any]:
    """Get a specific epic by ID with full details.
    
    Args:
        epic_id: Numeric epic identifier
    
    Returns:
        Full epic object including description, tags, status, etc.
    """

    async with get_taiga_client() as client:
        epic = await client.get_epic(epic_id)
    return dict(epic)


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
    name="taiga.stories.get",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_stories_get(user_story_id: int) -> dict[str, Any]:
    """Get a specific user story by ID with full details.
    
    Args:
        user_story_id: Numeric story identifier
    
    Returns:
        Full user story object including all fields
    """

    async with get_taiga_client() as client:
        story = await client.get_user_story(user_story_id)
    return dict(story)


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
    """List user stories for a Taiga project with optional filters (defaults to page_size=50)."""

    # Apply pagination defaults to avoid large payloads
    effective_page_size = min(page_size or 50, 100) if page_size else 50
    
    async with get_taiga_client() as client:
        stories = await client.list_user_stories(
            project_id,
            epic=epic_id,
            q=search,
            tags=tags,
            page=page,
            page_size=effective_page_size,
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
async def taiga_stories_update(
    user_story_id: int,
    subject: str | None | _UnsetType = UNSET,
    description: str | None | _UnsetType = UNSET,
    append_description: str | None | _UnsetType = UNSET,
    status: int | str | None | _UnsetType = UNSET,
    tags: list[str] | None | _UnsetType = UNSET,
    add_tags: list[str] | None | _UnsetType = UNSET,
    assigned_to: int | None | _UnsetType = UNSET,
    epic_id: int | None | _UnsetType = UNSET,
    milestone_id: int | None | _UnsetType = UNSET,
    custom_attributes: dict[str, Any] | None | _UnsetType = UNSET,
    version: int | None | _UnsetType = UNSET,
) -> dict[str, Any]:
    """Update a Taiga user story with partial field semantics.
    
    Supports append-only operations:
    - append_description: Appends text to existing description (vs overwrite with description)
    - add_tags: Merges new tags with existing (vs overwrite with tags)
    """

    async with get_taiga_client() as client:
        existing = await client.get_user_story(user_story_id)
        project_raw = existing.get("project")
        try:
            project_id = int(project_raw)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for story update") from None

        payload: dict[str, Any] = {}
        has_updates = False

        if subject is not UNSET:
            payload["subject"] = subject
            has_updates = True
        
        # Handle description - support both overwrite and append
        if description is not UNSET and append_description is not UNSET:
            raise ValueError("Cannot set both 'description' and 'append_description'")
        
        if description is not UNSET:
            payload["description"] = description
            has_updates = True
        elif append_description is not UNSET and append_description is not None:
            current_desc = existing.get("description", "")
            if current_desc:
                payload["description"] = f"{current_desc}\n\n{append_description}".strip()
            else:
                payload["description"] = append_description
            has_updates = True
        
        # Handle tags - support both overwrite and merge
        if tags is not UNSET and add_tags is not UNSET:
            raise ValueError("Cannot set both 'tags' and 'add_tags'")
        
        if tags is not UNSET:
            payload["tags"] = [] if tags is None else tags
            has_updates = True
        elif add_tags is not UNSET and add_tags is not None:
            existing_tags = set(existing.get("tags", []))
            new_tags = existing_tags | set(add_tags)
            payload["tags"] = sorted(new_tags)
            has_updates = True
        if assigned_to is not UNSET:
            payload["assigned_to"] = assigned_to
            has_updates = True
        if epic_id is not UNSET:
            payload["epic"] = epic_id
            has_updates = True
        if milestone_id is not UNSET:
            payload["milestone"] = milestone_id
            has_updates = True
        if custom_attributes is not UNSET:
            payload["custom_attributes"] = custom_attributes
            has_updates = True

        if status is not UNSET:
            if status is None:
                payload["status"] = None
            else:
                status_id = await _resolve_user_story_status_id(client, project_id, status)
                payload["status"] = status_id
            has_updates = True

        if not has_updates:
            raise ValueError("At least one field must be provided to update the story")

        if version is UNSET or version is None:
            version_value = existing.get("version")
            if version_value is None:
                raise TaigaAPIError("Unable to resolve version for story update")
            try:
                payload["version"] = int(version_value)
            except (TypeError, ValueError):
                raise TaigaAPIError("Unable to resolve version for story update") from None
        else:
            payload["version"] = int(version)

        try:
            updated = await client.update_user_story(user_story_id, payload)
        except TaigaAPIError as exc:
            if exc.status_code == 409:
                latest = await client.get_user_story(user_story_id)
                latest_version = latest.get("version")
                raise ValueError(
                    f"Conflict updating user story {user_story_id}: latest version is {latest_version}"
                ) from exc
            raise

    return dict(updated)


@mcp.tool(
    name="taiga.stories.delete",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=True, destructiveHint=True),
)
async def taiga_stories_delete(user_story_id: int) -> dict[str, Any]:
    """Delete a user story (DESTRUCTIVE - consider archive_or_close for production).
    
    Args:
        user_story_id: Numeric story identifier
    
    Returns:
        Confirmation payload with deleted story ID
    """

    async with get_taiga_client() as client:
        await client.delete_user_story(user_story_id)
    return {"id": user_story_id, "deleted": True}


@mcp.tool(
    name="taiga.epics.delete",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=True, destructiveHint=True),
)
async def taiga_epics_delete(epic_id: int) -> dict[str, Any]:
    """Delete an epic (DESTRUCTIVE - cannot be undone).
    
    Args:
        epic_id: Numeric epic identifier
    
    Returns:
        Confirmation payload with deleted epic ID
    """

    async with get_taiga_client() as client:
        await client.delete_epic(epic_id)
    return {"id": epic_id, "deleted": True}


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
async def taiga_tasks_create(
    user_story_id: int,
    subject: str,
    description: str | None | _UnsetType = UNSET,
    assigned_to: int | None | _UnsetType = UNSET,
    status: int | str | None | _UnsetType = UNSET,
    tags: list[str] | None | _UnsetType = UNSET,
    due_date: str | None | _UnsetType = UNSET,
    idempotency_key: str | None | _UnsetType = UNSET,
) -> dict[str, Any]:
    """Create a task for a Taiga user story."""

    cache_key: str | None = None

    async with get_taiga_client() as client:
        story = await client.get_user_story(user_story_id)
        project_raw = story.get("project")
        try:
            project_id = int(project_raw)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for task creation") from None

        if idempotency_key is not UNSET and idempotency_key:
            cache_key = _make_idempotency_cache_key(idempotency_key, user_story_id, subject)
            cached = await _IDEMPOTENCY_STORE.get(cache_key)
            if cached is not None:
                return cached

        payload: dict[str, Any] = {
            "project": project_id,
            "user_story": user_story_id,
            "subject": subject,
        }

        if description is not UNSET:
            payload["description"] = description
        if assigned_to is not UNSET:
            payload["assigned_to"] = assigned_to
        if tags is not UNSET:
            payload["tags"] = [] if tags is None else tags
        if due_date is not UNSET:
            payload["due_date"] = _validate_due_date(due_date)
        if status is not UNSET:
            if status is None:
                payload["status"] = None
            else:
                status_id = await _resolve_task_status_id(client, project_id, status)
                payload["status"] = status_id

        task = await client.create_task(payload)

        if cache_key:
            await _IDEMPOTENCY_STORE.store(cache_key, dict(task))

    return dict(task)


@mcp.tool(
    name="taiga.tasks.update",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=False, destructiveHint=False),
)
async def taiga_tasks_update(
    task_id: int,
    subject: str | None | _UnsetType = UNSET,
    description: str | None | _UnsetType = UNSET,
    append_description: str | None | _UnsetType = UNSET,
    assigned_to: int | None | _UnsetType = UNSET,
    status: int | str | None | _UnsetType = UNSET,
    tags: list[str] | None | _UnsetType = UNSET,
    add_tags: list[str] | None | _UnsetType = UNSET,
    due_date: str | None | _UnsetType = UNSET,
    version: int | None | _UnsetType = UNSET,
) -> dict[str, Any]:
    """Update fields on an existing Taiga task.
    
    Supports append-only operations:
    - append_description: Appends text to existing description (vs overwrite with description)
    - add_tags: Merges new tags with existing (vs overwrite with tags)
    """

    async with get_taiga_client() as client:
        existing = await client.get_task(task_id)
        project_raw = existing.get("project")
        try:
            project_id = int(project_raw)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for task update") from None

        payload: dict[str, Any] = {}
        has_updates = False

        if subject is not UNSET:
            payload["subject"] = subject
            has_updates = True
        
        # Handle description - support both overwrite and append
        if description is not UNSET and append_description is not UNSET:
            raise ValueError("Cannot set both 'description' and 'append_description'")
        
        if description is not UNSET:
            payload["description"] = description
            has_updates = True
        elif append_description is not UNSET and append_description is not None:
            current_desc = existing.get("description", "")
            if current_desc:
                payload["description"] = f"{current_desc}\n\n{append_description}".strip()
            else:
                payload["description"] = append_description
            has_updates = True
        
        if assigned_to is not UNSET:
            payload["assigned_to"] = assigned_to
            has_updates = True
        
        # Handle tags - support both overwrite and merge
        if tags is not UNSET and add_tags is not UNSET:
            raise ValueError("Cannot set both 'tags' and 'add_tags'")
        
        if tags is not UNSET:
            payload["tags"] = [] if tags is None else tags
            has_updates = True
        elif add_tags is not UNSET and add_tags is not None:
            existing_tags = set(existing.get("tags", []))
            new_tags = existing_tags | set(add_tags)
            payload["tags"] = sorted(new_tags)
            has_updates = True
        if due_date is not UNSET:
            payload["due_date"] = _validate_due_date(due_date)
            has_updates = True
        if status is not UNSET:
            if status is None:
                payload["status"] = None
            else:
                status_id = await _resolve_task_status_id(client, project_id, status)
                payload["status"] = status_id
            has_updates = True

        if not has_updates:
            raise ValueError("At least one field must be provided to update the task")

        if version is UNSET or version is None:
            version_value = existing.get("version")
            if version_value is None:
                raise TaigaAPIError("Unable to resolve version for task update")
            try:
                payload["version"] = int(version_value)
            except (TypeError, ValueError):
                raise TaigaAPIError("Unable to resolve version for task update") from None
        else:
            payload["version"] = int(version)

        try:
            updated = await client.update_task(task_id, payload)
        except TaigaAPIError as exc:
            if exc.status_code == 409:
                latest = await client.get_task(task_id)
                latest_version = latest.get("version")
                raise ValueError(
                    f"Conflict updating task {task_id}: latest version is {latest_version}"
                ) from exc
            raise

    return dict(updated)


@mcp.tool(
    name="taiga.tasks.delete",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=True, destructiveHint=True),
)
async def taiga_tasks_delete(task_id: int) -> dict[str, Any]:
    """Delete a task (DESTRUCTIVE - consider archive_or_close for production).
    
    Args:
        task_id: Numeric task identifier
    
    Returns:
        Confirmation payload with deleted task ID
    """

    async with get_taiga_client() as client:
        await client.delete_task(task_id)
    return {"id": task_id, "deleted": True}


@mcp.tool(
    name="taiga.tasks.archive_or_close",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=False, destructiveHint=False),
)
async def taiga_tasks_archive_or_close(
    task_id: int,
    closed_status: int | str | None = None,
    add_archive_tag: bool = True,
) -> dict[str, Any]:
    """Soft-delete a task by marking it as closed (safer than hard delete for production).
    
    Args:
        task_id: Numeric task identifier
        closed_status: Status ID or name to use for closure (finds closed status if not provided)
        add_archive_tag: If True, adds 'archived-by-mcp' tag (default: True)
    
    Returns:
        Updated task object with closed status and archive tag
    """

    async with get_taiga_client() as client:
        existing = await client.get_task(task_id)
        project_raw = existing.get("project")
        try:
            project_id = int(project_raw)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for task archive") from None
        
        # Find a closed status if not provided
        if closed_status is None:
            statuses = await client.list_task_statuses(project_id)
            closed_statuses = [s for s in statuses if s.get("is_closed", False)]
            if not closed_statuses:
                raise TaigaAPIError(f"No closed task status found for project {project_id}")
            status_id = closed_statuses[0]["id"]
        else:
            status_id = await _resolve_task_status_id(client, project_id, closed_status)
        
        # Build update payload
        payload: dict[str, Any] = {"status": status_id}
        
        # Add archive tag if requested
        if add_archive_tag:
            existing_tags = set(existing.get("tags", []))
            new_tags = existing_tags | {"archived-by-mcp"}
            payload["tags"] = sorted(new_tags)
        
        # Apply version for optimistic concurrency
        version = existing.get("version")
        if version is None:
            raise TaigaAPIError("Unable to resolve version for task archive")
        payload["version"] = int(version)
        
        updated = await client.update_task(task_id, payload)
    
    return dict(updated)


@mcp.tool(
    name="taiga.stories.archive_or_close",
    annotations=ToolAnnotations(openWorldHint=True, idempotentHint=False, destructiveHint=False),
)
async def taiga_stories_archive_or_close(
    user_story_id: int,
    closed_status: int | str | None = None,
    add_archive_tag: bool = True,
) -> dict[str, Any]:
    """Soft-delete a story by marking it as closed (safer than hard delete for production).
    
    Args:
        user_story_id: Numeric story identifier
        closed_status: Status ID or name to use for closure (finds closed status if not provided)
        add_archive_tag: If True, adds 'archived-by-mcp' tag (default: True)
    
    Returns:
        Updated story object with closed status and archive tag
    """

    async with get_taiga_client() as client:
        existing = await client.get_user_story(user_story_id)
        project_raw = existing.get("project")
        try:
            project_id = int(project_raw)
        except (TypeError, ValueError):
            raise TaigaAPIError("Unable to resolve project for story archive") from None
        
        # Find a closed status if not provided
        if closed_status is None:
            statuses = await client.list_user_story_statuses(project_id)
            closed_statuses = [s for s in statuses if s.get("is_closed", False)]
            if not closed_statuses:
                raise TaigaAPIError(f"No closed story status found for project {project_id}")
            status_id = closed_statuses[0]["id"]
        else:
            status_id = await _resolve_user_story_status_id(client, project_id, closed_status)
        
        # Build update payload
        payload: dict[str, Any] = {"status": status_id}
        
        # Add archive tag if requested
        if add_archive_tag:
            existing_tags = set(existing.get("tags", []))
            new_tags = existing_tags | {"archived-by-mcp"}
            payload["tags"] = sorted(new_tags)
        
        # Apply version for optimistic concurrency
        version = existing.get("version")
        if version is None:
            raise TaigaAPIError("Unable to resolve version for story archive")
        payload["version"] = int(version)
        
        updated = await client.update_user_story(user_story_id, payload)
    
    return dict(updated)


@mcp.tool(
    name="taiga.tasks.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_tasks_list(
    project_id: int | None | _UnsetType = UNSET,
    user_story_id: int | None | _UnsetType = UNSET,
    assigned_to: int | None | _UnsetType = UNSET,
    search: str | None | _UnsetType = UNSET,
    status: int | str | None | _UnsetType = UNSET,
    page: int | None | _UnsetType = UNSET,
    page_size: int | None | _UnsetType = UNSET,
) -> dict[str, Any]:
    """List tasks with optional filters and pagination metadata."""

    project_filter = None if project_id is UNSET else project_id
    user_story_filter = None if user_story_id is UNSET else user_story_id
    assigned_filter = None if assigned_to is UNSET else assigned_to
    search_filter = None if search is UNSET else search
    page_filter = None if page is UNSET else page
    page_size_filter = None if page_size is UNSET else page_size

    async with get_taiga_client() as client:
        resolved_status: int | None = None
        if status is not UNSET:
            if status is None:
                resolved_status = None
            elif isinstance(status, str):
                if project_filter is None:
                    raise ValueError("project_id is required when filtering by status name")
                try:
                    project_for_status = int(project_filter)
                except (TypeError, ValueError):
                    raise TaigaAPIError("Unable to resolve project for task status lookup") from None
                resolved_status = await _resolve_task_status_id(client, project_for_status, status)
            else:
                resolved_status = status

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
    name="taiga.tasks.get",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_tasks_get(task_id: int) -> dict[str, Any]:
    """Get a specific task by ID with full details.
    
    Args:
        task_id: Numeric task identifier
    
    Returns:
        Full task object including all fields
    """

    async with get_taiga_client() as client:
        task = await client.get_task(task_id)
    return dict(task)


@mcp.tool(
    name="taiga.users.list",
    annotations=ToolAnnotations(openWorldHint=True, readOnlyHint=True, idempotentHint=True),
)
async def taiga_users_list(
    project_id: int | None | _UnsetType = UNSET,
    search: str | None | _UnsetType = UNSET,
) -> list[dict[str, Any]]:
    """List Taiga users to support ID resolution."""

    project_filter = None if project_id is UNSET else project_id
    search_filter = None if search is UNSET else search
    async with get_taiga_client() as client:
        try:
            users = await client.list_users(search=search_filter or None, project_id=project_filter)
        except TaigaAPIError as exc:
            if project_filter is not None and exc.status_code in {401, 403}:
                users = await client.list_project_users(project_filter)
            else:
                raise

    normalised: list[dict[str, Any]] = []
    for entry in users:
        if isinstance(entry, dict) and isinstance(entry.get("user"), dict):
            payload = dict(entry["user"])
        else:
            payload = dict(entry)
        normalised.append(payload)

    keep = ("id", "full_name", "username", "email")
    results = [_slice(user, keep) for user in normalised]

    if search_filter:
        lowered = search_filter.lower()
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
    search: str | None | _UnsetType = UNSET,
) -> list[dict[str, Any]]:
    """List milestones for a project with optional search filtering."""

    search_filter = None if search is UNSET else search

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
        if search_filter:
            lowered = search_filter.lower()
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


async def openapi_schema(_):
    """Serve a minimal OpenAPI document for the /actions/* proxy.

    This is intended for ChatGPT Custom GPT Actions (API-key auth via X-Api-Key).
    """

    schema: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": "Taiga MCP Action Proxy",
            "version": "1.0.0",
            "description": "API-key protected REST facade over Taiga MCP (/actions/*).",
        },
        "servers": [{"url": "https://REPLACE_WITH_YOUR_TAIGA_MCP_HOST"}],
        "components": {
            "securitySchemes": {
                "ApiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-Api-Key",
                }
            }
        },
        "security": [{"ApiKeyAuth": []}],
        "paths": {
            "/actions/list_projects": {
                "get": {
                    "operationId": "listProjects",
                    "parameters": [
                        {"name": "search", "in": "query", "required": False, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/actions/get_project_by_slug": {
                "get": {
                    "operationId": "getProjectBySlug",
                    "parameters": [
                        {"name": "slug", "in": "query", "required": True, "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/actions/list_epics": {
                "get": {
                    "operationId": "listEpics",
                    "parameters": [
                        {
                            "name": "project_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                            "description": "Taiga project id (repeatable). If omitted, defaults to TAIGA_PROJECT_ID / TAIGA_PROJECT_SLUG.",
                        },
                        {
                            "name": "slug",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Project slug to resolve a project_id when project_id is omitted.",
                        },
                    ],
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/actions/list_stories": {
                "get": {
                    "operationId": "listStories",
                    "parameters": [
                        {"name": "project_id", "in": "query", "required": False, "schema": {"type": "integer"}},
                        {"name": "epic", "in": "query", "required": False, "schema": {"type": "integer"}},
                        {"name": "q", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "page", "in": "query", "required": False, "schema": {"type": "integer"}},
                        {"name": "page_size", "in": "query", "required": False, "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/actions/get_story": {
                "get": {
                    "operationId": "getStory",
                    "parameters": [
                        {"name": "story_id", "in": "query", "required": True, "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/actions/create_story": {
                "post": {
                    "operationId": "createStory",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}},
                    },
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/actions/update_story": {
                "post": {
                    "operationId": "updateStory",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}},
                    },
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
            "/actions/delete_story": {
                "post": {
                    "operationId": "deleteStory",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}},
                    },
                    "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "object"}}}}},
                }
            },
        },
    }

    return JSONResponse(schema)

@asynccontextmanager
async def lifespan(_app):
    # The streamable HTTP transport requires its session manager task group to be running.
    async with mcp.session_manager.run():
        yield


starlette_app = Starlette(
    routes=[
        Route("/", root),
        Route("/healthz", healthz),
        Route("/openapi.json", openapi_schema, methods=["GET"]),
        Route("/actions/diagnostics", _diagnostics_action, methods=["GET"]),
        Route("/actions/list_projects", _list_projects_action, methods=["GET"]),
        Route("/actions/get_project", _get_project_action, methods=["GET"]),
        Route("/actions/get_project_by_slug", _get_project_by_slug_action, methods=["GET"]),
        Route("/actions/list_epics", _list_epics_action, methods=["GET"]),
        Route("/actions/get_epic", _get_epic_action, methods=["GET"]),
        Route("/actions/list_stories", _list_user_stories_action, methods=["GET"]),
        Route("/actions/get_story", _get_story_action, methods=["GET"]),
        Route("/actions/get_task", _get_task_action, methods=["GET"]),
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
starlette_app.router.redirect_slashes = False

# Starlette's function middleware uses BaseHTTPMiddleware which is unsafe for
# streaming responses (e.g., SSE). Use a pure-ASGI wrapper for path rewrites.
app = _RewriteMountedPaths(starlette_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))