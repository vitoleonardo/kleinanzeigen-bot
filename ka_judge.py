"""KI-Bewertung der Anzeigen mit Claude.

Zwei Backends:
- "cli" (Standard): `claude -p` mit dem Claude-Abo, ohne API-Kosten
- "api": Anthropic SDK mit ANTHROPIC_API_KEY
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

MODEL = "claude-haiku-5-5"
# USD pro Token laut Preisliste (Prompts < 100K Tokens) - nur für das API-Backend;
# die CLI liefert ihre Kosten selbst mit.
PRICE_IN = 0.10 / 1_000_000
PRICE_OUT = 0.50 / 1_000_000
PRICE_CACHE_WRITE = 0.125 / 1_000_000
PRICE_CACHE_READ = 0.01 / 1_000_000

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-10"},
        "reason": {"type": "string", "description": "Ein kurzer Satz auf Deutsch"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}

SYSTEM = """Du bewertest kostenlose Kleinanzeigen ("Zu verschenken") für eine Person.
Entscheide, wie interessant die Anzeige für diese Person ist, anhand ihres Interessenprofils.

Skala:
- 0-2: uninteressant, Sperrmüll, kaputt ohne Reiz, oder vom Profil ausgeschlossen
- 3-4: eher nicht
- 5-6: könnte interessant sein, nichts Besonderes
- 7-8: passt gut zum Profil, lohnt sich abzuholen
- 9-10: echtes Highlight (wertig, gesucht, Marke/Design, sofort holen)

Berücksichtige Zustand, Marke, Nützlichkeit und ob das Foto zur Beschreibung passt.
"reason" ist EIN kurzer, konkreter Satz auf Deutsch (max. 20 Wörter), warum die Anzeige
(nicht) interessant ist."""

INQUIRY_SYSTEM = ("Schreibe eine kurze, freundliche Nachricht auf Deutsch an jemanden, der etwas "
                  "auf Kleinanzeigen verschenkt. Die Person möchte den Gegenstand gern abholen, "
                  "ist bei der Abholung flexibel. Duzen ist ok, 2-4 Sätze, kein "
                  "Betreff, keine Platzhalter wie [Name]. Gib nur den Nachrichtentext aus.")


class JudgeError(Exception):
    """Vorübergehender Fehler - Anzeige im nächsten Lauf erneut versuchen."""


def exclude_pattern(words):
    """Baut aus „baby*, kinderwagen“ (KA_EXCLUDE) einen Filter; * steht für beliebige Wortenden."""
    terms = [re.escape(w.strip()).replace(r"\*", r"\w*") for w in words.split(",") if w.strip()]
    return re.compile(r"\b(?:" + "|".join(terms) + r")(?!\w)", re.I) if terms else None


def is_excluded(ad, pattern):
    return bool(pattern and pattern.search(f"{ad.get('title', '')} {ad.get('description', '')}"))


def usage_record(inp=0, out=0, cache_read=0, cache_write=0, thinking=0, cost=None):
    """Einheitlicher Token-Datensatz für beide Backends."""
    if cost is None:
        cost = (inp * PRICE_IN + out * PRICE_OUT
                + cache_write * PRICE_CACHE_WRITE + cache_read * PRICE_CACHE_READ)
    return {"input": inp, "output": out, "cache_read": cache_read,
            "cache_write": cache_write, "thinking": thinking, "cost_usd": cost}


def _examples_text(examples):
    if not examples:
        return ""
    lines = [f"{'👍' if e['vote'] > 0 else '👎'} {e['title']} ({e['location']})" for e in examples]
    return ("\n\nFrühere Rückmeldungen der Person (👍 = fand ich gut, 👎 = nicht interessant). "
            "Richte dich danach:\n" + "\n".join(lines))


def _ad_text(ad):
    return (f"Titel: {ad['title']}\nOrt: {ad['location']} {ad.get('distance') or ''}\n"
            f"Beschreibung:\n{(ad.get('description') or '')[:3000]}")


def _parse_score(text):
    try:
        data = json.loads(text)
        return max(0, min(10, int(data["score"]))), str(data["reason"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise JudgeError(f"Ungültige Antwort: {text[:200]}") from e


def _media_type(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"GIF8":
        return "image/gif"
    return None


def download_image(url, max_bytes=4_000_000):
    """Lädt das Anzeigenbild. Gibt (media_type, base64) oder None zurück."""
    if not url:
        return None
    try:
        p = subprocess.run(["curl", "-s", "-L", "-m", "20", "--max-filesize", str(max_bytes), url],
                           capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    mt = _media_type(p.stdout) if p.returncode == 0 else None
    return (mt, base64.standard_b64encode(p.stdout).decode()) if mt else None


class CliJudge:
    """Bewertung über `claude -p` (Claude-Abo)."""

    def __init__(self, claude_bin=None, timeout=180):
        self.bin = (claude_bin or shutil.which("claude")
                    or str(Path.home() / ".npm-global/bin/claude"))
        self.timeout = timeout
        self.workdir = tempfile.mkdtemp(prefix="ka-claude-")

    def _run(self, system, content, schema=None):
        cmd = [self.bin, "-p", "--model", MODEL, "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--tools", "",
               "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
               "--disable-slash-commands", "--system-prompt", system]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY",)}
        try:
            p = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True,
                               timeout=self.timeout, cwd=self.workdir, env=env)
        except subprocess.TimeoutExpired as e:
            raise JudgeError("claude CLI: Timeout") from e
        except OSError as e:
            raise JudgeError(f"claude CLI nicht startbar: {e}") from e
        result = None
        for line in p.stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                result = ev
        if result is None:
            raise JudgeError(f"claude CLI ohne Ergebnis (exit {p.returncode}): "
                             f"{(p.stderr or p.stdout)[-300:]}")
        if result.get("is_error"):
            raise JudgeError(f"claude CLI: {result.get('subtype')} {str(result.get('result'))[:200]}")
        u = result.get("usage") or {}
        usage = usage_record(
            inp=u.get("input_tokens", 0), out=u.get("output_tokens", 0),
            cache_read=u.get("cache_read_input_tokens", 0),
            cache_write=u.get("cache_creation_input_tokens", 0),
            thinking=(u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
            cost=result.get("total_cost_usd") or 0.0)
        out = result.get("structured_output")
        text = json.dumps(out) if out is not None else (result.get("result") or "")
        return text, usage

    def score(self, ad, interests, examples):
        """Gibt (score, reason, [usage]) zurück."""
        system = SYSTEM + "\n\nInteressenprofil:\n" + interests.strip() + _examples_text(examples)
        content = [{"type": "text", "text": _ad_text(ad)}]
        img = ad["_score_image"] if "_score_image" in ad else download_image(ad.get("image"))
        if img:
            content.insert(0, {"type": "image",
                               "source": {"type": "base64", "media_type": img[0], "data": img[1]}})
        text, usage = self._run(system, content, SCORE_SCHEMA)
        score, reason = _parse_score(text)
        return score, reason, [usage]

    def inquiry(self, ad):
        text, usage = self._run(INQUIRY_SYSTEM, [{"type": "text", "text": _ad_text(ad)}])
        return text.strip(), [usage]


class ApiJudge:
    """Bewertung über die Anthropic API (ANTHROPIC_API_KEY)."""

    def __init__(self, client=None):
        import anthropic
        self.anthropic = anthropic
        self.client = client or anthropic.Anthropic()

    def _create(self, **kwargs):
        a = self.anthropic
        try:
            return self.client.messages.create(model=MODEL, **kwargs)
        except a.APIConnectionError as e:
            raise JudgeError(f"Verbindung: {e}") from e
        except a.RateLimitError as e:
            raise JudgeError("Rate limit") from e
        except a.InternalServerError as e:
            raise JudgeError(f"Serverfehler {e.status_code}") from e

    @staticmethod
    def _usage(resp):
        u = resp.usage
        return usage_record(inp=u.input_tokens, out=u.output_tokens,
                            cache_read=u.cache_read_input_tokens or 0,
                            cache_write=u.cache_creation_input_tokens or 0)

    def score(self, ad, interests, examples):
        system = SYSTEM + "\n\nInteressenprofil:\n" + interests.strip() + _examples_text(examples)
        content = [{"type": "text", "text": _ad_text(ad)}]
        if ad.get("image"):
            content.insert(0, {"type": "image", "source": {"type": "url", "url": ad["image"]}})
        params = dict(max_tokens=2048, system=system,
                      output_config={"effort": "low",
                                     "format": {"type": "json_schema", "schema": SCORE_SCHEMA}})
        try:
            resp = self._create(messages=[{"role": "user", "content": content}], **params)
        except self.anthropic.BadRequestError:
            # Meist ist das Bild nicht abrufbar - ohne Bild erneut versuchen
            if len(content) == 1:
                raise
            resp = self._create(messages=[{"role": "user", "content": content[1:]}], **params)
        if resp.stop_reason == "refusal":
            return 0, "Von der KI abgelehnt", [self._usage(resp)]
        text = next((b.text for b in resp.content if b.type == "text"), "")
        score, reason = _parse_score(text)
        return score, reason, [self._usage(resp)]

    def inquiry(self, ad):
        resp = self._create(max_tokens=2048, output_config={"effort": "low"}, system=INQUIRY_SYSTEM,
                            messages=[{"role": "user", "content": _ad_text(ad)}])
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        return text, [self._usage(resp)]


class CachedJudge:
    """CLI-Ergebnisse nur bei identischem Inhalt, Foto und Bewertungskontext wiederverwenden."""

    def __init__(self, judge, store):
        self.judge, self.store = judge, store

    def score(self, ad, interests, examples):
        image = download_image(ad.get("image"))
        prepared = {**ad, "_score_image": image}
        # Kurze Standardtexte sind kein verlässlicher Hinweis auf dieselbe Sache.
        # Fehlgeschlagene Bildabrufe dürfen keine dauerhafte Bewertung ohne Foto ergeben.
        eligible = bool(ad.get("title") and len((ad.get("description") or "").strip()) >= 40
                        and (not ad.get("image") or image))
        fingerprint = None
        if eligible:
            payload = [1, type(self.judge).__name__, MODEL, SYSTEM, SCORE_SCHEMA,
                       interests.strip(), _examples_text(examples), _ad_text(ad),
                       # Die vollen Texte verhindern Kollisionen nach Prompt-Kürzung.
                       ad.get("description"), image]
            fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
                                                     sort_keys=True).encode()).hexdigest()
            cached = self.store.cached_score(fingerprint)
            if cached:
                day = dt.datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
                self.store.record_score_cache_hit(day)
                return cached["score"], cached["reason"], []
        result = self.judge.score(prepared, interests, examples)
        if fingerprint:
            self.store.cache_score(fingerprint, result[0], result[1])
        return result

    def inquiry(self, ad):
        return self.judge.inquiry(ad)


def make_judge(backend="cli", store=None, **kw):
    if backend == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise JudgeError("KA_JUDGE_BACKEND=api braucht ANTHROPIC_API_KEY in der .env.")
        try:
            return ApiJudge()
        except ImportError as e:
            raise JudgeError("Anthropic SDK fehlt: pip install -r requirements.txt") from e
    if backend != "cli":
        raise JudgeError(f"KA_JUDGE_BACKEND muss cli oder api sein, nicht „{backend}“.")
    judge = CliJudge(**kw)
    if not shutil.which(judge.bin):
        raise JudgeError("claude CLI nicht gefunden: Claude Code installieren und anmelden "
                         "oder KA_JUDGE_BACKEND=api setzen.")
    return CachedJudge(judge, store) if store is not None else judge
