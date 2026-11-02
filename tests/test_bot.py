import datetime as dt
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ka_bot  # noqa: E402
import ka_scrape  # noqa: E402
from ka_db import Store  # noqa: E402
from ka_judge import (CachedJudge, JudgeError, exclude_pattern, is_excluded,  # noqa: E402
                      make_judge, usage_record)
from ka_telegram import Telegram, TelegramError, ad_caption, ad_keyboard  # noqa: E402
from ka_web import client_allowed  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
TZ = ka_bot.TZ


def test_parse_list():
    ads = ka_scrape.parse_list((FIX / "list.html").read_text())
    assert [a["id"] for a in ads] == ["1000000001", "1000000002", "1000000003"]
    a = ads[0]
    assert a["url"] == "https://www.kleinanzeigen.de/s-anzeige/stehlampe-zu-verschenken/1000000001-192-1"
    assert a["title"] == "Stehlampe zu verschenken"
    assert a["description"] == "Stehlampe aus Messing.\nFunktioniert."
    assert a["location"] == "12345 Musterstadt"
    assert a["distance"] == "ca. 3 km"
    assert a["posted"].startswith("Heute")
    assert a["image"].startswith("https://img.example.org/")
    assert ads[1]["title"] == "Werkbank & Regal"  # ohne JSON-LD aus der Überschrift


def test_parse_detail():
    d = ka_scrape.parse_detail((FIX / "detail.html").read_text())
    assert (d["lat"], d["lon"]) == (52.5200066, 13.404954)
    assert d["description"] == "Stehlampe aus Messing.\nFunktioniert & ist sauber.\nNur Abholung."
    assert d["image"].endswith("rule=$_59.JPG&v=1")


KOELN = {"_0": "Deutschland", "_945": "Köln - Nordrhein-Westfalen", "_977": "Innenstadt - Köln"}
NEUSTADT = {"_0": "Deutschland", "_41": "Neustadt - Bremen", "_14655": "Neustadt - Hessen",
            "_5370": "Neustadt an der Weinstraße - Rheinland-Pfalz"}


def test_pick_location():
    assert ka_scrape.pick_location("köln", KOELN) == ("945", "Köln - Nordrhein-Westfalen")
    assert ka_scrape.pick_location("50672", {"_0": "Deutschland", "_985": "50672 Köln Altstadt"}) \
        == ("985", "50672 Köln Altstadt")
    assert ka_scrape.pick_location("Neustadt - Hessen", NEUSTADT)[0] == "14655"
    # Bei nur einem Vorschlag genügt auch ein unvollständiger Name
    assert ka_scrape.pick_location("Bad Homb", {"_0": "Deutschland", "_4349": "Bad Homburg - Hessen"})[0] == "4349"


def test_pick_location_never_guesses():
    with pytest.raises(ka_scrape.LocationError, match="KA_LOCATION=Neustadt - Hessen"):
        ka_scrape.pick_location("Neustadt", NEUSTADT)
    with pytest.raises(ka_scrape.LocationError, match="nicht gefunden"):
        ka_scrape.pick_location("xyzxyz", {})
    with pytest.raises(ka_scrape.LocationError, match="nicht gefunden"):
        ka_scrape.pick_location("Atlantis", {"_0": "Deutschland"})


def fake_lookup(monkeypatch, places=None):
    calls = []
    places = places or {"Köln": ("945", "Köln - Nordrhein-Westfalen"), "Berlin": ("3331", "Berlin")}

    def find(query):
        calls.append(query)
        return places[query]
    monkeypatch.setattr(ka_scrape, "find_location", find)
    monkeypatch.setattr(ka_scrape, "geocode_location", lambda label: (50.9, 6.9))
    return calls


def test_configure_search_builds_url_and_caches(monkeypatch):
    calls = fake_lookup(monkeypatch)
    s = Store(":memory:")
    search = ka_bot.configure_search(s, "Köln", 10)
    assert search["url"] == "https://www.kleinanzeigen.de/s-zu-verschenken/sortierung:neueste/c192l945r10"
    assert search["center"] == [6.9, 50.9]
    assert ka_bot.configure_search(s, "Köln", 10) == search
    assert calls == ["Köln"]  # Neustart braucht die Ortssuche nicht erneut


def test_configure_search_rejects_unknown_radius():
    with pytest.raises(ka_scrape.LocationError, match="KA_RADIUS_KM"):
        ka_bot.configure_search(Store(":memory:"), "Köln", 7)


def test_configure_search_retries_failed_geocoding(monkeypatch):
    fake_lookup(monkeypatch)
    s = Store(":memory:")
    monkeypatch.setattr(ka_scrape, "geocode_location", lambda label: None)
    assert ka_bot.configure_search(s, "Köln", 5)["center"] is None
    monkeypatch.setattr(ka_scrape, "geocode_location", lambda label: (50.9, 6.9))
    assert ka_bot.configure_search(s, "Köln", 5)["center"] == [6.9, 50.9]


def test_changing_search_reseeds_without_losing_feedback(monkeypatch):
    fake_lookup(monkeypatch)
    s = Store(":memory:")
    ka_bot.configure_search(s, "Köln", 5)
    bot = ka_bot.Bot(s, SimpleNamespace(chat_id=""), None, dry_run=True)
    bot.ingest([sample_ad("1")])
    bot.ingest([sample_ad("2")])
    s.update("1", status="judged", score=9, judged_at=time.time())
    s.vote("1", 1)
    assert s.ad("2")["status"] == "new"

    ka_bot.configure_search(s, "Berlin", 5)
    assert s.pending() == [] and s.map_ads() == []
    assert s.digest_candidates(5, 10) == []
    assert s.feedback_examples()[0]["title"] == "Vintage Sessel"
    bot.ingest([sample_ad("3")])
    assert s.ad("3")["status"] == "seeded"  # neue Stadt: nichts rückwirkend melden

    ka_bot.configure_search(s, "Berlin", 5)
    assert s.ad("3")["status"] == "seeded"  # Neustart mit gleicher Suche ändert nichts


def test_existing_database_keeps_its_ads_on_upgrade(monkeypatch):
    fake_lookup(monkeypatch)
    s = Store(":memory:")
    s.insert(sample_ad("1"), status="seeded")
    s.set("seeded", 1)
    ka_bot.configure_search(s, "Köln", 5)
    assert s.ad("1")["status"] == "seeded" and s.get("seeded") == "1"


def test_scrape_uses_configured_search(monkeypatch):
    urls = []
    monkeypatch.setattr(ka_scrape, "fetch", lambda url: urls.append(url) or (200, (FIX / "list.html").read_text()))
    bot = ka_bot.Bot(Store(":memory:"), SimpleNamespace(chat_id=""), None, search_url="https://example.org/suche")
    assert len(bot.scrape()) == 3
    assert urls == ["https://example.org/suche"]


def test_quiet_hours():
    at = lambda h: dt.datetime(2026, 10, 19, h, 30, tzinfo=TZ)
    assert ka_bot.is_quiet(at(23)) and ka_bot.is_quiet(at(3))
    assert not ka_bot.is_quiet(at(7)) and not ka_bot.is_quiet(at(22))
    assert ka_bot.should_be_silent(at(2), 8)
    assert not ka_bot.should_be_silent(at(2), 9)  # Highlights klingeln trotzdem
    assert not ka_bot.should_be_silent(at(12), 7)


def test_digest_due():
    t = dt.datetime(2026, 10, 19, 19, 5, tzinfo=TZ)
    assert ka_bot.digest_due(t, None)
    assert not ka_bot.digest_due(t, "2026-10-19")
    assert not ka_bot.digest_due(t.replace(hour=18), None)


def test_budget():
    assert abs(usage_record(inp=1_000_000, out=1_000_000)["cost_usd"] - 0.60) < 1e-9
    assert usage_record(cost=0.01)["cost_usd"] == 0.01
    assert not ka_bot.budget_exceeded(5.0, 5.0, 0.92)
    assert ka_bot.budget_exceeded(5.5, 5.0, 0.92)


def test_exclude_words():
    pattern = exclude_pattern("baby*, kinderwagen, maxi-cosi")
    assert is_excluded({"title": "Kinderwagen Bugaboo", "description": ""}, pattern)
    assert is_excluded({"title": "Paket", "description": "Babykleidung Gr. 68"}, pattern)
    assert is_excluded({"title": "Maxi-Cosi", "description": ""}, pattern)
    assert not is_excluded({"title": "Dell Monitor 27 Zoll", "description": "funktioniert"}, pattern)
    assert not is_excluded({"title": "Kinderwagenrad", "description": ""}, pattern)
    assert exclude_pattern(" , ") is None
    assert not is_excluded({"title": "Kinderwagen", "description": ""}, None)


def test_make_judge_reports_missing_setup(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(JudgeError, match="ANTHROPIC_API_KEY"):
        make_judge("api")
    with pytest.raises(JudgeError, match="cli oder api"):
        make_judge("gpt")
    with pytest.raises(JudgeError, match="nicht gefunden"):
        make_judge("cli", claude_bin="/nonexistent/claude")


def test_digest_candidates_and_seeding():
    s = Store(":memory:")
    ad = {"id": "1", "url": "u", "title": "t", "description": "", "image": "",
          "location": "", "distance": "", "posted": ""}
    s.insert(ad)
    s.update("1", status="judged", score=6, judged_at=time.time())
    s.insert({**ad, "id": "2"})
    s.update("2", status="judged", score=8, judged_at=time.time(), notified=1)
    assert [a["id"] for a in s.digest_candidates(5, 6)] == ["1"]

    bot = ka_bot.Bot(s, SimpleNamespace(chat_id=""), None, dry_run=True)
    assert bot.ingest([{**ad, "id": "3"}, {**ad, "id": "4"}]) == 2
    assert s.ad("3")["status"] == "seeded"  # Erststart meldet nichts
    assert bot.ingest([{**ad, "id": "5"}, {**ad, "id": "3"}]) == 1
    assert s.ad("5")["status"] == "new"


def test_caption_escapes():
    c = ad_caption({"id": "1", "url": "https://x/1", "title": "<b>Lampe</b> & Tisch",
                    "location": "50672 Köln", "score": 8, "reason": "schön"}, "http://h:1")
    assert "&lt;b&gt;Lampe" in c and "&amp; Tisch" in c
    assert len(c.splitlines()) == 3 and "href=" not in c
    keys = ad_keyboard({"id": "1", "url": "https://x/1"}, map_url="http://h:1")
    assert keys["inline_keyboard"][0][1]["url"] == "http://h:1/?ad=1"


def test_client_allowed():
    assert client_allowed("192.168.1.50")
    assert client_allowed("100.101.1.2")
    assert client_allowed("172.17.0.5")
    assert not client_allowed("8.8.8.8")


def test_usage_tracking():
    s = Store(":memory:")
    s.add_usage("2026-10-19", "score", usage_record(inp=10, out=100, cache_write=2000, thinking=40, cost=0.001))
    s.add_usage("2026-10-19", "score", usage_record(inp=5, out=50, cache_read=2000, cost=0.0005))
    s.add_usage("2026-10-20", "inquiry", usage_record(inp=1, out=20, cost=0.0002))
    day = s.usage_sum("2026-10-19")
    assert day["calls"] == 2 and day["output"] == 150 and day["thinking"] == 40
    month = s.usage_sum("2026-10")
    assert month["calls"] == 3 and abs(month["cost_usd"] - 0.0017) < 1e-9
    assert abs(s.month_cost_usd("2026-10") - 0.0017) < 1e-9


def sample_ad(ad_id="1", **updates):
    return {"id": ad_id, "url": "https://example.org/" + ad_id, "title": "Vintage Sessel",
            "description": "Sessel aus Massivholz, guter Zustand, Abholung am Wochenende möglich.",
            "image": "", "location": "50672 Köln", "distance": "1 km", "posted": "Heute",
            "score": 7, "reason": "Passt zum Profil.", **updates}


def test_recommendations_start_at_seven(monkeypatch):
    s = Store(":memory:")
    for score in (6, 7, 8, 9, 10):
        ad = sample_ad(str(score))
        s.insert(ad)
        s.update(ad["id"], enriched=1)
    judge = SimpleNamespace(score=lambda ad, *_: (int(ad["id"]), "Passend", []))
    bot = ka_bot.Bot(s, SimpleNamespace(chat_id=""), judge)
    sent = []
    monkeypatch.setattr(bot, "send_ad", lambda ad: sent.append(ad["score"]))
    assert bot.process_pending() == 5
    assert sorted(sent) == [7, 8, 9, 10]
    assert bot.process_pending() == 0  # Known listings are never evaluated twice.


def test_telegram_one_photo_and_text_fallback(monkeypatch):
    tg = Telegram("unused", "1")
    calls = []
    def call(method, **params):
        calls.append((method, params))
        return {"message_id": 42}
    monkeypatch.setattr(tg, "call", call)
    ad = sample_ad(image="https://example.org/photo.jpg", lat=50.94, lon=6.93)
    assert tg.send_ad(ad, True, "http://map.home") == 42
    assert [method for method, _ in calls] == ["sendPhoto"]
    assert calls[0][1]["disable_notification"] is True
    assert "http" not in calls[0][1]["caption"]
    assert calls[0][1]["reply_markup"]["inline_keyboard"][0][1]["text"] == "🗺 Karte"
    calls.clear()
    def fail_photo(method, **params):
        calls.append((method, params))
        if method == "sendPhoto":
            raise TelegramError("unavailable image")
        return {"message_id": 43}
    monkeypatch.setattr(tg, "call", fail_photo)
    assert tg.send_ad(ad, False, "http://map.home") == 43
    assert [method for method, _ in calls] == ["sendPhoto", "sendMessage"]
    assert calls[-1][1]["reply_markup"]["inline_keyboard"][0][1]["url"] == "http://map.home/?ad=1"
    calls.clear()
    assert tg.send_ad(sample_ad(), False, "") == 43
    assert [method for method, _ in calls] == ["sendMessage"]


def test_feedback_preserves_map_button(monkeypatch):
    s = Store(":memory:")
    ad = sample_ad()
    s.insert(ad)
    calls = []
    tg = SimpleNamespace(chat_id="1", call=lambda method, **kw: calls.append((method, kw)))
    bot = ka_bot.Bot(s, tg, None, map_url="http://map.home")
    bot.handle_callback({"id": "callback", "data": "up:1", "message": {"message_id": 42, "chat": {"id": 1}}})
    markup = calls[0][1]["reply_markup"]
    assert markup["inline_keyboard"][0][1]["url"] == "http://map.home/?ad=1"
    assert markup["inline_keyboard"][1][0]["text"] == "👍 gespeichert"


def test_long_caption_is_valid_html():
    from html import unescape
    from xml.etree import ElementTree
    ad = sample_ad(title='"<&😀' * 1000, location="Köln " * 100,
                   reason='"<&😀' * 1000, distance="1 km " * 100)
    caption = ad_caption(ad)
    ElementTree.fromstring("<root>" + caption + "</root>")
    assert len(unescape(caption).encode("utf-16-le")) // 2 < 1024
    assert len(caption.splitlines()) == 3


def test_map_search_includes_description_and_missing_coordinates():
    s = Store(":memory:")
    ad = sample_ad(description="Seltenes Suchwort: Teakholz")
    s.insert(ad)
    s.update(ad["id"], status="judged", score=7)
    result = s.map_ads()
    assert result[0]["description"] == ad["description"]
    assert result[0]["lat"] is None


def test_score_cache_reuses_only_identical_context_and_photo(monkeypatch):
    import ka_judge
    s = Store(":memory:")
    calls = []
    photo = ["image/jpeg", "photo-one"]
    monkeypatch.setattr(ka_judge, "download_image", lambda url: tuple(photo) if url else None)
    def score(ad, interests, examples):
        calls.append(ad)
        return 8, "Passend", [usage_record(inp=100, out=10)]
    cached = CachedJudge(SimpleNamespace(score=score), s)
    ad = sample_ad(image="https://example.org/photo.jpg")
    assert cached.score(ad, "Vintage", [])[2]
    assert cached.score({**ad, "id": "repost", "url": "https://example.org/repost"}, "Vintage", [])[2] == []
    assert len(calls) == 1
    assert cached.score(ad, "Werkzeug", [])[2]  # Changed interests.
    feedback = [{"vote": -1, "title": "Sessel", "location": "Köln"}]
    assert cached.score(ad, "Vintage", feedback)[2]
    assert cached.score({**ad, "description": ad["description"] + " Defekt."}, "Vintage", [])[2]
    photo[1] = "photo-two"  # Same URL, changed image bytes.
    assert cached.score(ad, "Vintage", [])[2]
    assert len(calls) == 5
    assert s.cache_hits(ka_bot.now_local().date().isoformat()) == 1
    monkeypatch.setattr(ka_judge, "download_image", lambda _: None)
    assert cached.score(ad, "Vintage", [])[2]
    assert cached.score(ad, "Vintage", [])[2]  # Failed photo download is not cached.
    assert len(calls) == 7


def test_score_cache_expiration_short_text_and_errors(monkeypatch):
    import ka_judge
    s = Store(":memory:")
    monkeypatch.setattr(ka_judge, "download_image", lambda _: None)
    calls = []
    def score(*args):
        calls.append(1)
        return 7, "Passend", [usage_record(inp=30)]
    cached = CachedJudge(SimpleNamespace(score=score), s)
    ad = sample_ad()
    cached.score(ad, "Vintage", [])
    s._q("UPDATE score_cache SET created_at=?", (time.time() - 8 * 86400,))
    cached.score(ad, "Vintage", [])
    assert len(calls) == 2
    short = {**ad, "description": "Abholung"}
    cached.score(short, "Vintage", [])
    cached.score(short, "Vintage", [])
    assert len(calls) == 4
    def fail(*args):
        raise ka_judge.JudgeError("retry later")
    cached.judge.score = fail
    import pytest
    with pytest.raises(ka_judge.JudgeError):
        cached.score(ad, "New interests", [])
    cached.judge.score = score
    assert cached.score(ad, "New interests", [])[2]


def test_usage_average_excludes_inquiries_and_cache_hits():
    s = Store(":memory:")
    day = "2026-10-21"
    s.add_usage(day, "score", usage_record(inp=100, out=20, cache_read=80))
    s.add_usage(day, "inquiry", usage_record(inp=500, out=100))
    s.record_score_cache_hit(day)
    bot = ka_bot.Bot(s, SimpleNamespace(chat_id=""), None)
    text = bot.usage_text("Heute", day)
    assert "2 KI-Aufrufe" in text
    assert "Ø 200 Tokens/KI-Aufruf · 1 wiederverwendet" in text
