import json

import pytest

from app import _NormalizeToolNames


@pytest.fixture()
def anyio_backend():
    return 'asyncio'


async def _noop_send(message):
    pass


def _scope(method, path, scope_type='http'):
    return {
        'type': scope_type,
        'http_version': '1.1',
        'method': method,
        'path': path,
        'raw_path': path.encode(),
        'root_path': '',
        'scheme': 'http',
        'query_string': b'',
        'headers': [],
        'client': ('127.0.0.1', 12345),
        'server': ('127.0.0.1', 80),
    }


def _make_receive(messages):
    calls = 0

    async def receive():
        nonlocal calls
        msg = messages[calls]
        calls += 1
        return msg

    return receive


def _make_failing_receive():
    async def receive():
        raise AssertionError('request body should not be buffered for this request')

    return receive


def _make_capturing_app(captured, call_receive=True):
    async def app(scope, receive, send):
        captured['scope'] = scope
        captured['receive'] = receive
        captured['send'] = send
        if call_receive:
            captured['request'] = await receive()
            captured['body'] = captured['request'].get('body', b'')
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    return app


@pytest.mark.anyio('asyncio')
async def test_get_healthz_passthrough():
    captured = {}
    receive = _make_failing_receive()
    app = _make_capturing_app(captured, call_receive=False)

    await _NormalizeToolNames(app)(_scope('GET', '/healthz'), receive, _noop_send)

    assert captured['scope']['path'] == '/healthz'
    assert captured['receive'] is receive
    assert captured['send'] is _noop_send


@pytest.mark.anyio('asyncio')
async def test_get_root_passthrough():
    captured = {}
    receive = _make_failing_receive()
    app = _make_capturing_app(captured, call_receive=False)

    await _NormalizeToolNames(app)(_scope('GET', '/'), receive, _noop_send)

    assert captured['scope']['path'] == '/'
    assert captured['receive'] is receive


@pytest.mark.anyio('asyncio')
async def test_head_and_options_passthrough():
    for method in ('HEAD', 'OPTIONS', 'GET'):
        captured = {}
        receive = _make_failing_receive()
        app = _make_capturing_app(captured, call_receive=False)

        await _NormalizeToolNames(app)(_scope(method, '/mcp'), receive, _noop_send)

        assert captured['scope']['method'] == method
        assert captured['receive'] is receive


@pytest.mark.anyio('asyncio')
async def test_non_mcp_post_passthrough():
    captured = {}
    receive = _make_failing_receive()
    app = _make_capturing_app(captured, call_receive=False)

    await _NormalizeToolNames(app)(_scope('POST', '/actions/diagnostics'), receive, _noop_send)

    assert captured['scope']['path'] == '/actions/diagnostics'
    assert captured['receive'] is receive


@pytest.mark.anyio('asyncio')
async def test_post_mcp_rewrites_dot_to_underscore():
    captured = {}
    payload = {
        'jsonrpc': '2.0',
        'method': 'tools/call',
        'params': {'name': 'taiga.projects.list'},
        'id': 1,
    }
    body = json.dumps(payload).encode()
    receive = _make_receive([{'type': 'http.request', 'body': body, 'more_body': False}])
    app = _make_capturing_app(captured)

    await _NormalizeToolNames(app)(_scope('POST', '/mcp'), receive, _noop_send)

    parsed = json.loads(captured['body'])
    assert parsed['params']['name'] == 'taiga_projects_list'


@pytest.mark.anyio('asyncio')
async def test_post_mcp_slash_rewrites_dot_to_underscore():
    captured = {}
    payload = {
        'jsonrpc': '2.0',
        'method': 'tools/call',
        'params': {'name': 'taiga.epics.list'},
        'id': 2,
    }
    body = json.dumps(payload).encode()
    receive = _make_receive([{'type': 'http.request', 'body': body, 'more_body': False}])
    app = _make_capturing_app(captured)

    await _NormalizeToolNames(app)(_scope('POST', '/mcp/'), receive, _noop_send)

    parsed = json.loads(captured['body'])
    assert parsed['params']['name'] == 'taiga_epics_list'


@pytest.mark.anyio('asyncio')
async def test_post_mcp_non_tools_call_unchanged():
    captured = {}
    payload = {
        'jsonrpc': '2.0',
        'method': 'tools/list',
        'params': {},
        'id': 3,
    }
    body = json.dumps(payload).encode()
    receive = _make_receive([{'type': 'http.request', 'body': body, 'more_body': False}])
    app = _make_capturing_app(captured)

    await _NormalizeToolNames(app)(_scope('POST', '/mcp'), receive, _noop_send)

    parsed = json.loads(captured['body'])
    assert parsed == payload


@pytest.mark.anyio('asyncio')
async def test_post_mcp_disconnect_forwarded():
    captured = {}
    payload = {
        'jsonrpc': '2.0',
        'method': 'tools/list',
        'params': {},
        'id': 4,
    }
    body = json.dumps(payload).encode()
    receive = _make_receive([
        {'type': 'http.request', 'body': body, 'more_body': False},
        {'type': 'http.disconnect'},
    ])

    async def app(scope, receive, send):
        first = await receive()
        second = await receive()
        captured['messages'] = [first, second]
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _NormalizeToolNames(app)(_scope('POST', '/mcp'), receive, _noop_send)

    assert json.loads(captured['messages'][0].get('body', b'')) == payload
    assert captured['messages'][1]['type'] == 'http.disconnect'


@pytest.mark.anyio('asyncio')
async def test_post_mcp_chunked_body_reassembled_and_disconnect_forwarded():
    captured = {}
    body = json.dumps({'jsonrpc': '2.0', 'method': 'tools/list', 'params': {}, 'id': 5}).encode()
    receive = _make_receive([
        {'type': 'http.request', 'body': body[:10], 'more_body': True},
        {'type': 'http.request', 'body': body[10:], 'more_body': False},
        {'type': 'http.disconnect'},
    ])

    async def app(scope, receive, send):
        first = await receive()
        second = await receive()
        captured['messages'] = [first, second]
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'ok', 'more_body': False})

    await _NormalizeToolNames(app)(_scope('POST', '/mcp/'), receive, _noop_send)

    assert captured['messages'][0].get('body') == body
    assert captured['messages'][1]['type'] == 'http.disconnect'
