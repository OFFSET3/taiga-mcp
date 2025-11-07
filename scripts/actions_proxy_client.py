"""Utility CLI for interacting with the Taiga action proxy endpoints."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import httpx

DEFAULT_BASE_URL_ENV = "TAIGA_PROXY_BASE_URL"
DEFAULT_API_KEY_ENV = "ACTION_PROXY_API_KEY"


class ActionProxyError(RuntimeError):
    """Raised when the proxy returns a non-success response."""


def _default_base_url() -> str | None:
    return os.getenv(DEFAULT_BASE_URL_ENV)


def _default_api_key() -> str | None:
    return os.getenv(DEFAULT_API_KEY_ENV)


def _build_client(base_url: str, api_key: str) -> httpx.Client:
    base_url = base_url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    return httpx.Client(base_url=base_url, headers=headers, timeout=30.0)


def _handle_response(response: httpx.Response) -> Any:
    if response.is_success:
        if response.content:
            return response.json()
        return None
    try:
        payload = response.json()
    except Exception:  # pragma: no cover - fall back to text body
        payload = {"error": response.text or response.reason_phrase}
    message = payload.get("error") if isinstance(payload, dict) else str(payload)
    raise ActionProxyError(f"Request failed with HTTP {response.status_code}: {message}")


def _cmd_list_projects(client: httpx.Client, args: argparse.Namespace) -> Any:
    params = {}
    if args.search:
        params["search"] = args.search
    response = client.get("/actions/list_projects", params=params)
    return _handle_response(response)


def _cmd_get_project(client: httpx.Client, args: argparse.Namespace) -> Any:
    params = {"project_id": args.project_id}
    response = client.get("/actions/get_project", params=params)
    return _handle_response(response)


def _cmd_get_project_by_slug(client: httpx.Client, args: argparse.Namespace) -> Any:
    params = {"slug": args.slug}
    response = client.get("/actions/get_project_by_slug", params=params)
    return _handle_response(response)


def _cmd_list_epics(client: httpx.Client, args: argparse.Namespace) -> Any:
    params: list[tuple[str, Any]] = [("project_id", project_id) for project_id in args.project_id]
    response = client.get("/actions/list_epics", params=params)
    return _handle_response(response)


def _cmd_list_stories(client: httpx.Client, args: argparse.Namespace) -> Any:
    params: list[tuple[str, Any]] = [("project_id", args.project_id)]
    if args.epic_id is not None:
        params.append(("epic_id", args.epic_id))
    if args.search:
        params.append(("search", args.search))
    if args.page is not None:
        params.append(("page", args.page))
    if args.page_size is not None:
        params.append(("page_size", args.page_size))
    if args.tags:
        for tag in args.tags:
            params.append(("tag", tag))
    response = client.get("/actions/list_stories", params=params)
    return _handle_response(response)


def _cmd_list_statuses(client: httpx.Client, args: argparse.Namespace) -> Any:
    params = {"project_id": args.project_id}
    response = client.get("/actions/statuses", params=params)
    return _handle_response(response)


def _cmd_create_story(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {
        "project_id": args.project_id,
        "subject": args.subject,
    }
    if args.description:
        payload["description"] = args.description
    if args.status:
        payload["status"] = args.status
    if args.tags:
        payload["tags"] = args.tags
    if args.assigned_to is not None:
        payload["assigned_to"] = args.assigned_to
    response = client.post("/actions/create_story", json=payload)
    return _handle_response(response)


def _cmd_update_story(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {"story_id": args.story_id}
    if args.project_id is not None:
        payload["project_id"] = args.project_id
    if args.subject is not None:
        payload["subject"] = args.subject
    if args.description is not None:
        payload["description"] = args.description
    if args.status is not None:
        payload["status"] = args.status
    if args.tags:
        payload["tags"] = args.tags
    if args.assigned_to is not None:
        payload["assigned_to"] = args.assigned_to
    response = client.post("/actions/update_story", json=payload)
    return _handle_response(response)


def _cmd_delete_story(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload = {"story_id": args.story_id}
    response = client.post("/actions/delete_story", json=payload)
    return _handle_response(response)


def _cmd_add_story_to_epic(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload = {"epic_id": args.epic_id, "user_story_id": args.user_story_id}
    response = client.post("/actions/add_story_to_epic", json=payload)
    return _handle_response(response)


def _cmd_create_epic(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {
        "project_id": args.project_id,
        "subject": args.subject,
    }
    if args.description is not None:
        payload["description"] = args.description
    if args.status is not None:
        payload["status"] = args.status
    if args.assigned_to is not None:
        payload["assigned_to"] = args.assigned_to
    if args.tags:
        payload["tags"] = args.tags
    if args.color is not None:
        payload["color"] = args.color
    response = client.post("/actions/create_epic", json=payload)
    return _handle_response(response)


def _cmd_update_epic(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {"epic_id": args.epic_id}
    if args.subject is not None:
        payload["subject"] = args.subject
    if args.description is not None:
        payload["description"] = args.description
    if args.status is not None:
        payload["status"] = args.status
    if args.assigned_to is not None:
        payload["assigned_to"] = args.assigned_to
    if args.tags:
        payload["tags"] = args.tags
    if args.color is not None:
        payload["color"] = args.color
    response = client.post("/actions/update_epic", json=payload)
    return _handle_response(response)


def _cmd_delete_epic(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload = {"epic_id": args.epic_id}
    response = client.post("/actions/delete_epic", json=payload)
    return _handle_response(response)


def _cmd_create_task(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {
        "project_id": args.project_id,
        "subject": args.subject,
    }
    if args.description is not None:
        payload["description"] = args.description
    if args.status is not None:
        payload["status"] = args.status
    if args.assigned_to is not None:
        payload["assigned_to"] = args.assigned_to
    if args.tags:
        payload["tags"] = args.tags
    if args.user_story_id is not None:
        payload["user_story_id"] = args.user_story_id
    response = client.post("/actions/create_task", json=payload)
    return _handle_response(response)


def _cmd_update_task(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {"task_id": args.task_id}
    if args.subject is not None:
        payload["subject"] = args.subject
    if args.description is not None:
        payload["description"] = args.description
    if args.status is not None:
        payload["status"] = args.status
    if args.assigned_to is not None:
        payload["assigned_to"] = args.assigned_to
    if args.tags:
        payload["tags"] = args.tags
    if args.user_story_id is not None:
        payload["user_story_id"] = args.user_story_id
    response = client.post("/actions/update_task", json=payload)
    return _handle_response(response)


def _cmd_delete_task(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload = {"task_id": args.task_id}
    response = client.post("/actions/delete_task", json=payload)
    return _handle_response(response)


def _cmd_create_issue(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {
        "project_id": args.project_id,
        "subject": args.subject,
    }
    if args.description is not None:
        payload["description"] = args.description
    if args.status is not None:
        payload["status"] = args.status
    if args.priority is not None:
        payload["priority"] = args.priority
    if args.severity is not None:
        payload["severity"] = args.severity
    if args.type is not None:
        payload["type"] = args.type
    if args.assigned_to is not None:
        payload["assigned_to"] = args.assigned_to
    if args.tags:
        payload["tags"] = args.tags
    response = client.post("/actions/create_issue", json=payload)
    return _handle_response(response)


def _cmd_update_issue(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload: dict[str, Any] = {"issue_id": args.issue_id}
    if args.subject is not None:
        payload["subject"] = args.subject
    if args.description is not None:
        payload["description"] = args.description
    if args.status is not None:
        payload["status"] = args.status
    if args.priority is not None:
        payload["priority"] = args.priority
    if args.severity is not None:
        payload["severity"] = args.severity
    if args.type is not None:
        payload["type"] = args.type
    if args.assigned_to is not None:
        payload["assigned_to"] = args.assigned_to
    if args.tags:
        payload["tags"] = args.tags
    response = client.post("/actions/update_issue", json=payload)
    return _handle_response(response)


def _cmd_delete_issue(client: httpx.Client, args: argparse.Namespace) -> Any:
    payload = {"issue_id": args.issue_id}
    response = client.post("/actions/delete_issue", json=payload)
    return _handle_response(response)

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Interact with the Taiga MCP action proxy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=_default_base_url(),
        help=f"Proxy base URL (falls back to ${DEFAULT_BASE_URL_ENV})",
    )
    parser.add_argument(
        "--api-key",
        default=_default_api_key(),
        help=f"Proxy API key (falls back to ${DEFAULT_API_KEY_ENV})",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty print JSON responses",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_projects = subparsers.add_parser("list-projects", help="List Taiga projects")
    list_projects.add_argument("--search", help="Filter projects by name (case insensitive)")
    list_projects.set_defaults(func=_cmd_list_projects)

    get_project = subparsers.add_parser("get-project", help="Fetch detailed project metadata by id")
    get_project.add_argument("--project-id", type=int, required=True, help="Numeric project identifier")
    get_project.set_defaults(func=_cmd_get_project)

    get_project_by_slug = subparsers.add_parser(
        "get-project-by-slug",
        help="Fetch detailed project metadata by slug",
    )
    get_project_by_slug.add_argument("--slug", required=True, help="Project slug")
    get_project_by_slug.set_defaults(func=_cmd_get_project_by_slug)

    list_epics = subparsers.add_parser("list-epics", help="List epics for one or more projects")
    list_epics.add_argument(
        "--project-id",
        type=int,
        action="append",
        required=True,
        help="Project identifier (repeatable)",
    )
    list_epics.set_defaults(func=_cmd_list_epics)

    list_stories = subparsers.add_parser("list-stories", help="List user stories for a project")
    list_stories.add_argument("--project-id", type=int, required=True)
    list_stories.add_argument("--epic-id", type=int, help="Filter by epic identifier")
    list_stories.add_argument("--search", help="Search query for subject/description")
    list_stories.add_argument("--tag", dest="tags", action="append", help="Repeat to filter by tag")
    list_stories.add_argument("--page", type=int, help="Page number for pagination")
    list_stories.add_argument("--page-size", type=int, help="Page size for pagination")
    list_stories.set_defaults(func=_cmd_list_stories)

    list_statuses = subparsers.add_parser("list-statuses", help="List user story statuses for a project")
    list_statuses.add_argument("--project-id", type=int, required=True, help="Project identifier")
    list_statuses.set_defaults(func=_cmd_list_statuses)

    create_story = subparsers.add_parser("create-story", help="Create a Taiga user story")
    create_story.add_argument("--project-id", type=int, required=True)
    create_story.add_argument("--subject", required=True)
    create_story.add_argument("--description")
    create_story.add_argument("--status")
    create_story.add_argument("--tag", dest="tags", action="append", help="Repeat for multiple tags")
    create_story.add_argument("--assigned-to", type=int)
    create_story.set_defaults(func=_cmd_create_story)

    update_story = subparsers.add_parser("update-story", help="Update a Taiga user story")
    update_story.add_argument("--story-id", type=int, required=True)
    update_story.add_argument("--project-id", type=int)
    update_story.add_argument("--subject")
    update_story.add_argument("--description")
    update_story.add_argument("--status")
    update_story.add_argument("--tag", dest="tags", action="append", help="Repeat for multiple tags")
    update_story.add_argument("--assigned-to", type=int)
    update_story.set_defaults(func=_cmd_update_story)

    delete_story = subparsers.add_parser("delete-story", help="Delete a Taiga user story")
    delete_story.add_argument("--story-id", type=int, required=True)
    delete_story.set_defaults(func=_cmd_delete_story)

    add_story_to_epic = subparsers.add_parser("add-story-to-epic", help="Link a story to an epic")
    add_story_to_epic.add_argument("--epic-id", type=int, required=True)
    add_story_to_epic.add_argument("--user-story-id", type=int, required=True)
    add_story_to_epic.set_defaults(func=_cmd_add_story_to_epic)

    create_epic = subparsers.add_parser("create-epic", help="Create a Taiga epic")
    create_epic.add_argument("--project-id", type=int, required=True)
    create_epic.add_argument("--subject", required=True)
    create_epic.add_argument("--description")
    create_epic.add_argument("--status", type=int)
    create_epic.add_argument("--assigned-to", type=int)
    create_epic.add_argument("--tag", dest="tags", action="append", help="Repeat for multiple tags")
    create_epic.add_argument("--color")
    create_epic.set_defaults(func=_cmd_create_epic)

    update_epic = subparsers.add_parser("update-epic", help="Update a Taiga epic")
    update_epic.add_argument("--epic-id", type=int, required=True)
    update_epic.add_argument("--subject")
    update_epic.add_argument("--description")
    update_epic.add_argument("--status", type=int)
    update_epic.add_argument("--assigned-to", type=int)
    update_epic.add_argument("--tag", dest="tags", action="append", help="Repeat for multiple tags")
    update_epic.add_argument("--color")
    update_epic.set_defaults(func=_cmd_update_epic)

    delete_epic = subparsers.add_parser("delete-epic", help="Delete a Taiga epic")
    delete_epic.add_argument("--epic-id", type=int, required=True)
    delete_epic.set_defaults(func=_cmd_delete_epic)

    create_task = subparsers.add_parser("create-task", help="Create a Taiga task")
    create_task.add_argument("--project-id", type=int, required=True)
    create_task.add_argument("--subject", required=True)
    create_task.add_argument("--description")
    create_task.add_argument("--status", type=int)
    create_task.add_argument("--assigned-to", type=int)
    create_task.add_argument("--tag", dest="tags", action="append", help="Repeat for multiple tags")
    create_task.add_argument("--user-story-id", type=int)
    create_task.set_defaults(func=_cmd_create_task)

    update_task = subparsers.add_parser("update-task", help="Update a Taiga task")
    update_task.add_argument("--task-id", type=int, required=True)
    update_task.add_argument("--subject")
    update_task.add_argument("--description")
    update_task.add_argument("--status", type=int)
    update_task.add_argument("--assigned-to", type=int)
    update_task.add_argument("--tag", dest="tags", action="append", help="Repeat for multiple tags")
    update_task.add_argument("--user-story-id", type=int)
    update_task.set_defaults(func=_cmd_update_task)

    delete_task = subparsers.add_parser("delete-task", help="Delete a Taiga task")
    delete_task.add_argument("--task-id", type=int, required=True)
    delete_task.set_defaults(func=_cmd_delete_task)

    create_issue = subparsers.add_parser("create-issue", help="Create a Taiga issue")
    create_issue.add_argument("--project-id", type=int, required=True)
    create_issue.add_argument("--subject", required=True)
    create_issue.add_argument("--description")
    create_issue.add_argument("--status", type=int)
    create_issue.add_argument("--priority", type=int)
    create_issue.add_argument("--severity", type=int)
    create_issue.add_argument("--type", type=int, help="Issue type identifier")
    create_issue.add_argument("--assigned-to", type=int)
    create_issue.add_argument("--tag", dest="tags", action="append", help="Repeat for multiple tags")
    create_issue.set_defaults(func=_cmd_create_issue)

    update_issue = subparsers.add_parser("update-issue", help="Update a Taiga issue")
    update_issue.add_argument("--issue-id", type=int, required=True)
    update_issue.add_argument("--subject")
    update_issue.add_argument("--description")
    update_issue.add_argument("--status", type=int)
    update_issue.add_argument("--priority", type=int)
    update_issue.add_argument("--severity", type=int)
    update_issue.add_argument("--type", type=int, help="Issue type identifier")
    update_issue.add_argument("--assigned-to", type=int)
    update_issue.add_argument("--tag", dest="tags", action="append", help="Repeat for multiple tags")
    update_issue.set_defaults(func=_cmd_update_issue)

    delete_issue = subparsers.add_parser("delete-issue", help="Delete a Taiga issue")
    delete_issue.add_argument("--issue-id", type=int, required=True)
    delete_issue.set_defaults(func=_cmd_delete_issue)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.base_url:
        parser.error("--base-url is required (set TAIGA_PROXY_BASE_URL or pass explicitly)")
    if not args.api_key:
        parser.error("--api-key is required (set ACTION_PROXY_API_KEY or pass explicitly)")

    client = _build_client(args.base_url, args.api_key)
    try:
        result = args.func(client, args)
    finally:
        client.close()

    if result is None:
        return 0

    if args.pretty:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        json.dump(result, sys.stdout)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
