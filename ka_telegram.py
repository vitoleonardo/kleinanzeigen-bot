"""Telegram-Bot-API (stdlib) und Nachrichtenformat."""
import html
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id) if chat_id else ""

    def call(self, method, http_timeout=30, **params):
        req = urllib.request.Request(
            self.base + method, data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
            except (json.JSONDecodeError, OSError):
                raise TelegramError(f"{method}: HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TelegramError(f"{method}: {e}") from e
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def send(self, text, silent=False, **extra):
        return self.call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                         disable_notification=silent,
                         link_preview_options={"is_disabled": True}, **extra)

    def send_ad(self, ad, silent, map_url):
        """Schickt genau eine kompakte Foto- oder Textnachricht mit Buttons."""
        caption = ad_caption(ad, map_url)
        markup = ad_keyboard(ad, map_url=map_url)
        msg = None
        if ad.get("image"):
            try:
                msg = self.call("sendPhoto", chat_id=self.chat_id, photo=ad["image"],
                                caption=caption, parse_mode="HTML",
                                disable_notification=silent, reply_markup=markup)
            except TelegramError:
                msg = None  # Bild nicht ladbar -> Textnachricht
        if msg is None:
            msg = self.send(caption, silent=silent, reply_markup=markup)
        return msg["message_id"]


def _short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def ad_caption(ad, map_url=""):
    # Kürzen vor dem Escaping: HTML-Tags und Entities bleiben vollständig.
    e = html.escape
    where = " · ".join(x for x in (_short(ad.get("location"), 60),
                                   _short(ad.get("distance"), 24)) if x)
    lines = [f"<b>{e(_short(ad['title'], 100))}</b> · ⭐ {ad.get('score', '?')}"]
    if where:
        lines.append(e(where))
    if ad.get("reason"):
        lines.append(e(_short(ad["reason"], 140)))
    return "\n".join(lines)


def ad_keyboard(ad, voted=None, map_url=""):
    actions = [{"text": "Anzeige ↗", "url": ad["url"]}]
    if map_url:
        actions.append({"text": "🗺 Karte", "url": map_url.rstrip("/") + "/?" + urlencode({"ad": ad["id"]})})
    actions.append({"text": "✍️ Anfrage", "callback_data": f"ask:{ad['id']}"})
    feedback = ([{"text": "👍", "callback_data": f"up:{ad['id']}"},
                 {"text": "👎", "callback_data": f"down:{ad['id']}"}]
                if voted is None else
                [{"text": "👍 gespeichert" if voted > 0 else "👎 gespeichert",
                  "callback_data": f"noop:{ad['id']}"}])
    return {"inline_keyboard": [actions, feedback]}


def digest_text(ads, map_url=""):
    e = html.escape
    lines = [f"<b>🗓️ Knapp verpasst heute ({len(ads)})</b>", ""]
    for ad in ads[:15]:
        lines.append(f'• {ad["score"]}/10 <a href="{e(ad["url"])}">{e(ad["title"])}</a>'
                     f" – {e(ad.get('location') or '')}")
    if map_url:
        lines += ["", f'<a href="{e(map_url)}">🗺️ Alle auf der Karte</a>']
    return "\n".join(lines)
