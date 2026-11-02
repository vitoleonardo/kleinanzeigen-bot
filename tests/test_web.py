import json
import urllib.error
import urllib.request

import pytest

from ka_db import Store
from ka_web import start
from test_bot import sample_ad


@pytest.fixture
def web():
    store = Store(':memory:')
    store.insert(sample_ad())
    store.update('1', status='judged', score=3)
    server = start(store, '127.0.0.1', 0)
    base = f'http://127.0.0.1:{server.server_port}'
    yield store, base
    server.shutdown()
    server.server_close()
    store.conn.close()


def post(base, data, headers=None, path='/feedback'):
    request = urllib.request.Request(base + path,
        data=data if isinstance(data, bytes) else json.dumps(data).encode(),
        headers=headers if headers is not None else {'Content-Type': 'application/json', 'X-KA-Feedback': '1', 'Origin': base})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_web_feedback_is_shared_changeable_and_reversible(web):
    store, base = web
    status, result = post(base, {'id': '1', 'vote': 1})
    assert status == 200 and result == {'id': '1', 'vote': 1}
    assert store.feedback_examples()[0]['vote'] == 1
    assert store.ad('1')['score'] == 3  # User feedback doesn't overwrite the AI score.
    with urllib.request.urlopen(base + '/ads.json') as response:
        assert json.loads(response.read())[0]['feedback_vote'] == 1
    post(base, {'id': '1', 'vote': -1})
    assert len(store.feedback_examples()) == 1
    assert store.feedback_examples()[0]['vote'] == -1
    store.vote('1', 1)  # Same operation used by Telegram.
    assert store.map_ads()[0]['feedback_vote'] == 1
    assert post(base, {'id': '1', 'vote': 0}) == (200, {'id': '1', 'vote': None})
    assert store.feedback_examples() == []
    assert store.map_ads()[0]['feedback_vote'] is None


@pytest.mark.parametrize('data,status', [
    ({'id': '1', 'vote': True}, 400),
    ({'id': '1', 'vote': '1'}, 400),
    ({'id': '1', 'vote': 2}, 400),
    ({'id': 'missing', 'vote': 1}, 404),
    ({'id': 1, 'vote': 1}, 400),
    ([], 400),
    (b'{bad json', 400),
    (b'x' * 1025, 413),
])
def test_invalid_feedback_does_not_write(web, data, status):
    store, base = web
    assert post(base, data)[0] == status
    assert store.feedback_examples() == []


@pytest.mark.parametrize('headers,status', [
    ({'Content-Type': 'application/json'}, 403),
    ({'Content-Type': 'text/plain', 'X-KA-Feedback': '1'}, 415),
    ({'Content-Type': 'application/json', 'X-KA-Feedback': '1', 'Origin': 'https://unrelated.example'}, 403),
    ({'Content-Type': 'application/json', 'X-KA-Feedback': '1', 'Origin': 'null'}, 403),
    ({'Content-Type': 'application/json', 'X-KA-Feedback': '1', 'Sec-Fetch-Site': 'cross-site'}, 403),
])
def test_feedback_rejects_cross_site_requests(web, headers, status):
    store, base = web
    assert post(base, {'id': '1', 'vote': 1}, headers)[0] == status
    assert store.feedback_examples() == []


def test_feedback_retains_private_network_access_check(web, monkeypatch):
    import ka_web
    store, base = web
    monkeypatch.setattr(ka_web, 'client_allowed', lambda _: False)
    assert post(base, {'id': '1', 'vote': 1})[0] == 403
    assert store.feedback_examples() == []


def test_config_script_carries_search_for_the_map():
    store = Store(':memory:')
    site = {'label': 'Köln', 'radius_km': 10, 'center': [6.96, 50.94]}
    server = start(store, '127.0.0.1', 0, site=site)
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/config.js') as response:
            body = response.read().decode()
        assert body == f'window.KA_SITE = {json.dumps(site, ensure_ascii=False)};'
    finally:
        server.shutdown()
        server.server_close()
        store.conn.close()
