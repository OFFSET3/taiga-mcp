import pytest
from contextlib import asynccontextmanager
from typing import Any
from starlette.testclient import TestClient

import app
from taiga_client import TaigaAPIError


class DummyTaigaClient:
    def __init__(self) -> None:
        self.projects: list[dict] = []
        self.epics_by_project: dict[int, list[dict]] = {}
        self.user_stories_by_project: dict[int, list[dict]] = {}
        self.statuses_by_project: dict[int, list[dict]] = {}
        self.raise_map: dict[str, Exception] = {}
        self.created_payloads: list[dict] = []
        self.link_calls: list[tuple[int, int]] = []

        self.list_projects_params = None
        self.project_details: dict[int, dict] = {}
        self.project_details_by_slug: dict[str, dict] = {}
        self.list_user_stories_calls: list[tuple[int, dict[str, Any]]] = []
        self.story_details: dict[int, dict] = {}
        self.epic_details: dict[int, dict] = {}
        self.task_details: dict[int, dict] = {}
        self.issue_details: dict[int, dict] = {}
        self.updated_stories: list[tuple[int, dict]] = []
        self.deleted_story_ids: list[int] = []
        self.epics_created: list[dict] = []
        self.epic_updates: list[tuple[int, dict]] = []
        self.deleted_epic_ids: list[int] = []
        self.tasks_created: list[dict] = []
        self.task_updates: list[tuple[int, dict]] = []
        self.deleted_task_ids: list[int] = []
        self.issues_created: list[dict] = []
        self.issue_updates: list[tuple[int, dict]] = []
        self.deleted_issue_ids: list[int] = []
        self.user_id = 123

    async def list_projects(self, params=None) -> list[dict]:
        error = self.raise_map.get("list_projects")
        if error:
            raise error
        self.list_projects_params = params
        return self.projects

    async def get_current_user_id(self) -> int:
        return self.user_id

    async def get_project(self, project_id: int) -> dict:
        error = self.raise_map.get("get_project")
        if error:
            raise error
        return self.project_details.get(project_id, {"id": project_id})

    async def get_project_by_slug(self, slug: str) -> dict:
        error = self.raise_map.get("get_project_by_slug")
        if error:
            raise error
        return self.project_details_by_slug.get(slug, {"slug": slug})

    async def list_epics(self, project_id: int) -> list[dict]:
        error = self.raise_map.get("list_epics")
        if error:
            raise error
        return list(self.epics_by_project.get(project_id, []))

    async def list_user_stories(
        self,
        project_id: int,
        *,
        epic: int | None = None,
        q: str | None = None,
        tags: list[str] | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> list[dict]:
        error = self.raise_map.get("list_user_stories")
        if error:
            raise error
        params = {
            "epic": epic,
            "q": q,
            "tags": tags,
            "page": page,
            "page_size": page_size,
        }
        self.list_user_stories_calls.append((project_id, params))
        return list(self.user_stories_by_project.get(project_id, []))

    async def list_user_story_statuses(self, project_id: int) -> list[dict]:
        error = self.raise_map.get("list_user_story_statuses")
        if error:
            raise error
        return list(self.statuses_by_project.get(project_id, []))

    async def create_user_story(self, payload: dict) -> dict:
        error = self.raise_map.get("create_user_story")
        if error:
            raise error
        self.created_payloads.append(payload)
        return {
            "id": 42,
            "ref": 108,
            "project": payload["project"],
            "subject": payload["subject"],
            "status": payload.get("status"),
            "description": payload.get("description"),
            "assigned_to": payload.get("assigned_to"),
            "tags": payload.get("tags"),
            "created_date": "2025-10-01T12:00:00Z",
            "modified_date": "2025-10-01T12:00:00Z",
            "ignored": "value",
        }

    async def get_user_story(self, story_id: int) -> dict:
        record = dict(self.story_details.get(story_id, {"id": story_id, "project": 1, "version": 1}))
        record.setdefault("project", 1)
        record.setdefault("version", 1)
        return record

    async def get_epic(self, epic_id: int) -> dict:
        record = dict(self.epic_details.get(epic_id, {"id": epic_id, "version": 1}))
        record.setdefault("version", 1)
        return record

    async def update_user_story(self, story_id: int, payload: dict) -> dict:
        error = self.raise_map.get("update_user_story")
        if error:
            raise error
        self.updated_stories.append((story_id, payload))
        base = self.story_details.get(story_id, {"project": payload.get("project", 1)})
        result = {
            "id": story_id,
            "ref": 200 + story_id,
            "project": payload.get("project", base.get("project")),
            "subject": payload.get("subject", "Story"),
            "status": payload.get("status"),
            "description": payload.get("description"),
            "assigned_to": payload.get("assigned_to"),
            "tags": payload.get("tags"),
            "created_date": "2025-10-01T12:00:00Z",
            "modified_date": "2025-10-02T12:00:00Z",
        }
        return result

    async def delete_user_story(self, story_id: int) -> None:
        error = self.raise_map.get("delete_user_story")
        if error:
            raise error
        self.deleted_story_ids.append(story_id)

    async def link_epic_user_story(self, epic_id: int, user_story_id: int) -> dict | None:
        error = self.raise_map.get("link_epic_user_story")
        if error:
            raise error
        self.link_calls.append((epic_id, user_story_id))
        return {"epic": epic_id, "user_story": user_story_id}

    async def create_epic(self, payload: dict) -> dict:
        error = self.raise_map.get("create_epic")
        if error:
            raise error
        self.epics_created.append(payload)
        return {
            "id": 11,
            "ref": 5,
            "project": payload.get("project"),
            "subject": payload.get("subject"),
            "status": payload.get("status"),
            "description": payload.get("description"),
            "assigned_to": payload.get("assigned_to"),
            "tags": payload.get("tags"),
            "color": payload.get("color"),
            "created_date": "2025-10-01T12:00:00Z",
            "modified_date": "2025-10-01T12:00:00Z",
        }

    async def update_epic(self, epic_id: int, payload: dict) -> dict:
        error = self.raise_map.get("update_epic")
        if error:
            raise error
        self.epic_updates.append((epic_id, payload))
        result = {
            "id": epic_id,
            "ref": 5,
            "project": 2,
            "subject": payload.get("subject", "Epic"),
            "status": payload.get("status"),
            "description": payload.get("description"),
            "assigned_to": payload.get("assigned_to"),
            "tags": payload.get("tags"),
            "color": payload.get("color"),
            "created_date": "2025-10-01T12:00:00Z",
            "modified_date": "2025-10-02T12:00:00Z",
        }
        return result

    async def delete_epic(self, epic_id: int) -> None:
        error = self.raise_map.get("delete_epic")
        if error:
            raise error
        self.deleted_epic_ids.append(epic_id)

    async def create_task(self, payload: dict) -> dict:
        error = self.raise_map.get("create_task")
        if error:
            raise error
        self.tasks_created.append(payload)
        return {
            "id": 31,
            "ref": 9,
            "project": payload.get("project"),
            "subject": payload.get("subject"),
            "status": payload.get("status"),
            "description": payload.get("description"),
            "assigned_to": payload.get("assigned_to"),
            "tags": payload.get("tags"),
            "user_story": payload.get("user_story"),
            "created_date": "2025-10-01T12:00:00Z",
            "modified_date": "2025-10-01T12:00:00Z",
        }

    async def get_task(self, task_id: int) -> dict:
        record = dict(self.task_details.get(task_id, {"id": task_id, "version": 1}))
        record.setdefault("version", 1)
        return record

    async def update_task(self, task_id: int, payload: dict) -> dict:
        error = self.raise_map.get("update_task")
        if error:
            raise error
        self.task_updates.append((task_id, payload))
        return {
            "id": task_id,
            "ref": 9,
            "project": 3,
            "subject": payload.get("subject", "Task"),
            "status": payload.get("status"),
            "description": payload.get("description"),
            "assigned_to": payload.get("assigned_to"),
            "tags": payload.get("tags"),
            "user_story": payload.get("user_story"),
            "created_date": "2025-10-01T12:00:00Z",
            "modified_date": "2025-10-02T12:00:00Z",
        }

    async def delete_task(self, task_id: int) -> None:
        error = self.raise_map.get("delete_task")
        if error:
            raise error
        self.deleted_task_ids.append(task_id)

    async def create_issue(self, payload: dict) -> dict:
        error = self.raise_map.get("create_issue")
        if error:
            raise error
        self.issues_created.append(payload)
        return {
            "id": 51,
            "ref": 14,
            "project": payload.get("project"),
            "subject": payload.get("subject"),
            "status": payload.get("status"),
            "priority": payload.get("priority"),
            "severity": payload.get("severity"),
            "issue_type": payload.get("issue_type"),
            "description": payload.get("description"),
            "assigned_to": payload.get("assigned_to"),
            "tags": payload.get("tags"),
            "created_date": "2025-10-01T12:00:00Z",
            "modified_date": "2025-10-01T12:00:00Z",
        }

    async def get_issue(self, issue_id: int) -> dict:
        record = dict(self.issue_details.get(issue_id, {"id": issue_id, "version": 1}))
        record.setdefault("version", 1)
        return record

    async def update_issue(self, issue_id: int, payload: dict) -> dict:
        error = self.raise_map.get("update_issue")
        if error:
            raise error
        self.issue_updates.append((issue_id, payload))
        return {
            "id": issue_id,
            "ref": 14,
            "project": 4,
            "subject": payload.get("subject", "Issue"),
            "status": payload.get("status"),
            "priority": payload.get("priority"),
            "severity": payload.get("severity"),
            "issue_type": payload.get("issue_type"),
            "description": payload.get("description"),
            "assigned_to": payload.get("assigned_to"),
            "tags": payload.get("tags"),
            "created_date": "2025-10-01T12:00:00Z",
            "modified_date": "2025-10-02T12:00:00Z",
        }

    async def delete_issue(self, issue_id: int) -> None:
        error = self.raise_map.get("delete_issue")
        if error:
            raise error
        self.deleted_issue_ids.append(issue_id)


@pytest.fixture()
def proxy_client(monkeypatch):
    monkeypatch.setenv("ACTION_PROXY_API_KEY", "secret")
    fake_client = DummyTaigaClient()

    @asynccontextmanager
    async def fake_ctx():
        yield fake_client

    monkeypatch.setattr(app, "get_taiga_client", fake_ctx)

    @asynccontextmanager
    async def fake_lifespan(_app):
        yield

    monkeypatch.setattr(app.app.router, "lifespan_context", fake_lifespan)

    with TestClient(app.app) as client:
        yield client, fake_client


def test_missing_api_key_returns_401(monkeypatch):
    monkeypatch.setenv("ACTION_PROXY_API_KEY", "secret")
    with TestClient(app.app) as client:
        response = client.get("/actions/list_projects")
    assert response.status_code == 401
    assert response.json()["error"] == "Missing X-Api-Key header"


def test_invalid_api_key_returns_401(proxy_client):
    client, _ = proxy_client
    response = client.get(
        "/actions/list_projects",
        headers={"X-Api-Key": "wrong"},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "Invalid API key"


def test_list_projects_honours_search_filter(proxy_client):
    client, fake_client = proxy_client
    fake_client.projects = [
        {"id": 1, "name": "Alpha", "slug": "alpha", "is_private": False},
        {"id": 2, "name": "Beta", "slug": "beta", "is_private": True},
    ]

    response = client.get(
        "/actions/list_projects?search=beta",
        headers={"X-Api-Key": "secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["projects"] == [
        {"id": 2, "name": "Beta", "slug": "beta", "is_private": True}
    ]
    assert fake_client.list_projects_params == {"member": str(fake_client.user_id)}


def test_list_projects_defaults_to_membership_filter(proxy_client):
    client, fake_client = proxy_client
    fake_client.projects = [{"id": 1, "name": "Alpha", "slug": "alpha", "is_private": True}]

    response = client.get(
        "/actions/list_projects",
        headers={"X-Api-Key": "secret"},
    )

    assert response.status_code == 200
    assert fake_client.list_projects_params == {"member": str(fake_client.user_id)}


def test_list_projects_forwards_query_params(proxy_client):
    client, fake_client = proxy_client
    fake_client.projects = [{"id": 1, "name": "Alpha", "slug": "alpha", "is_private": False}]

    response = client.get(
        "/actions/list_projects?member=12&search=alpha",
        headers={"X-Api-Key": "secret"},
    )

    assert response.status_code == 200
    assert fake_client.list_projects_params == {"member": "12"}


def test_list_user_stories_requires_project_id(proxy_client):
    client, _ = proxy_client

    response = client.get(
        "/actions/list_stories",
        headers={"X-Api-Key": "secret"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "project_id is required"


def test_list_user_stories_passes_filters(proxy_client):
    client, fake_client = proxy_client
    fake_client.user_stories_by_project[99] = [
        {
            "id": 1,
            "ref": 10,
            "subject": "Review prior art",
            "tags": ["ip", "legal"],
            "project": 99,
        }
    ]

    response = client.get(
        "/actions/list_stories?project_id=99&epic_id=5&search=prior%20art&tag=ip&tag=legal&page=2&page_size=50",
        headers={"X-Api-Key": "secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stories"][0]["subject"] == "Review prior art"
    project_id, params = fake_client.list_user_stories_calls.pop()
    assert project_id == 99
    assert params == {
        "epic": 5,
        "q": "prior art",
        "tags": ["ip", "legal"],
        "page": 2,
        "page_size": 50,
    }


def test_list_user_stories_translates_taiga_errors(proxy_client):
    client, fake_client = proxy_client
    fake_client.raise_map["list_user_stories"] = TaigaAPIError("Taiga says no")

    response = client.get(
        "/actions/list_stories?project_id=1",
        headers={"X-Api-Key": "secret"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Taiga says no"


def test_create_story_validates_required_fields(proxy_client):
    client, _ = proxy_client
    response = client.post(
        "/actions/create_story",
        headers={"X-Api-Key": "secret"},
        json={},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Field 'project_id' is required"


def test_create_story_translates_taiga_errors(proxy_client):
    client, fake_client = proxy_client
    fake_client.raise_map["create_user_story"] = TaigaAPIError("Taiga unavailable")

    response = client.post(
        "/actions/create_story",
        headers={"X-Api-Key": "secret"},
        json={"project_id": 1, "subject": "New story"},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "Taiga unavailable"


def test_create_story_success(proxy_client):
    client, fake_client = proxy_client
    fake_client.statuses_by_project[1] = [
        {"id": 5, "name": "In Progress", "slug": "in-progress"}
    ]

    response = client.post(
        "/actions/create_story",
        headers={"X-Api-Key": "secret"},
        json={
            "project_id": 1,
            "subject": "Story title",
            "description": "Body",
            "status": "In Progress",
            "tags": ["api"],
            "assigned_to": 9,
        },
    )

    assert response.status_code == 200
    payload = response.json()["story"]
    assert payload["project"] == 1
    assert payload["subject"] == "Story title"
    assert fake_client.created_payloads[0]["status"] == 5


def test_add_story_to_epic_requires_ids(proxy_client):
    client, _ = proxy_client
    response = client.post(
        "/actions/add_story_to_epic",
        headers={"X-Api-Key": "secret"},
        json={"epic_id": 3},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "Field 'user_story_id' is required"


def test_add_story_to_epic_success(proxy_client):
    client, fake_client = proxy_client
    response = client.post(
        "/actions/add_story_to_epic",
        headers={"X-Api-Key": "secret"},
        json={"epic_id": 7, "user_story_id": 99},
    )
    assert response.status_code == 200
    assert fake_client.link_calls == [(7, 99)]
    assert response.json()["link"] == {"epic": 7, "user_story": 99}


def test_get_project_requires_id(proxy_client):
    client, _ = proxy_client
    response = client.get(
        "/actions/get_project",
        headers={"X-Api-Key": "secret"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "project_id is required"


def test_get_project_returns_payload(proxy_client):
    client, fake_client = proxy_client
    fake_client.project_details[7] = {"id": 7, "name": "Alpha"}

    response = client.get(
        "/actions/get_project?project_id=7",
        headers={"X-Api-Key": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["project"] == {"id": 7, "name": "Alpha"}


def test_get_project_by_slug_requires_slug(proxy_client):
    client, _ = proxy_client
    response = client.get(
        "/actions/get_project_by_slug",
        headers={"X-Api-Key": "secret"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "slug is required"


def test_get_project_by_slug_returns_payload(proxy_client):
    client, fake_client = proxy_client
    fake_client.project_details_by_slug["alpha"] = {"id": 7, "slug": "alpha"}

    response = client.get(
        "/actions/get_project_by_slug?slug=alpha",
        headers={"X-Api-Key": "secret"},
    )

    assert response.status_code == 200
    assert response.json()["project"] == {"id": 7, "slug": "alpha"}


def test_update_story_converts_status_slug(proxy_client):
    client, fake_client = proxy_client
    fake_client.story_details[7] = {"id": 7, "project": 3, "version": 4}
    fake_client.statuses_by_project[3] = [
        {"id": 8, "name": "In Progress", "slug": "in-progress"}
    ]

    response = client.post(
        "/actions/update_story",
        headers={"X-Api-Key": "secret"},
        json={"story_id": 7, "status": "in-progress"},
    )

    assert response.status_code == 200
    assert fake_client.updated_stories == [(7, {"status": 8, "version": 4})]


def test_update_story_requires_fields(proxy_client):
    client, _ = proxy_client
    response = client.post(
        "/actions/update_story",
        headers={"X-Api-Key": "secret"},
        json={"story_id": 7},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "At least one field must be provided to update"


def test_delete_story_success(proxy_client):
    client, fake_client = proxy_client
    response = client.post(
        "/actions/delete_story",
        headers={"X-Api-Key": "secret"},
        json={"story_id": 9},
    )

    assert response.status_code == 200
    assert fake_client.deleted_story_ids == [9]
    assert response.json()["deleted"] == {"story_id": 9}


def test_create_epic_success(proxy_client):
    client, fake_client = proxy_client
    response = client.post(
        "/actions/create_epic",
        headers={"X-Api-Key": "secret"},
        json={
            "project_id": 2,
            "subject": "New epic",
            "status": 4,
        },
    )

    assert response.status_code == 200
    assert fake_client.epics_created[0]["project"] == 2
    assert response.json()["epic"]["subject"] == "New epic"


def test_update_epic_requires_changes(proxy_client):
    client, _ = proxy_client
    response = client.post(
        "/actions/update_epic",
        headers={"X-Api-Key": "secret"},
        json={"epic_id": 4},
    )

    assert response.status_code == 400
    assert response.json()["error"] == "At least one field must be provided to update"


def test_delete_epic_success(proxy_client):
    client, fake_client = proxy_client
    response = client.post(
        "/actions/delete_epic",
        headers={"X-Api-Key": "secret"},
        json={"epic_id": 12},
    )

    assert response.status_code == 200
    assert fake_client.deleted_epic_ids == [12]


def test_create_task_success(proxy_client):
    client, fake_client = proxy_client
    response = client.post(
        "/actions/create_task",
        headers={"X-Api-Key": "secret"},
        json={
            "project_id": 2,
            "subject": "Task",
            "user_story_id": 99,
        },
    )

    assert response.status_code == 200
    assert fake_client.tasks_created[0]["user_story"] == 99


def test_update_task_success(proxy_client):
    client, fake_client = proxy_client
    fake_client.task_details[5] = {"id": 5, "version": 3}
    response = client.post(
        "/actions/update_task",
        headers={"X-Api-Key": "secret"},
        json={
            "task_id": 5,
            "status": 7,
        },
    )

    assert response.status_code == 200
    assert fake_client.task_updates == [(5, {"status": 7, "version": 3})]


def test_delete_task_success(proxy_client):
    client, fake_client = proxy_client
    response = client.post(
        "/actions/delete_task",
        headers={"X-Api-Key": "secret"},
        json={"task_id": 6},
    )

    assert response.status_code == 200
    assert fake_client.deleted_task_ids == [6]


def test_create_issue_success(proxy_client):
    client, fake_client = proxy_client
    response = client.post(
        "/actions/create_issue",
        headers={"X-Api-Key": "secret"},
        json={
            "project_id": 3,
            "subject": "Issue",
            "severity": 2,
            "priority": 4,
            "type": 1,
        },
    )

    assert response.status_code == 200
    assert fake_client.issues_created[0]["severity"] == 2


def test_update_issue_success(proxy_client):
    client, fake_client = proxy_client
    fake_client.issue_details[8] = {"id": 8, "version": 6}
    response = client.post(
        "/actions/update_issue",
        headers={"X-Api-Key": "secret"},
        json={
            "issue_id": 8,
            "status": 5,
        },
    )

    assert response.status_code == 200
    assert fake_client.issue_updates == [(8, {"status": 5, "version": 6})]


def test_delete_issue_success(proxy_client):
    client, fake_client = proxy_client
    response = client.post(
        "/actions/delete_issue",
        headers={"X-Api-Key": "secret"},
        json={"issue_id": 19},
    )

    assert response.status_code == 200
    assert fake_client.deleted_issue_ids == [19]
