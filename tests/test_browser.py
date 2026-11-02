"""Opt-in browser checks: KA_BROWSER_TESTS=1 uv run --with pytest --with playwright pytest tests."""
import os
import time
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("KA_BROWSER_TESTS") != "1", reason="opt-in browser checks")


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        args = {"headless": True, "args": ["--no-sandbox"]}
        if os.environ.get("KA_CHROMIUM"):
            args["executable_path"] = os.environ["KA_CHROMIUM"]
        b = p.chromium.launch(**args)
        yield b
        b.close()


@pytest.fixture(scope="module")
def map_assets():
    # Real MapLibre, with a local blank style so these checks don't depend on map tiles.
    assets = {}
    for ext in ("js", "css"):
        with urllib.request.urlopen(f"https://unpkg.com/maplibre-gl@5/dist/maplibre-gl.{ext}", timeout=30) as r:
            assets[ext] = r.read()
    return assets


@pytest.fixture
def ui(browser, map_assets):
    from ka_db import Store
    from ka_web import start
    from test_bot import sample_ad
    store = Store(":memory:")
    for i in range(61):
        ad = sample_ad(str(i), title=f"Sessel {i:02}", description=f"Teakholz Suchwort{i:02} aus Köln",
                       score=7 if i < 60 else 6)
        store.insert(ad)
        store.update(ad["id"], status="judged", score=ad["score"],
                     first_seen=time.time() - i * 60, lat=50.94 + i * .001, lon=6.94 + i * .001)
    server = start(store, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_port}"
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.route("https://unpkg.com/**", lambda route: route.fulfill(
        body=map_assets["css" if route.request.url.endswith(".css") else "js"],
        content_type="text/css" if route.request.url.endswith(".css") else "text/javascript"))
    context.route("https://tiles.openfreemap.org/**", lambda route: route.fulfill(
        json={"version": 8, "sources": {}, "layers": [{"id": "bg", "type": "background",
              "paint": {"background-color": "#eef1e9"}}]}))
    context.route("https://example.org/**", lambda route: route.fulfill(body="<h1>Listing</h1>", content_type="text/html"))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base)
    page.wait_for_function("initialized && map && map.loaded()")
    yield page, base, store
    assert errors == []
    context.close()
    server.shutdown()
    server.server_close()
    store.conn.close()


def test_search_pagination_and_new_tab(ui):
    page, base, _ = ui
    assert page.locator('#list li[data-id]').count() == 25
    assert page.locator('#count').inner_text() == '60 Anzeigen'
    assert page.evaluate('Object.keys(markers).length') == 60
    page.locator('#next').click()
    assert page.locator('#page-label').inner_text() == 'Seite 2 / 3'
    page.locator('#list').evaluate('(el) => el.scrollTop = 430')
    before = page.locator('#list').evaluate('(el) => el.scrollTop')
    with page.expect_popup() as popup:
        page.locator('#list a.title').nth(4).click()
    popup.value.wait_for_load_state()
    assert popup.value.url.startswith('https://example.org/')
    popup.value.close()
    assert page.url == base + '/'
    assert abs(page.locator('#list').evaluate('(el) => el.scrollTop') - before) < 2
    page.reload()
    page.wait_for_function('initialized')
    assert page.locator('#page-label').inner_text() == 'Seite 2 / 3'
    assert abs(page.locator('#list').evaluate('(el) => el.scrollTop') - before) < 2
    page.locator('#search').fill('Suchwort42')  # Description-only match.
    assert page.locator('#count').inner_text() == '1 Anzeigen'
    assert page.locator('#list a.title').inner_text() == 'Sessel 42'
    assert page.locator('#page-label').inner_text() == 'Seite 1 / 1'
    assert page.locator('#next').is_disabled()
    page.reload()
    page.wait_for_function('initialized')
    assert page.locator('#search').input_value() == 'Suchwort42'
    assert page.locator('#list a.title').inner_text() == 'Sessel 42'
    page.locator('#search').fill('not present')
    assert page.locator('#list .empty').is_visible()
    assert page.locator('#prev').is_disabled()
    page.locator('#search').fill('Suchwort60')
    page.locator('#good').uncheck()
    assert page.locator('#list a.title').inner_text() == 'Sessel 60'
    page.locator('#sort').select_option('score')
    page.reload()
    page.wait_for_function('initialized')
    assert not page.locator('#good').is_checked()
    assert page.locator('#sort').input_value() == 'score'


def test_refresh_keeps_list_camera_and_popup(ui):
    from test_bot import sample_ad
    page, _, store = ui
    page.locator('#next').click()
    page.locator('#list').evaluate('(el) => el.scrollTop = 400')
    page.locator('#list [data-map]').nth(4).click()
    page.wait_for_function('!map.isMoving() && popupId !== null')
    before = page.evaluate('({scroll: $("list").scrollTop, center: map.getCenter().toArray(), zoom: map.getZoom(), popup: popupId})')
    store.insert(sample_ad('new', title='New listing'))
    store.update('new', status='judged', score=9, lat=50.95, lon=6.95)
    page.evaluate('load()')
    assert page.locator('#refresh').is_visible()
    assert page.locator('#count').inner_text() == '60 Anzeigen'
    after = page.evaluate('({scroll: $("list").scrollTop, center: map.getCenter().toArray(), zoom: map.getZoom(), popup: popupId})')
    assert before == after
    page.locator('#refresh').click()
    assert page.locator('#count').inner_text() == '61 Anzeigen'
    assert page.evaluate('popupId') == before['popup']
    assert page.evaluate('markers[popupId].getPopup().isOpen()')
    camera = page.evaluate('({center: map.getCenter().toArray(), zoom: map.getZoom()})')
    page.reload()
    page.wait_for_function('initialized && map.loaded()')
    restored = page.evaluate('({center: map.getCenter().toArray(), zoom: map.getZoom()})')
    assert restored['center'] == pytest.approx(camera['center'])
    assert restored['zoom'] == pytest.approx(camera['zoom'])
    assert page.evaluate('popupId') == before['popup']
    assert page.evaluate('markers[popupId].getPopup().isOpen()')


def test_mobile_restore_and_deep_link(ui):
    page, base, _ = ui
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#next').click()
    page.locator('#list').evaluate('(el) => el.scrollTop = 300')
    before = page.locator('#list').evaluate('(el) => el.scrollTop')
    page.locator('#switch [data-view="map"]').click()
    page.reload()
    page.wait_for_function('initialized')
    assert page.locator('body').get_attribute('class').find('view-map') >= 0
    page.locator('#switch [data-view="list"]').click()
    assert page.locator('#page-label').inner_text() == 'Seite 2 / 3'
    assert abs(page.locator('#list').evaluate('(el) => el.scrollTop') - before) < 2
    page.goto(base + '/?ad=60')  # Below default filter, on a later page.
    page.wait_for_function('initialized && popupId === "60"')
    assert not page.locator('#good').is_checked()
    assert page.evaluate('markers["60"].getPopup().isOpen()')
    page.locator('#switch [data-view="list"]').click()
    assert page.locator('#list li.sel a.title').inner_text() == 'Sessel 60'
    page.goto(base)
    page.wait_for_function('initialized')
    assert page.locator('#page-label').inner_text() == 'Seite 2 / 3'
    assert page.locator('#good').is_checked()  # Deep link didn't overwrite the main tab state.


def test_failed_refresh_retains_results_and_recovers(ui):
    page, _, _ = ui
    page.route('**/ads.json', lambda route: route.fulfill(status=503, body='Unavailable'))
    page.evaluate('load()')
    assert page.locator('#notice').inner_text().startswith('Anzeigen konnten nicht geladen werden')
    assert page.locator('#list li[data-id]').count() == 25
    page.unroute('**/ads.json')
    page.locator('#refresh').click()
    page.wait_for_function('!loading')
    assert page.locator('#notice').inner_text() == ''
    assert page.locator('#refresh').is_hidden()


def test_map_feedback_changes_and_undoes_without_moving(ui):
    page, _, store = ui
    page.locator('#next').click()
    page.locator('#list').evaluate('(el) => el.scrollTop = 400')
    page.locator('#list [data-map]').nth(3).click()
    page.wait_for_function('!map.isMoving() && popupId !== null')
    ad_id = page.evaluate('popupId')
    before = page.evaluate('({scroll: $("list").scrollTop, center: map.getCenter().toArray(), zoom: map.getZoom(), page, popup: popupId})')
    popup = page.locator('.maplibregl-popup .feedback')
    good = popup.get_by_role('button', name='Gefällt mir', exact=True)
    good.click()
    page.wait_for_function('feedbackPending.size === 0')
    assert store.feedback_examples()[0]['vote'] == 1
    assert good.get_attribute('aria-pressed') == 'true'
    row = page.locator(f'#list [data-feedback-id="{ad_id}"]')
    assert row.get_by_role('button', name='Gefällt mir', exact=True).get_attribute('aria-pressed') == 'true'
    after = page.evaluate('({scroll: $("list").scrollTop, center: map.getCenter().toArray(), zoom: map.getZoom(), page, popup: popupId})')
    assert before == after
    popup.get_by_role('button', name='Nicht interessant', exact=True).click()
    page.wait_for_function('feedbackPending.size === 0')
    assert store.feedback_examples()[0]['vote'] == -1
    page.reload()
    page.wait_for_function('initialized && popupId !== null')
    assert popup.get_by_role('button', name='Nicht interessant', exact=True).get_attribute('aria-pressed') == 'true'
    popup.get_by_role('button', name='Nicht interessant', exact=True).click()
    page.wait_for_function('feedbackPending.size === 0')
    assert store.feedback_examples() == []
    assert popup.get_by_role('button', name='Nicht interessant', exact=True).get_attribute('aria-pressed') == 'false'


def test_low_score_feedback_from_mobile_list_and_popup(ui):
    page, _, store = ui
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('#good').uncheck()
    page.locator('#search').fill('Suchwort60')
    good = page.locator('#list .feedback').get_by_role('button', name='Gefällt mir', exact=True)
    good.click()
    page.wait_for_function('feedbackPending.size === 0')
    assert store.feedback_examples()[0]['title'] == 'Sessel 60'
    assert store.ad('60')['score'] == 6
    page.locator('#list [data-map]').click()
    page.wait_for_function('popupId === "60" && !map.isMoving()')
    popup = page.locator('.maplibregl-popup .feedback')
    assert popup.get_by_role('button', name='Gefällt mir', exact=True).get_attribute('aria-pressed') == 'true'
    popup.get_by_role('button', name='Nicht interessant', exact=True).click()
    page.wait_for_function('feedbackPending.size === 0')
    assert store.feedback_examples()[0]['vote'] == -1
    page.locator('#switch [data-view="list"]').click()
    assert page.locator('#search').input_value() == 'Suchwort60'
    assert page.locator('#list .feedback').get_by_role('button', name='Nicht interessant', exact=True).get_attribute('aria-pressed') == 'true'


def test_feedback_failure_keeps_previous_vote_and_allows_retry(ui):
    page, _, store = ui
    page.route('**/feedback', lambda route: route.fulfill(status=500, body='Unavailable'))
    good = page.locator('#list .feedback').first.get_by_role('button', name='Gefällt mir', exact=True)
    good.click()
    page.wait_for_function('feedbackPending.size === 0')
    assert page.locator('#list .feedback-status.error').is_visible()
    assert good.get_attribute('aria-pressed') == 'false'
    assert good.is_enabled()
    assert store.feedback_examples() == []
    page.unroute('**/feedback')
    good.click()
    page.wait_for_function('feedbackPending.size === 0')
    assert store.feedback_examples()[0]['vote'] == 1
    assert good.get_attribute('aria-pressed') == 'true'
    assert page.locator('#list .feedback-status.error').count() == 0


def test_staged_refresh_does_not_undo_feedback(ui):
    from test_bot import sample_ad
    page, _, store = ui
    store.insert(sample_ad('new', title='New listing'))
    store.update('new', status='judged', score=9)
    page.evaluate('load()')
    assert page.locator('#refresh').is_visible()
    page.locator('#list .feedback').first.get_by_role('button', name='Gefällt mir', exact=True).click()
    page.wait_for_function('feedbackPending.size === 0')
    page.locator('#refresh').click()
    group = page.locator('#list [data-feedback-id="0"]')
    assert group.get_by_role('button', name='Gefällt mir', exact=True).get_attribute('aria-pressed') == 'true'
    store.vote('0', -1)  # A subsequent Telegram vote is authoritative on the next refresh.
    page.evaluate('load()')
    page.locator('#refresh').click()
    assert group.get_by_role('button', name='Nicht interessant', exact=True).get_attribute('aria-pressed') == 'true'


def test_map_follows_configured_location(browser, map_assets):
    from ka_db import Store
    from ka_web import start
    store = Store(":memory:")
    server = start(store, "127.0.0.1", 0, site={"label": "Leipzig", "radius_km": 20, "center": [12.37, 51.34]})
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.route("https://unpkg.com/**", lambda route: route.fulfill(
        body=map_assets["css" if route.request.url.endswith(".css") else "js"],
        content_type="text/css" if route.request.url.endswith(".css") else "text/javascript"))
    context.route("https://tiles.openfreemap.org/**", lambda route: route.fulfill(
        json={"version": 8, "sources": {}, "layers": []}))
    page = context.new_page()
    page.goto(f"http://127.0.0.1:{server.server_port}")
    page.wait_for_function("map && map.loaded() && map.getSource('radius')")
    assert page.title() == "Zu verschenken · Leipzig"
    assert page.locator("#heading").inner_text() == "🎁 Zu verschenken · Leipzig"
    assert page.evaluate("map.getCenter().toArray()") == pytest.approx([12.37, 51.34])
    assert page.evaluate("map.getZoom()") == pytest.approx(10.5)
    context.close()
    server.shutdown()
    server.server_close()
    store.conn.close()


def test_map_works_without_known_center(browser, map_assets):
    from ka_db import Store
    from ka_web import start
    from test_bot import sample_ad
    store = Store(":memory:")
    ad = sample_ad("1")
    store.insert(ad)
    store.update("1", status="judged", score=8, lat=52.5, lon=13.4)
    server = start(store, "127.0.0.1", 0)
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    context.route("https://unpkg.com/**", lambda route: route.fulfill(
        body=map_assets["css" if route.request.url.endswith(".css") else "js"],
        content_type="text/css" if route.request.url.endswith(".css") else "text/javascript"))
    context.route("https://tiles.openfreemap.org/**", lambda route: route.fulfill(
        json={"version": 8, "sources": {}, "layers": []}))
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"http://127.0.0.1:{server.server_port}")
    page.wait_for_function("initialized && map && map.loaded()")
    assert errors == []
    assert page.evaluate("map.getSource('radius')") is None
    assert page.evaluate("map.getCenter().toArray()") == pytest.approx([13.4, 52.5], abs=0.01)
    context.close()
    server.shutdown()
    server.server_close()
    store.conn.close()
