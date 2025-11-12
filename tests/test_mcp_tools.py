from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

import app
from taiga_client import TaigaAPIError


class DummyToolClient:
    def __init__(self) -> None:
        self.stories: dict[int, dict[str, Any]] = {
            5: {"id": 5, "project": 3, "version": 4},
        }
        self.story_statuses: dict[int, list[dict[str, Any]]] = {
            3: [
                {"id": 10, "name": "New", "slug": "new"},
                {"id": 11, "name": "In Progress", "slug": "in-progress"},
            ]
        }
        self.task_statuses: dict[int, list[dict[str, Any]]] = {
            3: [
                {"id": 20, "name": "New", "slug": "new"},
                {"id": 21, "name": "Doing", "slug": "doing"},
            ]
        }
        self.tasks: dict[int, dict[str, Any]] = {
            7: {
                "id": 7,
                "project": 3,
                "version": 2,
                "subject": "Existing",
                "status": 20,
            }
        }
        self.milestones: list[dict[str, Any]] = [
            {
                "id": 30,
                "name": "Milestone 4",
                "slug": "m4",
                "estimated_start": "2025-01-01",
                "estimated_finish": "2025-02-01",
                "closed": False,
                "project": 9,
            },
            {
                "id": 31,
                "name": "Milestone 5",
                "slug": "m5",
                "estimated_start": "2025-02-02",
                "estimated_finish": "2025-03-01",
                "closed": False,
                "project": 9,
            },
        ]
        self.users: list[dict[str, Any]] = [
            {
                "id": 90,
                "full_name": "John Black",
                "username": "jblack",
                "email": "john@example.com",
            },
            {
                "id": 91,
                "full_name": "Alex Green",
                "username": "agreen",
                "email": "alex@example.com",
            },
        ]
        self.created_tasks: list[dict[str, Any]] = []
        self.updated_stories: list[tuple[int, dict[str, Any]]] = []
        self.updated_tasks: list[tuple[int, dict[str, Any]]] = []
        self.list_tasks_calls: list[dict[str, Any]] = []
        self.list_tasks_result: tuple[list[dict[str, Any]], dict[str, Any]] = (
            [
                {
                    "id": 55,
                    "ref": 8,
                    "subject": "Investigate",
                    "project": 3,
                    "user_story": 5,
                    "status": 20,
                    "description": "",
                    "assigned_to": None,
                    "tags": ["prior-art-miner"],
                    "due_date": "2025-11-21",
                    "created_date": "2025-10-01T12:00:00Z",
                    "modified_date": "2025-10-01T12:00:00Z",
                    "version": 1,
                }
            ],
            {"page": 1, "page_size": 20, "total": 1},
        )
        self.raise_on_update_task: TaigaAPIError | None = None

    async def get_user_story(self, story_id: int) -> dict[str, Any]:
        return dict(self.stories[story_id])

    async def list_user_story_statuses(self, project_id: int) -> list[dict[str, Any]]:
        return list(self.story_statuses.get(project_id, []))

    async def update_user_story(self, story_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.updated_stories.append((story_id, dict(payload)))
        story = dict(self.stories.get(story_id, {"project": payload.get("project", 0)}))
        story.update(payload)
        return story

    async def list_task_statuses(self, project_id: int) -> list[dict[str, Any]]:
        return list(self.task_statuses.get(project_id, []))

    async def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        record.setdefault("id", 100 + len(self.created_tasks))
        record.setdefault("ref", 50 + len(self.created_tasks))
        record.setdefault("created_date", "2025-10-01T12:00:00Z")
        record.setdefault("modified_date", "2025-10-01T12:00:00Z")
        record.setdefault("version", 1)
        self.created_tasks.append(dict(payload))
        return record

    async def get_task(self, task_id: int) -> dict[str, Any]:
        return dict(self.tasks.get(task_id, {"id": task_id, "project": 3, "version": 1}))

    async def update_task(self, task_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        if self.raise_on_update_task:
            raise self.raise_on_update_task
        self.updated_tasks.append((task_id, dict(payload)))
        task = dict(self.tasks.get(task_id, {"project": 3}))
        task.update(payload)
        return task

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
        self.list_tasks_calls.append(
            {
                "project_id": project_id,
                "user_story_id": user_story_id,
                "assigned_to": assigned_to,
                "search": search,
                "status": status,
                "page": page,
                "page_size": page_size,
            }
        )
        return self.list_tasks_result

    async def list_users(self, *, search: str | None = None) -> list[dict[str, Any]]:
        if not search:
            return list(self.users)
        lowered = search.lower()
        return [
            user
            for user in self.users
            if lowered in user.get("full_name", "").lower()
            or lowered in user.get("username", "").lower()
            or lowered in user.get("email", "").lower()
        ]

    async def list_milestones(self, project_id: int) -> list[dict[str, Any]]:
        return [m for m in self.milestones if m.get("project") == project_id]


@pytest.fixture()
def tool_client(monkeypatch) -> DummyToolClient:
    dummy = DummyToolClient()

    @asynccontextmanager
    async def fake_client():
        yield dummy

    monkeypatch.setattr(app, "get_taiga_client", fake_client)
    return dummy


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio("asyncio")
async def test_mcp_lists_story_and_task_tools():
    tools = await app.mcp.list_tools()
    tool_names = {tool.name for tool in tools}
    expected = {
        "taiga.stories.update",
        "taiga.tasks.create",
        "taiga.tasks.update",
        "taiga.tasks.list",
        "taiga.users.list",
        "taiga.milestones.list",
    }
    assert expected.issubset(tool_names)


@pytest.mark.anyio("asyncio")
async def test_taiga_stories_update_resolves_status(tool_client: DummyToolClient):
    result = await app.taiga_stories_update(
        app.StoryUpdateInput(
            user_story_id=5,
            subject="Updated",
            status="In Progress",
            tags=["prior-art-miner"],
            milestone_id=4,
            custom_attributes={"effort": 5},
        )
    )

    assert result["subject"] == "Updated"
    story_id, payload = tool_client.updated_stories[-1]
    assert story_id == 5
    assert payload["status"] == 11
    assert payload["tags"] == ["prior-art-miner"]
    assert payload["milestone"] == 4
    assert payload["custom_attributes"] == {"effort": 5}


@pytest.mark.anyio("asyncio")
async def test_taiga_tasks_create_honours_idempotency(tool_client: DummyToolClient):
    result_first = await app.taiga_tasks_create(
        app.TaskCreateInput(
            user_story_id=5,
            subject="Stand up USPTO bulk mirror",
            status="Doing",
            due_date="2025-11-21",
            idempotency_key="abc123",
        )
    )
    result_second = await app.taiga_tasks_create(
        app.TaskCreateInput(
            user_story_id=5,
            subject="Stand up USPTO bulk mirror",
            status="Doing",
            due_date="2025-11-21",
            idempotency_key="abc123",
        )
    )

    assert result_first == result_second
    assert len(tool_client.created_tasks) == 1
    payload = tool_client.created_tasks[0]
    assert payload["due_date"] == "2025-11-21"
    assert payload["status"] == 21


@pytest.mark.anyio("asyncio")
async def test_taiga_tasks_update_conflict_raises_value_error(tool_client: DummyToolClient):
    tool_client.raise_on_update_task = TaigaAPIError("Conflict", status_code=409)

    with pytest.raises(ValueError) as excinfo:
        await app.taiga_tasks_update(app.TaskUpdateInput(task_id=7, subject="Revised"))

    assert "latest version" in str(excinfo.value)


@pytest.mark.anyio("asyncio")
async def test_taiga_tasks_list_filters_and_pagination(tool_client: DummyToolClient):
    response = await app.taiga_tasks_list(
        app.TaskListInput(project_id=3, status="Doing", page=1, page_size=50)
    )

    assert response["pagination"]["total"] == 1
    assert response["tasks"][0]["subject"] == "Investigate"
    call = tool_client.list_tasks_calls[-1]
    assert call["status"] == 21
    assert call["page_size"] == 50


@pytest.mark.anyio("asyncio")
async def test_taiga_tasks_list_requires_project_for_status(tool_client: DummyToolClient):
    with pytest.raises(ValueError):
        await app.taiga_tasks_list(app.TaskListInput(status="Doing"))


@pytest.mark.anyio("asyncio")
async def test_taiga_users_list_search_filter(tool_client: DummyToolClient):
    users = await app.taiga_users_list(search="john")
    assert len(users) == 1
    assert users[0]["username"] == "jblack"


@pytest.mark.anyio("asyncio")
async def test_taiga_milestones_list_search(tool_client: DummyToolClient):
    milestones = await app.taiga_milestones_list(project_id=9, search="m5")
    assert len(milestones) == 1
    assert milestones[0]["slug"] == "m5"


@pytest.mark.anyio("asyncio")
async def test_taiga_tasks_create_rejects_bad_due_date(tool_client: DummyToolClient):
    with pytest.raises(ValueError):
        await app.taiga_tasks_create(
            app.TaskCreateInput(
                user_story_id=5,
                subject="Invalid date",
                due_date="21-11-2025",
            )
        )
