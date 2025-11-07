import json

import pytest

import scripts.actions_proxy_client as cli


class DummyResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload).encode("utf-8") if payload is not None else b""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):  # noqa: D401 - mimic httpx.Response
        return self._payload

    @property
    def text(self) -> str:
        if isinstance(self._payload, str):
            return self._payload
        return json.dumps(self._payload)


class DummyClient:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        return DummyResponse(200, {"ok": True, "params": params})

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        return DummyResponse(200, {"ok": True, "payload": json})

    def close(self):
        pass


@pytest.fixture()
def dummy_client(monkeypatch):
    client = DummyClient()
    monkeypatch.setattr(cli, "_build_client", lambda base_url, api_key: client)
    return client


def test_list_projects_invokes_get(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "--pretty",
        "list-projects",
        "--search",
        "alpha",
    ])

    call = dummy_client.calls.pop()
    assert call == ("GET", "/actions/list_projects", {"search": "alpha"})
    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["params"] == {"search": "alpha"}


def test_list_stories_invokes_get(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "--pretty",
        "list-stories",
        "--project-id",
        "77",
        "--epic-id",
        "5",
        "--search",
        "prior art",
        "--tag",
        "ip",
        "--tag",
        "legal",
        "--page",
        "2",
        "--page-size",
        "25",
    ])

    call = dummy_client.calls.pop()
    assert call == (
        "GET",
        "/actions/list_stories",
        [
            ("project_id", 77),
            ("epic_id", 5),
            ("search", "prior art"),
            ("page", 2),
            ("page_size", 25),
            ("tag", "ip"),
            ("tag", "legal"),
        ],
    )
    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["ok"] is True


def test_get_project_invokes_get(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "get-project",
        "--project-id",
        "77",
    ])

    call = dummy_client.calls.pop()
    assert call == ("GET", "/actions/get_project", {"project_id": 77})
    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["ok"] is True


def test_get_project_by_slug_invokes_get(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "get-project-by-slug",
        "--slug",
        "alpha",
    ])

    call = dummy_client.calls.pop()
    assert call == ("GET", "/actions/get_project_by_slug", {"slug": "alpha"})
    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["ok"] is True


def test_create_story_invokes_post(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "create-story",
        "--project-id",
        "77",
        "--subject",
        "Story",
        "--description",
        "Body",
        "--status",
        "In Progress",
        "--tag",
        "api",
        "--tag",
        "backend",
        "--assigned-to",
        "5",
    ])

    call = dummy_client.calls.pop()
    assert call[0] == "POST"
    assert call[1] == "/actions/create_story"
    assert call[2] == {
        "project_id": 77,
        "subject": "Story",
        "description": "Body",
        "status": "In Progress",
        "tags": ["api", "backend"],
        "assigned_to": 5,
    }
    out, err = capsys.readouterr()
    assert err == ""
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["payload"]["subject"] == "Story"


def test_update_story_invokes_post(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "update-story",
        "--story-id",
        "44",
        "--status",
        "in-progress",
    ])

    call = dummy_client.calls.pop()
    assert call[0] == "POST"
    assert call[1] == "/actions/update_story"
    assert call[2] == {"story_id": 44, "status": "in-progress"}


def test_delete_story_invokes_post(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "delete-story",
        "--story-id",
        "44",
    ])

    call = dummy_client.calls.pop()
    assert call == ("POST", "/actions/delete_story", {"story_id": 44})


def test_create_epic_invokes_post(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "create-epic",
        "--project-id",
        "9",
        "--subject",
        "Epic",
        "--status",
        "4",
    ])

    call = dummy_client.calls.pop()
    assert call == (
        "POST",
        "/actions/create_epic",
        {"project_id": 9, "subject": "Epic", "status": 4},
    )


def test_update_task_invokes_post(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "update-task",
        "--task-id",
        "7",
        "--status",
        "5",
    ])

    call = dummy_client.calls.pop()
    assert call == ("POST", "/actions/update_task", {"task_id": 7, "status": 5})


def test_delete_issue_invokes_post(dummy_client, capsys):
    cli.main([
        "--base-url",
        "https://example.com",
        "--api-key",
        "secret",
        "delete-issue",
        "--issue-id",
        "3",
    ])

    call = dummy_client.calls.pop()
    assert call == ("POST", "/actions/delete_issue", {"issue_id": 3})


def test_error_response_raises(dummy_client):
    def failing_get(path, params=None):
        return DummyResponse(400, {"error": "Invalid"})

    dummy_client.get = failing_get  # type: ignore[assignment]

    with pytest.raises(cli.ActionProxyError) as exc:
        cli.main([
            "--base-url",
            "https://example.com",
            "--api-key",
            "secret",
            "list-projects",
        ])

    assert "HTTP 400" in str(exc.value)
