#!/usr/bin/env python3
"""Kleinanzeigen-"Zu verschenken"-Bot: KI-Bewertung + Telegram + Karte."""
import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import ka_scrape
from ka_db import Store
from ka_judge import JudgeError, exclude_pattern, is_excluded, make_judge
from ka_telegram import Telegram, TelegramError, ad_keyboard, digest_text

HERE = Path(__file__).resolve().parent
TZ = ZoneInfo("Europe/Berlin")
INTERESTS = HERE / "interests.md"
ALARM_COOLDOWN = 6 * 3600

# Befehlsliste für das "/"-Menü in Telegram (setMyCommands) und /hilfe
COMMANDS = [
    ("status", "Letzter Abruf, Anzeigen heute, Kosten"),
    ("tokens", "Token-Verbrauch heute und im Monat"),
    ("interessen", "Profil anzeigen – mit Text: Profil ersetzen"),
    ("mehr", "Text ans Profil anhängen, z. B. /mehr Schuhe Gr. 41"),
    ("karte", "Link zur Übersichtskarte"),
    ("pause", "Meldungen pausieren"),
    ("weiter", "Meldungen fortsetzen"),
    ("test", "Testnachricht schicken"),
    ("hilfe", "Diese Übersicht"),
]
DETAIL_PAUSE = 3  # Sekunden zwischen Detailseiten-Abrufen

log = logging.getLogger("ka")


def load_env(path):
    """Einfacher .env-Parser; vorhandene Umgebungsvariablen haben Vorrang."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def now_local():
    return dt.datetime.now(TZ)


def is_quiet(t, start=23, end=7):
    return t.hour >= start or t.hour < end


def should_be_silent(t, score, loud_from=9):
    return is_quiet(t) and score < loud_from


def digest_due(t, last_digest_date, hour=19):
    return t.hour >= hour and last_digest_date != t.date().isoformat()


def budget_exceeded(spent_usd, budget_eur, usd_eur):
    return spent_usd * usd_eur >= budget_eur


def configure_search(store, query, radius_km):
    """Löst den Ort einmalig je Konfiguration auf und seedet neu, wenn sich die Suche ändert."""
    if radius_km not in ka_scrape.RADII:
        raise ka_scrape.LocationError(
            "KA_RADIUS_KM muss einer dieser Werte sein: " + ", ".join(map(str, ka_scrape.RADII)))
    saved = json.loads(store.get("search", "null"))
    if saved and (saved["query"], saved["radius_km"]) == (query, radius_km):
        search = saved
    else:
        location_id, label = ka_scrape.find_location(query)
        search = {"query": query, "radius_km": radius_km, "label": label, "center": None,
                  "url": ka_scrape.search_url(location_id, radius_km)}
        # Ohne gespeicherte Suche ist es eine bestehende DB: ihre Anzeigen bleiben gültig.
        if saved and saved["url"] != search["url"]:
            store.start_new_search()
            log.info("Neue Suche: %s, %d km", label, radius_km)
    if search["center"] is None:
        geo = ka_scrape.geocode_location(search["label"])
        search["center"] = [geo[1], geo[0]] if geo else None
    store.set("search", json.dumps(search))
    return search


class Bot:
    def __init__(self, store, tg, judge, *, min_score=7, budget_eur=5.0, usd_eur=0.92,
                 map_url="", dry_run=False, search_url="", place="", exclude=None):
        self.store, self.tg, self.judge = store, tg, judge
        self.min_score, self.budget_eur, self.usd_eur = min_score, budget_eur, usd_eur
        self.map_url = map_url.rstrip("/")
        self.dry_run = dry_run
        self.search_url, self.place, self.exclude = search_url, place, exclude
        self.failures = 0

    # --- Hilfen ---
    def month(self):
        return now_local().strftime("%Y-%m")

    def track(self, usages, kind="score"):
        day = now_local().date().isoformat()
        for u in usages:
            self.store.add_usage(day, kind, u)
            log.info("Tokens [%s]: in %d, out %d (davon Thinking %d), Cache %d/%d, %.5f $",
                     kind, u["input"], u["output"], u["thinking"], u["cache_read"],
                     u["cache_write"], u["cost_usd"])

    def usage_text(self, label, prefix):
        u = self.store.usage_sum(prefix)
        total = u["input"] + u["output"] + u["cache_read"] + u["cache_write"]
        scored = self.store.usage_sum(prefix, kind="score")
        score_tokens = sum(scored[k] for k in ("input", "output", "cache_read", "cache_write"))
        average = round(score_tokens / scored["calls"]) if scored["calls"] else 0
        reused = self.store.cache_hits(prefix)
        return (f"<b>{label}</b>: {u['calls']} KI-Aufrufe, {total:,} Tokens\n"
                f"  Input {u['input']:,} · Output {u['output']:,} (Thinking {u['thinking']:,})\n"
                f"  Cache gelesen {u['cache_read']:,} · geschrieben {u['cache_write']:,}\n"
                f"  Bewertungen: Ø {average:,} Tokens/KI-Aufruf · {reused} wiederverwendet\n"
                f"  ≈ {u['cost_usd'] * self.usd_eur:.3f} € (Listenpreis)").replace(",", ".")

    def notify(self, text, silent=False):
        if self.dry_run or not self.tg.chat_id:
            log.info("[notify] %s", text)
            return
        try:
            self.tg.send(text, silent=silent)
        except TelegramError as e:
            log.error("Telegram: %s", e)

    def alarm(self, kind, text):
        last = float(self.store.get(f"alarm:{kind}", 0))
        if time.time() - last < ALARM_COOLDOWN:
            return
        self.store.set(f"alarm:{kind}", time.time())
        self.notify("⚠️ " + text)

    def over_budget(self):
        spent = self.store.month_cost_usd(self.month())
        if budget_exceeded(spent, self.budget_eur, self.usd_eur):
            if self.store.get("budget_alarm") != self.month():
                self.store.set("budget_alarm", self.month())
                self.notify(f"💸 KI-Budget von {self.budget_eur:.2f} € für {self.month()} erreicht. "
                            "Bewertung pausiert bis Monatsende.")
            return True
        return False

    # --- Ablauf ---
    def scrape(self):
        status, page = ka_scrape.fetch(self.search_url)
        if status != 200:
            self.failures += 1
            log.warning("Suche: HTTP %s (Fehler in Folge: %d)", status, self.failures)
            if status in (403, 429):
                self.alarm("blocked", f"Kleinanzeigen antwortet mit HTTP {status} – evtl. geblockt.")
            elif self.failures >= 6:
                self.alarm("down", f"Kleinanzeigen seit {self.failures} Versuchen nicht erreichbar.")
            return None
        ads = ka_scrape.parse_list(page)
        if not ads:
            self.failures += 1
            self.alarm("layout", "0 Anzeigen gefunden – hat sich das Seitenlayout geändert?")
            return None
        self.failures = 0
        self.store.set("last_scrape", time.time())
        return ads

    def ingest(self, ads):
        seeding = self.store.get("seeded") is None
        new = [a for a in reversed(ads) if not self.store.has(a["id"])]
        for a in new:
            self.store.insert(a, status="seeded" if seeding else "new")
        if seeding:
            self.store.set("seeded", time.time())
            log.info("Erststart: %d Anzeigen als bekannt markiert", len(new))
            self.notify(f"✅ Bot läuft für {self.place}. {len(new)} aktuelle Anzeigen als bekannt markiert – "
                        "ab jetzt melde ich neue interessante Sachen.", silent=True)
        return len(new)

    def enrich(self, ad):
        status, page = ka_scrape.fetch(ad["url"], retries=1)
        fields = {"enriched": 1}
        if status == 200:
            d = ka_scrape.parse_detail(page)
            if d["description"]:
                fields["description"] = d["description"]
            if d["image"]:
                fields["image"] = d["image"]
            fields["lat"], fields["lon"] = d["lat"], d["lon"]
        if fields.get("lat") is None:
            m = re.match(r"(\d{5})", ad.get("location") or "")
            if m:
                cached = self.store.get(f"geo:{m.group(1)}")
                if cached is None:
                    geo = ka_scrape.geocode_plz(m.group(1))
                    cached = f"{geo[0]},{geo[1]}" if geo else ""
                    self.store.set(f"geo:{m.group(1)}", cached)
                if cached:
                    fields["lat"], fields["lon"] = map(float, cached.split(","))
        self.store.update(ad["id"], **fields)
        time.sleep(DETAIL_PAUSE)
        return {**ad, **fields}

    def judge_ad(self, ad, interests, examples):
        """Ergänzt, bewertet und speichert eine Anzeige. Gibt das Dict zurück, bei Fehler None."""
        if not ad["enriched"]:
            ad = self.enrich(ad)
        if is_excluded(ad, self.exclude):
            score, reason = 0, "Ausschlussliste"
        else:
            try:
                score, reason, usages = self.judge.score(ad, interests, examples)
            except JudgeError as e:
                log.warning("Bewertung fehlgeschlagen (%s), nächster Versuch später", e)
                return None
            except Exception as e:  # z.B. 400/401 - nicht endlos wiederholen
                log.error("Bewertung %s: %s", ad["id"], e)
                self.alarm("judge", f"KI-Bewertung schlägt fehl: {e}")
                return None
            self.track(usages)
        self.store.update(ad["id"], score=score, reason=reason, status="judged",
                          judged_at=time.time())
        log.info("%2d/10 %s – %s (%s)", score, ad["title"], reason, ad["location"])
        return {**ad, "score": score, "reason": reason}

    def process_pending(self, limit=None):
        interests = INTERESTS.read_text() if INTERESTS.exists() else ""
        examples = self.store.feedback_examples()
        done = 0
        for ad in self.store.pending():
            if limit is not None and done >= limit:
                break
            if self.over_budget():
                break
            ad = self.judge_ad(ad, interests, examples)
            if ad is None:
                break
            if ad["score"] >= self.min_score:
                self.send_ad(ad)
            done += 1
        return done

    def send_ad(self, ad):
        if self.dry_run or not self.tg.chat_id:
            log.info("[send] %s", ad["title"])
            return
        silent = should_be_silent(now_local(), ad["score"])
        try:
            self.tg.send_ad(ad, silent, self.map_url)
            self.store.update(ad["id"], notified=1)
        except TelegramError as e:
            log.error("Senden fehlgeschlagen: %s", e)

    def maybe_digest(self):
        t = now_local()
        if not digest_due(t, self.store.get("last_digest")):
            return
        self.store.set("last_digest", t.date().isoformat())
        ads = self.store.digest_candidates(5, self.min_score - 1)
        if ads:
            self.notify(digest_text(ads, self.map_url), silent=True)

    def cycle(self):
        if self.store.get("paused") == "1":
            return
        ads = self.scrape()
        if ads:
            n = self.ingest(ads)
            if n:
                log.info("%d neue Anzeigen", n)
        self.process_pending()
        self.maybe_digest()

    def interval(self, base):
        return min(base * 2 ** max(0, self.failures - 1), 1800) if self.failures else base

    # --- Telegram-Eingang ---
    def handle_update(self, upd):
        if "callback_query" in upd:
            return self.handle_callback(upd["callback_query"])
        msg = upd.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat:
            return
        if not self.tg.chat_id:
            log.info("Nachricht von Chat-ID %s – in .env als TELEGRAM_CHAT_ID eintragen", chat)
            self.tg.call("sendMessage", chat_id=chat,
                         text=f"Deine Chat-ID ist {chat}. Trag sie als TELEGRAM_CHAT_ID in die .env ein "
                              "und starte den Bot neu.")
            return
        if chat != self.tg.chat_id:
            return
        self.handle_command(text)

    def handle_command(self, text):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/hilfe", "/help"):
            self.notify("<b>Befehle</b>\n" + "\n".join(f"/{c} – {html.escape(d)}" for c, d in COMMANDS)
                        + "\n\n<b>Buttons unter Meldungen</b>\n👍/👎 – Feedback, der Bot lernt daraus\n"
                        "✍️ Anfrage – Abhol-Nachricht formulieren\n🔗 – Anzeige öffnen")
        elif cmd == "/status":
            last = float(self.store.get("last_scrape", 0))
            midnight = now_local().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            n, sent = self.store.count_today(midnight)
            spent = self.store.month_cost_usd(self.month()) * self.usd_eur
            ago = f"vor {int((time.time() - last) // 60)} Min" if last else "noch nie"
            paused = " (⏸️ pausiert)" if self.store.get("paused") == "1" else ""
            self.notify(f"📊 Letzter Abruf: {ago}{paused}\nHeute: {n} neue Anzeigen, {sent} gemeldet\n"
                        f"KI-Kosten {self.month()}: {spent:.3f} € von {self.budget_eur:.2f} € (Listenpreis)\n\n"
                        + self.usage_text("Tokens heute", now_local().date().isoformat()))
        elif cmd == "/tokens":
            self.notify(self.usage_text("Heute", now_local().date().isoformat()) + "\n\n"
                        + self.usage_text(f"Monat {self.month()}", self.month()))
        elif cmd == "/interessen":
            if arg:
                INTERESTS.write_text(arg + "\n")
                self.notify("✅ Interessenprofil ersetzt.")
            else:
                self.notify("<b>Dein Profil</b>\n" + html.escape(INTERESTS.read_text()))
        elif cmd == "/mehr" and arg:
            with INTERESTS.open("a") as f:
                f.write("\n" + arg + "\n")
            self.notify("✅ Ergänzt.")
        elif cmd == "/karte":
            self.notify(f'🗺️ <a href="{self.map_url}">{self.map_url}</a>\n(nur im Heimnetz/VPN)'
                        if self.map_url else "Keine KA_MAP_URL konfiguriert.")
        elif cmd == "/pause":
            self.store.set("paused", "1")
            self.notify("⏸️ Pausiert. /weiter zum Fortsetzen.")
        elif cmd == "/weiter":
            self.store.set("paused", "0")
            self.notify("▶️ Läuft wieder.")
        elif cmd == "/test":
            self.notify("👋 Test ok.")

    def handle_callback(self, cq):
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
        if chat != self.tg.chat_id:
            return
        action, _, ad_id = (cq.get("data") or "").partition(":")
        ad = self.store.ad(ad_id)
        answer = ""
        if ad and action in ("up", "down"):
            vote = 1 if action == "up" else -1
            self.store.vote(ad_id, vote)
            answer = "Danke, gemerkt!"
            try:
                self.tg.call("editMessageReplyMarkup", chat_id=chat,
                             message_id=cq["message"]["message_id"],
                             reply_markup=ad_keyboard(ad, voted=vote, map_url=self.map_url))
            except TelegramError:
                pass
        elif ad and action == "ask":
            self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="Schreibe…")
            try:
                text, usages = self.judge.inquiry(ad)
                self.track(usages, kind="inquiry")
                self.tg.send(f"✍️ <b>Vorschlag</b> (zum Kopieren antippen):\n\n<code>{html.escape(text)}</code>",
                             reply_parameters={"message_id": cq["message"]["message_id"]})
            except Exception as e:
                self.notify(f"Konnte keinen Text erzeugen: {e}")
            return
        try:
            self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=answer)
        except TelegramError:
            pass

    def poll_forever(self):
        offset = None
        while True:
            try:
                params = {"timeout": 50, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                for upd in self.tg.call("getUpdates", http_timeout=65, **params):
                    offset = upd["update_id"] + 1
                    try:
                        self.handle_update(upd)
                    except Exception:
                        log.exception("Update-Verarbeitung")
            except TelegramError as e:
                log.warning("getUpdates: %s", e)
                time.sleep(10)


def main():
    load_env(HERE / ".env")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="nichts senden, In-Memory-DB")
    ap.add_argument("--once", action="store_true", help="nur einen Durchlauf")
    ap.add_argument("--limit", type=int, default=5, help="max. Bewertungen im Dry-Run")
    ap.add_argument("--send-top", type=int, metavar="N",
                    help="aktuelle Anzeigen bewerten und die besten N an Telegram schicken")
    ap.add_argument("--test-telegram", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    env = os.environ
    tg = Telegram(env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", ""))
    if args.test_telegram:
        tg.send("👋 Kleinanzeigen-Bot: Test ok.")
        return

    query = env.get("KA_LOCATION", "").strip()
    if not query:
        sys.exit("KA_LOCATION fehlt in der .env, z. B. KA_LOCATION=Köln oder KA_LOCATION=50672")
    if not args.dry_run and not env.get("TELEGRAM_BOT_TOKEN"):
        sys.exit("TELEGRAM_BOT_TOKEN fehlt in der .env")
    if not INTERESTS.exists():
        shutil.copy(HERE / "interests.example.md", INTERESTS)
        log.warning("interests.md aus der Vorlage angelegt – bitte an deine Interessen anpassen.")
    store = Store(":memory:" if args.dry_run else str(HERE / "ka.db"))
    try:
        judge = make_judge(env.get("KA_JUDGE_BACKEND", "cli"), store=store)
        search = configure_search(store, query, int(env.get("KA_RADIUS_KM", 5)))
    except (JudgeError, ka_scrape.LocationError) as e:
        sys.exit(str(e))
    except ValueError:
        sys.exit("KA_RADIUS_KM muss eine Zahl sein.")
    place = search["label"].split(" - ")[0]
    log.info("Suche: %s, %d km", search["label"], search["radius_km"])
    bot = Bot(store, tg, judge, min_score=int(env.get("KA_MIN_SCORE", 7)),
              budget_eur=float(env.get("KA_BUDGET_EUR", 5)),
              usd_eur=float(env.get("KA_USD_EUR", 0.92)),
              map_url=env.get("KA_MAP_URL", ""), dry_run=args.dry_run,
              search_url=search["url"], place=place,
              exclude=exclude_pattern(env.get("KA_EXCLUDE", "")))

    if args.dry_run:
        ads = bot.scrape() or []
        print(f"{len(ads)} Anzeigen geparst")
        for a in ads:
            store.insert(a)
        store.set("seeded", 1)
        bot.process_pending(limit=args.limit)
        print(re.sub(r"<[^>]+>", "", bot.usage_text("Verbrauch", now_local().date().isoformat())))
        return
    if args.send_top:
        # Bewertet die 8 neuesten Anzeigen (auch bereits bekannte) und schickt die besten N
        ads = bot.scrape() or []
        interests = INTERESTS.read_text() if INTERESTS.exists() else ""
        examples = store.feedback_examples()
        scored = []
        for a in ads[:8]:
            if not store.has(a["id"]):
                store.insert(a, status="seeded")
            row = store.ad(a["id"])
            if row["score"] is None:
                row = bot.judge_ad(row, interests, examples)
            if row is not None:
                scored.append(row)
        for ad in sorted(scored, key=lambda a: a["score"], reverse=True)[:args.send_top]:
            bot.send_ad(ad)
        return

    if env.get("KA_WEB_PORT"):
        import ka_web
        ka_web.start(store, env.get("KA_WEB_HOST", "0.0.0.0"), int(env["KA_WEB_PORT"]),
                     site={"label": place, "radius_km": search["radius_km"], "center": search["center"]})
        log.info("Karte auf Port %s", env["KA_WEB_PORT"])
    try:
        tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
    except TelegramError as e:
        log.warning("setMyCommands: %s", e)
    threading.Thread(target=bot.poll_forever, daemon=True, name="telegram").start()
    if not tg.chat_id:
        log.warning("TELEGRAM_CHAT_ID leer – schreib dem Bot, er antwortet mit deiner Chat-ID.")
        while True:
            time.sleep(3600)

    base = int(env.get("KA_INTERVAL", 300))
    while True:
        try:
            bot.cycle()
        except Exception:
            log.exception("Durchlauf fehlgeschlagen")
        if args.once:
            break
        time.sleep(bot.interval(base))


if __name__ == "__main__":
    main()
