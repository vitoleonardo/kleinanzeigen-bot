"""SQLite-State: Anzeigen, Feedback, API-Kosten, Meta-Werte."""
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS ads (
    id TEXT PRIMARY KEY,
    url TEXT, title TEXT, description TEXT, image TEXT,
    location TEXT, distance TEXT, posted TEXT,
    lat REAL, lon REAL, enriched INTEGER DEFAULT 0,
    score INTEGER, reason TEXT,
    -- new = noch zu bewerten, judged = bewertet, seeded = beim Erststart übersprungen, expired = zu alt oder aus einer früheren Suche
    status TEXT NOT NULL DEFAULT 'new',
    notified INTEGER DEFAULT 0,
    first_seen REAL, judged_at REAL
);
CREATE TABLE IF NOT EXISTS feedback (ad_id TEXT PRIMARY KEY, vote INTEGER, ts REAL);
-- Token-Verbrauch pro Tag und Zweck (score = Bewertung, inquiry = Anfrage-Text)
CREATE TABLE IF NOT EXISTS usage (
    day TEXT, kind TEXT, calls INTEGER DEFAULT 0,
    input INTEGER DEFAULT 0, output INTEGER DEFAULT 0,
    cache_read INTEGER DEFAULT 0, cache_write INTEGER DEFAULT 0,
    thinking INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
    PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS score_cache (
    fingerprint TEXT PRIMARY KEY, score INTEGER, reason TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS score_cache_hits (day TEXT PRIMARY KEY, hits INTEGER DEFAULT 0);
"""


class Store:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def _q(self, sql, args=(), one=False):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            rows = cur.fetchall()
        rows = [dict(r) for r in rows]
        return (rows[0] if rows else None) if one else rows

    # --- meta ---
    def get(self, key, default=None):
        r = self._q("SELECT value FROM meta WHERE key=?", (key,), one=True)
        return r["value"] if r else default

    def set(self, key, value):
        self._q("INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def start_new_search(self):
        """Alte Anzeigen archivieren und neu seeden; Bewertungen und Feedback bleiben erhalten."""
        self._q("UPDATE ads SET status='expired' WHERE status!='expired'")
        self._q("DELETE FROM meta WHERE key='seeded'")

    # --- ads ---
    def has(self, ad_id):
        return self._q("SELECT 1 FROM ads WHERE id=?", (ad_id,), one=True) is not None

    def insert(self, ad, status="new"):
        self._q("INSERT OR IGNORE INTO ads(id,url,title,description,image,location,distance,"
                "posted,status,first_seen) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ad["id"], ad["url"], ad["title"], ad["description"], ad["image"],
                 ad["location"], ad["distance"], ad["posted"], status, time.time()))

    def ad(self, ad_id):
        return self._q("SELECT * FROM ads WHERE id=?", (ad_id,), one=True)

    def update(self, ad_id, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self._q(f"UPDATE ads SET {cols} WHERE id=?", (*fields.values(), ad_id))

    def pending(self, max_age_h=24):
        cutoff = time.time() - max_age_h * 3600
        self._q("UPDATE ads SET status='expired' WHERE status='new' AND first_seen<?", (cutoff,))
        return self._q("SELECT * FROM ads WHERE status='new' ORDER BY first_seen, id")

    def map_ads(self, days=3):
        cutoff = time.time() - days * 86400
        return self._q("SELECT id,url,title,description,image,location,distance,posted,lat,lon,score,reason,"
                       "first_seen, f.vote AS feedback_vote FROM ads "
                       "LEFT JOIN feedback f ON f.ad_id=ads.id WHERE first_seen>=? "
                       "AND status IN ('judged','seeded') ORDER BY first_seen DESC", (cutoff,))

    def digest_candidates(self, lo, hi, hours=24):
        cutoff = time.time() - hours * 3600
        return self._q("SELECT * FROM ads WHERE status='judged' AND notified=0 AND judged_at>=? "
                       "AND score BETWEEN ? AND ? ORDER BY score DESC, judged_at DESC",
                       (cutoff, lo, hi))

    def count_today(self, since_ts):
        r = self._q("SELECT COUNT(*) AS n, SUM(notified) AS sent FROM ads "
                    "WHERE first_seen>=? AND status!='seeded'", (since_ts,), one=True)
        return r["n"] or 0, r["sent"] or 0

    # --- feedback ---
    def vote(self, ad_id, vote):
        self._q("INSERT INTO feedback(ad_id,vote,ts) VALUES(?,?,?) "
                "ON CONFLICT(ad_id) DO UPDATE SET vote=excluded.vote, ts=excluded.ts",
                (ad_id, vote, time.time()))

    def clear_vote(self, ad_id):
        self._q("DELETE FROM feedback WHERE ad_id=?", (ad_id,))

    def feedback_examples(self, limit=30):
        return self._q("SELECT f.vote, a.title, a.location, a.score FROM feedback f "
                       "JOIN ads a ON a.id=f.ad_id ORDER BY f.ts DESC LIMIT ?", (limit,))

    # --- usage ---
    USAGE_COLS = ("input", "output", "cache_read", "cache_write", "thinking", "cost_usd")

    def add_usage(self, day, kind, u):
        vals = [u[c] for c in self.USAGE_COLS]
        upd = ", ".join(f"{c}={c}+excluded.{c}" for c in self.USAGE_COLS)
        self._q(f"INSERT INTO usage(day,kind,calls,{','.join(self.USAGE_COLS)}) "
                f"VALUES(?,?,1,?,?,?,?,?,?) ON CONFLICT(day,kind) DO UPDATE SET "
                f"calls=calls+1, {upd}", (day, kind, *vals))

    def usage_sum(self, prefix, kind=None):
        """Summen für einen Tag ('2026-10-19') oder Monat ('2026-10')."""
        cols = ", ".join(f"COALESCE(SUM({c}),0) AS {c}" for c in ("calls",) + self.USAGE_COLS)
        condition = " AND kind=?" if kind else ""
        args = (prefix + "%", kind) if kind else (prefix + "%",)
        return self._q(f"SELECT {cols} FROM usage WHERE day LIKE ?{condition}", args, one=True)

    def cached_score(self, fingerprint):
        return self._q("SELECT score,reason FROM score_cache WHERE fingerprint=? AND created_at>=?",
                       (fingerprint, time.time() - 7 * 86400), one=True)

    def cache_score(self, fingerprint, score, reason):
        self._q("DELETE FROM score_cache WHERE created_at<?", (time.time() - 7 * 86400,))
        self._q("INSERT OR REPLACE INTO score_cache VALUES(?,?,?,?)",
                (fingerprint, score, reason, time.time()))

    def record_score_cache_hit(self, day):
        self._q("INSERT INTO score_cache_hits(day,hits) VALUES(?,1) "
                "ON CONFLICT(day) DO UPDATE SET hits=hits+1", (day,))

    def cache_hits(self, prefix):
        return self._q("SELECT COALESCE(SUM(hits),0) AS n FROM score_cache_hits WHERE day LIKE ?",
                       (prefix + "%",), one=True)["n"]

    def month_cost_usd(self, month):
        return self.usage_sum(month)["cost_usd"]
