"""Meridian Daily - Streamlit newspaper (production skeleton).

Layers: Policy/Security (hidden rules) -> Repository (PostgreSQL+pgvector) ->
services (Shortener, Vectorizer, Translator, Cart, Allocation, Publisher) -> NewsUI.
Run:  streamlit run app.py   (env vars: see Settings)
"""
from __future__ import annotations

import base64, contextvars, csv, functools, hashlib, hmac, html, io, json, os, re, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


# ───────────────────────── configuration ─────────────────────────
@dataclass(frozen=True)
class Settings:
    dsn: str = os.getenv("DATABASE_URL", "postgresql://news:news@localhost:5432/news")
    redis_url: str = os.getenv("REDIS_URL", "")
    public_url: str = os.getenv("PUBLIC_URL", "http://localhost:8501").rstrip("/")
    pool_max: int = int(os.getenv("DB_POOL_MAX", "20"))
    # only a real AdSense id (ca-pub-...) turns ads on; blank or any placeholder keeps them off
    adsense_client: str = os.getenv("ADSENSE_CLIENT", "") if os.getenv("ADSENSE_CLIENT", "").startswith("ca-pub-") else ""
    libre_url: str = os.getenv("LIBRETRANSLATE_URL", "")       # self-hosted = no third party
    font_css_url: str = os.getenv("FONT_CSS_URL", "")          # self-hosted @font-face css
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "1") == "1"
    editor_invite: str = os.getenv("EDITOR_INVITE_CODE", "")   # secret code that creates editor accounts
    rate: float = float(os.getenv("RATE_PER_STORY", "20"))     # default fee per published story
    currency: str = os.getenv("CURRENCY", "BDT")
    tz: str = os.getenv("NEWSROOM_TZ", "Asia/Dhaka")           # timezone editors schedule in
    # one database per zone; a blank zone falls back to DATABASE_URL (single-database mode)
    dsn_white: str = os.getenv("DATABASE_URL_WHITE", "")          # published news, editorial, finance
    dsn_blue: str = os.getenv("DATABASE_URL_BLUE", "")            # employees / HR (reserved)
    dsn_reporters: str = os.getenv("DATABASE_URL_REPORTERS", "")  # accounts, submissions, billing
    dsn_community: str = os.getenv("DATABASE_URL_COMMUNITY", "")  # favourites, clippings, polls, votes
    google: bool = bool(os.getenv("GOOGLE_CLIENT_ID"))          # Google sign-in on when a client id is set
    # footer content (all optional; blank items are simply not shown)
    footer_company: str = os.getenv("FOOTER_COMPANY", "Meridian Daily")
    footer_since: str = os.getenv("FOOTER_SINCE", "")
    footer_region: str = os.getenv("FOOTER_REGION", "")
    footer_address: str = os.getenv("FOOTER_ADDRESS", "")
    footer_phone: str = os.getenv("FOOTER_PHONE", "")            # comma separated
    footer_factcheck: str = os.getenv("FOOTER_FACTCHECK", "")    # fake-news checker line or number
    footer_emails: str = os.getenv("FOOTER_EMAILS", "")          # comma separated
    footer_editor: str = os.getenv("FOOTER_EDITOR", "")
    footer_publisher: str = os.getenv("FOOTER_PUBLISHER", "")
    footer_social: str = os.getenv("FOOTER_SOCIAL", "")          # JSON: {"facebook":"https://...", "x":"https://..."}
    desks: dict = field(default_factory=lambda: json.loads(os.getenv("DESK_KEYS", "{}")))


class PolicyError(Exception):
    pass


STATUS = {"submitted": "Waiting for review", "in_review": "Being reviewed", "changes_requested": "Changes requested",
          "scheduled": "Scheduled", "published": "Published", "rejected": "Not accepted"}


# ───────────────── encapsulated business rules / organisational policy ─────────────────
class Policy:
    """All editorial/organisational rules live here; callers never see the numbers."""
    __CATEGORIES = ("National", "World", "Business", "Sports", "Tech", "Culture", "Opinion")
    __MAX_IMAGE = 3 * 1024 * 1024
    __IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
    __QUOTA = {"reader": (50, 10, 120), "member": (200, 25, 300)}  # favourites, check-ins, req/min

    @property
    def categories(self) -> tuple:
        return self.__CATEGORIES

    def quota(self, tier: str) -> tuple:
        return self.__QUOTA.get(tier, self.__QUOTA["reader"])

    def check_article(self, a: dict) -> dict:
        for k in ("slug", "category", "title", "body"):
            if not str(a.get(k, "")).strip():
                raise PolicyError(f"Missing field: {k}")
        if a["category"] not in self.__CATEGORIES:
            raise PolicyError("Unknown category")
        if not re.fullmatch(r"[a-z0-9-]{3,80}", a["slug"]):
            raise PolicyError("Slug must be lowercase letters, digits, hyphens")
        if a.get("sponsored") and not a.get("sponsor"):
            raise PolicyError("Sponsored content must name its sponsor")
        if a["category"] == "Opinion" and not a.get("author"):
            raise PolicyError("Opinion pieces need a named author")
        return a

    def check_image(self, img: bytes) -> None:
        if len(img) > self.__MAX_IMAGE:
            raise PolicyError("Image too large")
        try:
            with Image.open(io.BytesIO(img)) as im:
                if im.format not in self.__IMAGE_FORMATS:
                    raise PolicyError("Unsupported image format")
                im.verify()
        except PolicyError:
            raise
        except Exception:
            raise PolicyError("Corrupt image")

    __CHECKS = (("sources", "Key claims confirmed by 2+ independent sources"),
                ("facts", "Names, numbers, dates and quotes checked"),
                ("media", "Photo/video provenance checked"),
                ("reply", "Right of reply offered for any allegation"),
                ("legal", "No legal, privacy or safety concern"))
    __MAX_PENDING = 10

    __INCOME = ("Sponsorship", "Advertising", "Subscriptions", "Grants", "Other income")
    __EXPENSE = ("Salaries", "Hosting & tools", "Marketing", "Office", "Other expense")   # reporter fees are derived
    __DEPTS = ("Editorial", "Production", "Distribution", "Finance", "HR", "IT", "Sales", "Admin")
    __ATT = ("present", "late", "absent", "leave")

    @property
    def income_accounts(self) -> tuple: return self.__INCOME

    @property
    def expense_accounts(self) -> tuple: return self.__EXPENSE

    @property
    def departments(self) -> tuple: return self.__DEPTS

    @property
    def attendance_states(self) -> tuple: return self.__ATT

    collars = ("white", "blue")

    def budget_lines(self) -> list:
        return ([("Income", a) for a in self.__INCOME] + [("Expense", "Reporter fees")] + [("Expense", a) for a in self.__EXPENSE])

    @staticmethod
    def performance_index(attendance: float, score: float) -> float:
        return round(100 * (0.4 * attendance + 0.6 * score / 5), 1)

    __POLL_PASS, __POLL_QUORUM = 0.85, 3

    @property
    def poll_rules(self) -> tuple:
        return self.__POLL_PASS, self.__POLL_QUORUM

    def poll_passed(self, yes: int, no: int) -> bool:
        votes = yes + no                       # "not interested" is an abstention
        return votes >= self.__POLL_QUORUM and yes / votes >= self.__POLL_PASS

    @property
    def checks(self) -> tuple:
        return self.__CHECKS

    def can_publish(self, ticked: dict) -> bool:
        return all(ticked.get(k) for k, _ in self.__CHECKS)

    def can_submit(self, pending: int) -> bool:
        return pending < self.__MAX_PENDING

    __GAP = int(os.getenv("PUBLISH_GAP_MIN", "15"))        # minimum minutes between go-lives (rate limit)
    __SLA = int(os.getenv("REVIEW_SLA_MIN", "60"))         # review milestone: minutes before a story is overdue
    __PER_HOUR = int(os.getenv("SUBMITS_PER_HOUR", "5"))   # stories one reporter may submit per hour

    @property
    def gap(self) -> int:
        return self.__GAP

    @property
    def sla(self) -> int:
        return self.__SLA

    def can_submit_now(self, last_hour: int) -> bool:
        return last_hour < self.__PER_HOUR

    @staticmethod
    def password_ok(pw: str) -> bool:
        return len(pw) >= 10

    @staticmethod
    def slugify(title: str) -> str:
        return (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "story") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def scope_ok(scopes: list, category: str) -> bool:
        return "publish:*" in scopes or f"publish:{category.lower()}" in scopes


# ───────────────────────── cache / shared state ─────────────────────────
class KV:
    """Redis when REDIS_URL is set (shared by all pods), else bounded in-process TTL map."""
    def __init__(self, url: str):
        self._r, self._d, self._lock = None, {}, threading.RLock()
        if url:
            import redis
            self._r = redis.Redis.from_url(url, socket_timeout=1)

    def get(self, k):
        if self._r:
            try:
                v = self._r.get(k)
                return v.decode() if v else None
            except Exception:
                return None
        with self._lock:
            v = self._d.get(k)
            return v[0] if v and v[1] > time.time() else None

    def set(self, k, v, ttl=3600):
        if self._r:
            try:
                self._r.set(k, v, ex=ttl)
            except Exception:
                pass
            return
        with self._lock:
            if len(self._d) > 50_000:
                now = time.time()
                self._d = {a: b for a, b in self._d.items() if b[1] > now}
            self._d[k] = (v, time.time() + ttl)

    def incr(self, k, ttl=60) -> int:
        if self._r:
            try:
                p = self._r.pipeline()
                p.incr(k)
                p.expire(k, ttl, nx=True)
                return p.execute()[0]
            except Exception:
                return 0
        with self._lock:
            n = int(self.get(k) or 0) + 1
            old = self._d.get(k)
            self._d[k] = (str(n), old[1] if old and old[1] > time.time() else time.time() + ttl)
            return n


# ───────────────────────── persistence ─────────────────────────
SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS articles(
  id BIGSERIAL PRIMARY KEY, short_code TEXT UNIQUE, slug TEXT UNIQUE NOT NULL,
  category TEXT NOT NULL, title TEXT NOT NULL, summary TEXT, body TEXT NOT NULL,
  lang TEXT DEFAULT 'en', author TEXT, sponsored BOOLEAN DEFAULT FALSE, sponsor TEXT,
  image BYTEA, image_sha256 TEXT, version INT DEFAULT 1,
  published_at TIMESTAMPTZ DEFAULT now(), embedding vector(256),
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig,
      title || ' ' || coalesce(summary,'') || ' ' || body)) STORED);
CREATE INDEX IF NOT EXISTS ix_art_cat ON articles(category, published_at DESC);
CREATE INDEX IF NOT EXISTS ix_art_tsv ON articles USING GIN(tsv);
CREATE INDEX IF NOT EXISTS ix_art_vec ON articles USING hnsw(embedding vector_cosine_ops);
CREATE TABLE IF NOT EXISTS favorites(
  user_id TEXT, article_id BIGINT,
  state TEXT NOT NULL DEFAULT 'saved' CHECK (state IN ('saved','checked_in','checked_out')),
  updated_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(user_id, article_id));
CREATE TABLE IF NOT EXISTS clippings(
  id BIGSERIAL PRIMARY KEY, user_id TEXT, article_id BIGINT,
  note TEXT, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_clip_user ON clippings(user_id);
CREATE TABLE IF NOT EXISTS used_nonces(nonce TEXT PRIMARY KEY, seen_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS users(
  id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'reporter' CHECK (role IN ('reporter','editor')),
  pw_hash TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS submissions(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id), desk TEXT,
  slug TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, summary TEXT, body TEXT NOT NULL, author TEXT,
  image BYTEA, image_sha256 TEXT,
  status TEXT NOT NULL DEFAULT 'submitted' CHECK (status IN ('submitted','in_review','changes_requested','published','rejected')),
  checks JSONB DEFAULT '{}', editor_note TEXT, reviewer_id BIGINT, fee NUMERIC(12,2) DEFAULT 0, article_id BIGINT,
  created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_sub_status ON submissions(status, created_at);
CREATE INDEX IF NOT EXISTS ix_sub_user ON submissions(user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS transactions(
  id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES users(id), submission_id BIGINT,
  kind TEXT NOT NULL CHECK (kind IN ('earning','payout','adjustment')), amount NUMERIC(12,2) NOT NULL,
  note TEXT, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_tx_user ON transactions(user_id, created_at DESC);
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS publish_at TIMESTAMPTZ;
ALTER TABLE submissions DROP CONSTRAINT IF EXISTS submissions_status_check;
ALTER TABLE submissions ADD CONSTRAINT submissions_status_check CHECK (status IN
  ('submitted','in_review','changes_requested','scheduled','published','rejected'));
CREATE INDEX IF NOT EXISTS ix_sub_due ON submissions(status, publish_at);
CREATE TABLE IF NOT EXISTS submission_events(
  id BIGSERIAL PRIMARY KEY, submission_id BIGINT NOT NULL, event TEXT NOT NULL, actor TEXT, detail TEXT,
  at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_ev_sub ON submission_events(submission_id, at);
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('reporter','editor','sponsor'));
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS sponsored BOOLEAN DEFAULT FALSE;
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS sponsor TEXT;
CREATE TABLE IF NOT EXISTS edit_proposals(
  id BIGSERIAL PRIMARY KEY, submission_id BIGINT NOT NULL, proposer_id BIGINT NOT NULL,
  title TEXT, summary TEXT, body TEXT, reason TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','applied','rejected')),
  created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_prop_status ON edit_proposals(status, created_at DESC);
CREATE TABLE IF NOT EXISTS edit_votes(
  proposal_id BIGINT REFERENCES edit_proposals(id) ON DELETE CASCADE, voter_id BIGINT NOT NULL,
  vote TEXT NOT NULL CHECK (vote IN ('yes','no','not_interested')), comment TEXT,
  created_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(proposal_id, voter_id));
CREATE TABLE IF NOT EXISTS ledger(
  id BIGSERIAL PRIMARY KEY, entry_date DATE NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('income','expense','capital','drawing')), account TEXT NOT NULL,
  amount NUMERIC(14,2) NOT NULL CHECK (amount > 0), settled BOOLEAN NOT NULL DEFAULT TRUE,
  memo TEXT, ref TEXT UNIQUE, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_ledger_date ON ledger(entry_date);
CREATE TABLE IF NOT EXISTS budget(period TEXT NOT NULL, account TEXT NOT NULL, amount NUMERIC(14,2) NOT NULL, PRIMARY KEY(period, account));
CREATE TABLE IF NOT EXISTS employees(
  id BIGSERIAL PRIMARY KEY, collar TEXT NOT NULL CHECK (collar IN ('blue','white')), name TEXT NOT NULL, email TEXT,
  title TEXT NOT NULL, department TEXT NOT NULL, salary NUMERIC(12,2) NOT NULL DEFAULT 0, hired_on DATE NOT NULL DEFAULT current_date,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','left')));
CREATE TABLE IF NOT EXISTS attendance(
  employee_id BIGINT NOT NULL, day DATE NOT NULL, status TEXT NOT NULL CHECK (status IN ('present','late','absent','leave')),
  PRIMARY KEY(employee_id, day));
CREATE TABLE IF NOT EXISTS reviews(
  id BIGSERIAL PRIMARY KEY, employee_id BIGINT NOT NULL, period TEXT NOT NULL,
  quality INT CHECK (quality BETWEEN 1 AND 5), punctuality INT CHECK (punctuality BETWEEN 1 AND 5),
  teamwork INT CHECK (teamwork BETWEEN 1 AND 5), output INT CHECK (output BETWEEN 1 AND 5),
  note TEXT, reviewer TEXT, created_at TIMESTAMPTZ DEFAULT now());
CREATE INDEX IF NOT EXISTS ix_rev_emp ON reviews(employee_id, period);
"""

# Which database each table lives in. No cross-database foreign keys: zones only share ids.
TABLE_ZONE = {"articles": "white", "used_nonces": "white", "users": "reporters", "submissions": "reporters",
              "transactions": "reporters", "favorites": "community", "clippings": "community",
              "edit_proposals": "community", "edit_votes": "community", "submission_events": "reporters",
              "employees": ("white", "blue"), "attendance": ("white", "blue"), "reviews": ("white", "blue")}
LIST_COLS = "id, short_code, slug, category, title, summary, author, sponsored, sponsor, published_at, version"


class Repository:
    ZONES = ("white", "blue", "reporters", "community")

    def __init__(self, cfg):
        self._zone = contextvars.ContextVar("zone", default="white")
        pools, self._pools = {}, {}
        for z in self.ZONES:
            dsn = getattr(cfg, f"dsn_{z}") or cfg.dsn
            if dsn not in pools:                         # zones sharing a database share a pool
                pools[dsn] = ConnectionPool(dsn, min_size=1, max_size=max(4, cfg.pool_max // 2), open=True,
                                            kwargs={"row_factory": dict_row}, check=ConnectionPool.check_connection)
            self._pools[z] = pools[dsn]

    def _q(self, sql, params=(), one=False, zone=None):
        with self._pools[zone or self._zone.get()].connection() as c:
            cur = c.execute(sql, params)
            if cur.description is None:
                return None
            return cur.fetchone() if one else cur.fetchall()

    def init_schema(self):
        def zone_of(stmt):
            m = re.search(r"\b(?:TABLE(?: IF NOT EXISTS)?|ON)\s+(\w+)", stmt)
            z_ = TABLE_ZONE.get(m.group(1), "white") if m else "white"
            return (z_,) if isinstance(z_, str) else z_
        for z in self.ZONES:
            stmts = [t for t in SCHEMA.split(";\n") if t.strip() and z in zone_of(t)]
            if stmts:
                with self._pools[z].connection() as c:
                    c.execute("SELECT pg_advisory_xact_lock(884422)")   # safe with many pods booting
                    for t in stmts:
                        c.execute(t)

    def count(self):
        return self._q("SELECT count(*) n FROM articles", one=True)["n"]

    def upsert_article(self, a, vec, img, sha) -> int:
        row = self._q("""INSERT INTO articles(slug,category,title,summary,body,lang,author,sponsored,sponsor,
                image,image_sha256,embedding) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)
            ON CONFLICT(slug) DO UPDATE SET title=EXCLUDED.title, summary=EXCLUDED.summary, body=EXCLUDED.body,
                image=COALESCE(EXCLUDED.image, articles.image), image_sha256=COALESCE(EXCLUDED.image_sha256, articles.image_sha256),
                embedding=EXCLUDED.embedding, version=articles.version+1
            RETURNING id""", (a["slug"], a["category"], a["title"], a.get("summary"), a["body"], a.get("lang", "en"),
                              a.get("author"), bool(a.get("sponsored")), a.get("sponsor"), img, sha, vec), one=True)
        return row["id"]

    def set_code(self, aid, code):
        self._q("UPDATE articles SET short_code=%s WHERE id=%s", (code, aid))

    def home_feed(self, per_cat=5):
        return self._q(f"""SELECT {LIST_COLS} FROM (SELECT *, row_number() OVER
            (PARTITION BY category ORDER BY published_at DESC) rn FROM articles) t WHERE rn<=%s
            ORDER BY published_at DESC""", (per_cat,))

    def section(self, cat, limit=30):
        return self._q(f"SELECT {LIST_COLS} FROM articles WHERE category=%s ORDER BY published_at DESC LIMIT %s", (cat, limit))

    def article(self, aid):
        return self._q("SELECT id,short_code,slug,category,title,summary,body,author,sponsored,sponsor,published_at,version,image_sha256 "
                       "FROM articles WHERE id=%s", (aid,), one=True)

    def image(self, aid):
        r = self._q("SELECT image FROM articles WHERE id=%s", (aid,), one=True)
        return bytes(r["image"]) if r and r["image"] else None

    def search(self, vec, text, limit=20):
        return self._q(f"""SELECT {LIST_COLS} FROM articles
            ORDER BY (embedding <=> %s::vector) - 0.3*ts_rank(tsv, plainto_tsquery('simple', %s)) LIMIT %s""",
                       (vec, text, limit))

    def use_nonce(self, n) -> bool:
        return self._q("INSERT INTO used_nonces(nonce) VALUES(%s) ON CONFLICT DO NOTHING RETURNING nonce", (n,), one=True) is not None

    # accounts, submissions, billing
    def user_add(self, email, name, role, pw_hash):
        return self._q("INSERT INTO users(email,name,role,pw_hash) VALUES(%s,%s,%s,%s) ON CONFLICT(email) DO NOTHING "
                       "RETURNING id,email,name,role", (email, name, role, pw_hash), one=True)

    def user_get(self, email):
        return self._q("SELECT id,email,name,role,pw_hash FROM users WHERE email=%s", (email,), one=True)

    def sub_add(self, uid, desk, a, img, sha) -> int:
        return self._q("""INSERT INTO submissions(user_id,desk,slug,category,title,summary,body,author,image,image_sha256,sponsored,sponsor)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                       (uid, desk, a["slug"], a["category"], a["title"], a.get("summary"), a["body"], a.get("author"),
                        img, sha, bool(a.get("sponsored")), a.get("sponsor")), one=True)["id"]

    def sub_update(self, sid, uid, a, img, sha):
        return self._q("""UPDATE submissions SET category=%s,title=%s,summary=%s,body=%s,image=COALESCE(%s,image),
            image_sha256=COALESCE(%s,image_sha256),status='submitted',editor_note=NULL,updated_at=now()
            WHERE id=%s AND user_id=%s AND status='changes_requested' RETURNING id""",
                       (a["category"], a["title"], a.get("summary"), a["body"], img, sha, sid, uid), one=True)

    def sub_pending(self, uid) -> int:
        return self._q("SELECT count(*) n FROM submissions WHERE user_id=%s AND status IN "
                       "('submitted','in_review','changes_requested')", (uid,), one=True)["n"]

    def subs_of(self, uid):
        return self._q("SELECT id,title,category,status,editor_note,fee,publish_at,updated_at FROM submissions "
                       "WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (uid,))

    def queue(self):
        return self._q("""SELECT s.id,s.title,s.category,s.status,s.desk,s.created_at,u.name AS reporter
            FROM submissions s LEFT JOIN users u ON u.id=s.user_id
            WHERE s.status IN ('submitted','in_review') ORDER BY s.created_at LIMIT 200""")

    def sub_get(self, sid):
        return self._q("""SELECT s.id,s.user_id,s.desk,s.slug,s.category,s.title,s.summary,s.body,s.author,s.status,
            s.editor_note,s.sponsored,s.sponsor,u.name AS reporter FROM submissions s LEFT JOIN users u ON u.id=s.user_id WHERE s.id=%s""",
                       (sid,), one=True)

    def sub_image(self, sid):
        r = self._q("SELECT image FROM submissions WHERE id=%s", (sid,), one=True)
        return bytes(r["image"]) if r and r["image"] else None

    def sub_claim(self, sid, reviewer) -> bool:
        return self._q("UPDATE submissions SET status='in_review',reviewer_id=%s,updated_at=now() WHERE id=%s "
                       "AND status IN ('submitted','in_review') RETURNING id", (reviewer, sid), one=True) is not None

    def sub_done(self, sid, article_id, fee, checks, note):
        return self._q("UPDATE submissions SET status='published',article_id=%s,fee=%s,checks=%s::jsonb,editor_note=%s,"
                       "updated_at=now() WHERE id=%s AND status='in_review' RETURNING user_id",
                       (article_id, fee, json.dumps(checks), note, sid), one=True)

    def sub_set(self, sid, status, note, reviewer) -> bool:
        return self._q("UPDATE submissions SET status=%s,editor_note=%s,reviewer_id=%s,updated_at=now() WHERE id=%s "
                       "AND status IN ('submitted','in_review') RETURNING id", (status, note, reviewer, sid), one=True) is not None

    def tx_add(self, uid, sid, kind, amount, note):
        self._q("INSERT INTO transactions(user_id,submission_id,kind,amount,note) VALUES(%s,%s,%s,%s,%s)",
                (uid, sid, kind, amount, note[:200]))

    def tx_list(self, uid):
        return self._q("SELECT created_at,kind,amount,note FROM transactions WHERE user_id=%s "
                       "ORDER BY created_at DESC LIMIT 300", (uid,))

    def balances(self):
        return self._q("""SELECT u.id,u.name,u.email,SUM(CASE WHEN t.kind='payout' THEN -t.amount ELSE t.amount END) AS due
            FROM users u JOIN transactions t ON t.user_id=u.id GROUP BY u.id HAVING SUM(CASE WHEN t.kind='payout'
            THEN -t.amount ELSE t.amount END) <> 0 ORDER BY due DESC""")

    def sub_recent(self, uid) -> int:
        return self._q("SELECT count(*) n FROM submissions WHERE user_id=%s AND created_at > now() - interval '1 hour'",
                       (uid,), one=True)["n"]

    def slot_near(self, when, gap):
        return self._q("SELECT max(publish_at) m FROM submissions WHERE status IN ('scheduled','in_review','published') "
                       "AND publish_at > %s AND publish_at < %s", (when - gap, when + gap), one=True)["m"]

    def sub_schedule(self, sid, reviewer, when, fee, checks, note) -> bool:
        return self._q("UPDATE submissions SET status='scheduled',reviewer_id=%s,publish_at=%s,fee=%s,checks=%s::jsonb,"
                       "editor_note=%s,updated_at=now() WHERE id=%s AND status IN ('submitted','in_review') RETURNING id",
                       (reviewer, when, fee, json.dumps(checks), note, sid), one=True) is not None

    def sub_due(self, limit):
        return self._q("""UPDATE submissions SET status='in_review',updated_at=now() WHERE id IN (
            SELECT id FROM submissions WHERE status='scheduled' AND publish_at<=now()
            ORDER BY publish_at LIMIT %s FOR UPDATE SKIP LOCKED) RETURNING id,publish_at,fee,checks""", (limit,))

    def scheduled(self):
        return self._q("SELECT id,title,category,publish_at FROM submissions WHERE status='scheduled' "
                       "ORDER BY publish_at LIMIT 100")

    def sub_unschedule(self, sid) -> bool:
        return self._q("UPDATE submissions SET status='submitted',publish_at=NULL,updated_at=now() WHERE id=%s "
                       "AND status='scheduled' RETURNING id", (sid,), one=True) is not None

    def art_time(self, aid, when):
        self._q("UPDATE articles SET published_at=%s WHERE id=%s", (when, aid))

    def ev_add(self, sid, event, actor, detail=""):
        self._q("INSERT INTO submission_events(submission_id,event,actor,detail) VALUES(%s,%s,%s,%s)",
                (sid, event, actor, detail))

    def ev_list(self, sid):
        return self._q("SELECT at,event,actor,detail FROM submission_events WHERE submission_id=%s ORDER BY at", (sid,))

    # archive (white)
    def archive(self, d1, d2, cats):
        return self._q("""SELECT id,short_code,title,category,author,sponsored,published_at FROM articles
            WHERE published_at<=now() AND published_at::date BETWEEN %s AND %s
              AND (cardinality(%s::text[])=0 OR category=ANY(%s)) ORDER BY published_at DESC LIMIT 500""",
                       (d1, d2, list(cats), list(cats)))

    # reporters zone helpers
    def sub_edit(self, sid, title, summary, body) -> bool:
        return self._q("UPDATE submissions SET title=%s,summary=%s,body=%s,updated_at=now() WHERE id=%s "
                       "AND status IN ('submitted','in_review','changes_requested') RETURNING id",
                       (title, summary, body, sid), one=True) is not None

    def open_subs(self):
        return self._q("SELECT id,title,category FROM submissions WHERE status IN "
                       "('submitted','in_review','changes_requested') ORDER BY created_at DESC LIMIT 100")

    def subs_by_ids(self, ids):
        return self._q("SELECT id,title FROM submissions WHERE id=ANY(%s)", (list(ids),)) if ids else []

    def fees(self, ids):
        rows = self._q("SELECT article_id,fee FROM submissions WHERE article_id=ANY(%s) AND status='published'", (list(ids),)) if ids else []
        return {r["article_id"]: r["fee"] for r in rows}

    # polling (community)
    def prop_add(self, sid, uid, title, summary, body, reason) -> int:
        return self._q("INSERT INTO edit_proposals(submission_id,proposer_id,title,summary,body,reason) "
                       "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id", (sid, uid, title, summary, body, reason), one=True)["id"]

    def prop_get(self, pid):
        return self._q("SELECT * FROM edit_proposals WHERE id=%s", (pid,), one=True)

    def prop_open(self):
        return self._q("SELECT * FROM edit_proposals WHERE status='open' ORDER BY created_at DESC LIMIT 50")

    def prop_close(self, pid, status) -> bool:
        return self._q("UPDATE edit_proposals SET status=%s WHERE id=%s AND status='open' RETURNING id",
                       (status, pid), one=True) is not None

    def vote_cast(self, pid, uid, vote, comment):
        self._q("INSERT INTO edit_votes(proposal_id,voter_id,vote,comment) VALUES(%s,%s,%s,%s) ON CONFLICT(proposal_id,voter_id) "
                "DO UPDATE SET vote=EXCLUDED.vote, comment=EXCLUDED.comment, created_at=now()", (pid, uid, vote, comment))

    def vote_tally(self, pid) -> dict:
        t = {"yes": 0, "no": 0, "not_interested": 0}
        for r in self._q("SELECT vote,count(*) n FROM edit_votes WHERE proposal_id=%s GROUP BY vote", (pid,)):
            t[r["vote"]] = r["n"]
        return t

    def vote_comments(self, pid):
        return self._q("SELECT vote,comment FROM edit_votes WHERE proposal_id=%s AND comment<>'' ORDER BY created_at", (pid,))

    # finance (white)
    def ledger_add(self, day, kind, account, amount, settled, memo, ref=None) -> bool:
        return self._q("INSERT INTO ledger(entry_date,kind,account,amount,settled,memo,ref) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                       "ON CONFLICT(ref) DO NOTHING RETURNING id", (day, kind, account, amount, settled, memo, ref), one=True) is not None

    def ledger_sums(self, start, end):
        return self._q("SELECT kind,account,settled,SUM(amount) amt FROM ledger WHERE entry_date BETWEEN %s AND %s "
                       "GROUP BY kind,account,settled", (start, end))

    def ledger_recent(self, limit=50):
        return self._q("SELECT entry_date,kind,account,amount,settled,memo FROM ledger ORDER BY entry_date DESC,id DESC LIMIT %s", (limit,))

    def ledger_unsettled(self):
        return self._q("SELECT id,entry_date,kind,account,amount,memo FROM ledger WHERE NOT settled AND kind IN ('income','expense') "
                       "ORDER BY entry_date LIMIT 30")

    def ledger_settle(self, lid):
        self._q("UPDATE ledger SET settled=TRUE WHERE id=%s", (lid,))

    def budget_get(self, period) -> dict:
        return {r["account"]: float(r["amount"]) for r in self._q("SELECT account,amount FROM budget WHERE period=%s", (period,))}

    def budget_set(self, period, account, amount):
        self._q("INSERT INTO budget(period,account,amount) VALUES(%s,%s,%s) ON CONFLICT(period,account) DO UPDATE "
                "SET amount=EXCLUDED.amount", (period, account, amount))

    def tx_sums(self, start, end):
        return self._q("SELECT kind,COALESCE(SUM(amount),0) amt FROM transactions WHERE created_at::date BETWEEN %s AND %s "
                       "GROUP BY kind", (start, end))

    def reporter_stats(self):
        return self._q("""SELECT u.name, count(*) AS submitted, count(*) FILTER (WHERE s.status='published') AS published,
            count(*) FILTER (WHERE s.status='rejected') AS rejected,
            COALESCE(avg(EXTRACT(EPOCH FROM (s.updated_at - s.created_at))/3600) FILTER (WHERE s.status='published'),0) AS hours_to_live,
            COALESCE(sum(s.fee) FILTER (WHERE s.status='published' AND NOT COALESCE(s.sponsored,false)),0) AS earned
            FROM submissions s JOIN users u ON u.id=s.user_id GROUP BY u.id,u.name ORDER BY published DESC LIMIT 50""")

    def analytics_rows(self, days):
        return self._q("SELECT id,category,title,summary,body,published_at FROM articles WHERE lang='en' AND published_at<=now() "
                       "AND published_at > now() - make_interval(days => %s) ORDER BY published_at DESC LIMIT 2000", (days,))

    # HR: white-collar staff live in the white database, blue-collar staff in the blue database
    @staticmethod
    def _hz(collar):
        return "blue" if collar == "blue" else "white"

    def emp_add(self, collar, name, email, title, dept, salary, hired) -> int:
        return self._q("INSERT INTO employees(collar,name,email,title,department,salary,hired_on) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                       "RETURNING id", (collar, name, email, title, dept, salary, hired), one=True, zone=self._hz(collar))["id"]

    def emp_list(self, collar):
        return self._q("SELECT id,name,email,title,department,salary,hired_on,status FROM employees WHERE collar=%s "
                       "ORDER BY status,name", (collar,), zone=self._hz(collar))

    def emp_status(self, collar, eid, status):
        self._q("UPDATE employees SET status=%s WHERE id=%s AND collar=%s", (status, eid, collar), zone=self._hz(collar))

    def att_day(self, collar, day) -> dict:
        rows = self._q("SELECT a.employee_id,a.status FROM attendance a JOIN employees e ON e.id=a.employee_id "
                       "WHERE a.day=%s AND e.collar=%s", (day, collar), zone=self._hz(collar))
        return {r["employee_id"]: r["status"] for r in rows}

    def att_save(self, collar, day, rows):
        for eid, status in rows:
            self._q("INSERT INTO attendance(employee_id,day,status) VALUES(%s,%s,%s) ON CONFLICT(employee_id,day) "
                    "DO UPDATE SET status=EXCLUDED.status", (eid, day, status), zone=self._hz(collar))

    def att_stats(self, collar, start):
        return self._q("""SELECT a.employee_id, count(*) days, count(*) FILTER (WHERE a.status IN ('present','late')) present
            FROM attendance a JOIN employees e ON e.id=a.employee_id WHERE a.day>=%s AND e.collar=%s GROUP BY a.employee_id""",
                       (start, collar), zone=self._hz(collar))

    def review_add(self, collar, eid, period, q, p, t, o, note, reviewer):
        self._q("INSERT INTO reviews(employee_id,period,quality,punctuality,teamwork,output,note,reviewer) "
                "SELECT %s,%s,%s,%s,%s,%s,%s,%s WHERE EXISTS (SELECT 1 FROM employees WHERE id=%s AND collar=%s)",
                (eid, period, q, p, t, o, note, reviewer, eid, collar), zone=self._hz(collar))

    def review_avgs(self, collar):
        return self._q("""SELECT r.employee_id, avg((r.quality+r.punctuality+r.teamwork+r.output)/4.0) score
            FROM reviews r JOIN employees e ON e.id=r.employee_id WHERE e.collar=%s GROUP BY r.employee_id""",
                       (collar,), zone=self._hz(collar))

    def payroll_total(self) -> float:
        return sum(float(self._q("SELECT COALESCE(SUM(salary),0) s FROM employees WHERE collar=%s AND status='active'",
                                 (c,), one=True, zone=self._hz(c))["s"]) for c in ("white", "blue"))

    # favourites / cart
    def fav_set(self, uid, aid, state):
        self._q("""INSERT INTO favorites(user_id,article_id,state) VALUES(%s,%s,%s)
                   ON CONFLICT(user_id,article_id) DO UPDATE SET state=EXCLUDED.state, updated_at=now()""", (uid, aid, state))

    def fav_del(self, uid, aid):
        self._q("DELETE FROM favorites WHERE user_id=%s AND article_id=%s", (uid, aid))

    def fav_rows(self, uid):
        return self._q("SELECT article_id, state FROM favorites WHERE user_id=%s AND state<>'checked_out' "
                       "ORDER BY updated_at DESC", (uid,))

    def clip_rows(self, uid):
        return self._q("SELECT id, article_id, note FROM clippings WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (uid,))

    def titles(self, ids):
        rows = self._q("SELECT id, short_code, title FROM articles WHERE id = ANY(%s)", (list(ids),)) if ids else []
        return {r["id"]: r for r in rows}

    def fav_list(self, uid):
        favs = self.fav_rows(uid)
        t = self.titles([f["article_id"] for f in favs])
        return [{**t[f["article_id"]], "state": f["state"]} for f in favs if f["article_id"] in t]

    def clip_add(self, uid, aid, note):
        self._q("INSERT INTO clippings(user_id,article_id,note) VALUES(%s,%s,%s)", (uid, aid, note[:500]))

    def clip_list(self, uid):
        rows = self.clip_rows(uid)
        t = self.titles([r["article_id"] for r in rows])
        return [{"id": r["id"], "note": r["note"], **t[r["article_id"]]} for r in rows if r["article_id"] in t]


_ZONE_METHODS = {
    "reporters": ("user_add", "user_get", "sub_add", "sub_update", "sub_pending", "subs_of", "queue", "sub_get", "sub_image",
                  "sub_claim", "sub_done", "sub_set", "tx_add", "tx_list", "balances", "tx_sums", "reporter_stats", "sub_recent", "slot_near", "sub_schedule", "sub_due", "scheduled", "sub_unschedule", "ev_add", "ev_list", "sub_edit", "open_subs", "subs_by_ids", "fees"),
    "community": ("fav_set", "fav_del", "fav_rows", "clip_add", "clip_rows", "prop_add", "prop_get", "prop_open", "prop_close",
                  "vote_cast", "vote_tally", "vote_comments"),
}


def _in_zone(fn, zone):
    @functools.wraps(fn)
    def run(self, *a, **k):
        token = self._zone.set(zone)
        try:
            return fn(self, *a, **k)
        finally:
            self._zone.reset(token)
    return run


for _zone, _names in _ZONE_METHODS.items():
    for _name in _names:
        setattr(Repository, _name, _in_zone(getattr(Repository, _name), _zone))


# ───────────────────────── services ─────────────────────────
class LinkShortener:
    """Bijective id<->7-char base62 code: unguessable-looking, collision-free, decodes to a PK lookup."""
    _A, _M = 2654435761, 62 ** 7
    _CH = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def encode(self, i: int) -> str:
        n, out = (i * self._A) % self._M, ""
        for _ in range(7):
            n, r = divmod(n, 62)
            out = self._CH[r] + out
        return out

    def decode(self, code: str) -> int | None:
        if not re.fullmatch(r"[0-9a-zA-Z]{7}", code or ""):
            return None
        n = 0
        for ch in code:
            n = n * 62 + self._CH.index(ch)
        return (n * pow(self._A, -1, self._M)) % self._M


class Vectorizer:
    """Dependency-free hashing vectoriser (256-d, uni+bi-grams). Swap for a model embedder later."""
    DIM = 256

    def embed(self, text: str) -> str:
        toks = re.findall(r"\w+", text.lower())
        v = [0.0] * self.DIM
        for t in toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]:
            h = int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), "big")
            v[h % self.DIM] += 1.0 if (h >> 63) else -1.0
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return "[" + ",".join(f"{x / norm:.5f}" for x in v) + "]"


class Translator:
    LANGS = {"English": "en", "বাংলা": "bn", "हिन्दी": "hi", "اردو": "ur", "العربية": "ar",
             "Français": "fr", "Español": "es", "中文": "zh-CN"}

    def __init__(self, kv: KV, libre_url: str, pool: ThreadPoolExecutor):
        self._kv, self._libre, self._pool = kv, libre_url, pool

    def _engine(self, lang):
        if self._libre:
            from deep_translator import LibreTranslator
            return LibreTranslator(source="auto", target=lang, base_url=self._libre, api_key=os.getenv("LIBRETRANSLATE_KEY", ""))
        from deep_translator import GoogleTranslator
        return GoogleTranslator(source="auto", target=lang)

    def one(self, text: str, lang: str) -> str:
        if lang == "en" or not text:
            return text
        key = f"tr:{lang}:{hashlib.sha1(text.encode()).hexdigest()}"
        if hit := self._kv.get(key):
            return hit
        try:
            out = self._engine(lang).translate(text[:4500])
        except Exception:
            return text                       # never break the page because translation failed
        self._kv.set(key, out, 7 * 86400)
        return out

    def many(self, texts, lang):
        return list(self._pool.map(lambda t: self.one(t, lang), texts)) if lang != "en" else list(texts)

    def body(self, text, lang):
        return "\n\n".join(self.many(text.split("\n\n"), lang))


@dataclass(frozen=True)
class Allocation:
    favorites: int
    checkins: int
    per_minute: int


class AllocationManager:
    """Virtual per-user slice: quota + rate limit, cached by cookie id (shared via Redis across pods)."""
    def __init__(self, kv: KV, policy: Policy):
        self._kv, self._policy = kv, policy

    def of(self, uid: str, tier="reader") -> Allocation:
        return Allocation(*self._policy.quota(tier))

    def allow(self, uid: str) -> bool:
        return self._kv.incr(f"rl:{uid}:{int(time.time() // 60)}", 60) <= self.of(uid).per_minute


class CartService:
    """Favourites as a shopping list: save -> check in (reading shelf) -> check out (export + clear)."""
    def __init__(self, repo: Repository, alloc: AllocationManager, short_url):
        self._repo, self._alloc, self._url = repo, alloc, short_url

    def add(self, uid, aid):
        a = self._alloc.of(uid)
        if len(self._repo.fav_list(uid)) >= a.favorites:
            raise PolicyError("Your list is full. Check out or remove items first.")
        self._repo.fav_set(uid, aid, "saved")

    def check_in(self, uid, aid):
        a = self._alloc.of(uid)
        if sum(i["state"] == "checked_in" for i in self._repo.fav_list(uid)) >= a.checkins:
            raise PolicyError("Reading shelf is full.")
        self._repo.fav_set(uid, aid, "checked_in")

    def remove(self, uid, aid):
        self._repo.fav_del(uid, aid)

    def items(self, uid):
        return self._repo.fav_list(uid)

    def check_out(self, uid) -> str:
        items = self._repo.fav_list(uid)
        for i in items:
            self._repo.fav_set(uid, i["id"], "checked_out")
        return "# My reading list\n\n" + "\n".join(f"- [{i['title']}]({self._url(i['short_code'])})" for i in items)


class SecurityService:
    """Verifies sealed packages from remote desks: authenticated encryption (Fernet), per-desk keys and
    scopes, replay protection, image SHA-256 + decode check. Independent of TLS, so a MITM can neither
    read nor alter a story or its photo."""
    def __init__(self, desks: dict, repo: Repository, policy: Policy):
        self.__desks, self.__repo, self.__policy = desks, repo, policy

    def open(self, token: str):
        desk, _, tok = token.strip().partition(".")
        cfg = self.__desks.get(desk)
        if not cfg:
            raise PolicyError("Unknown desk")
        try:
            raw = Fernet(cfg["key"].encode()).decrypt(tok.encode(), ttl=int(cfg.get("ttl", 86400)))
        except (InvalidToken, ValueError):
            raise PolicyError("Package is tampered, expired, or sealed with the wrong key")
        p = json.loads(raw)
        if p.get("desk") != desk:
            raise PolicyError("Desk mismatch")
        art = p["article"]
        if not self.__policy.scope_ok(cfg.get("scopes", []), art.get("category", "")):
            raise PolicyError("Desk has no scope for this category")
        img = None
        if p.get("image_b64"):
            img = base64.b64decode(p["image_b64"], validate=True)
            if hashlib.sha256(img).hexdigest() != p.get("image_sha256"):
                raise PolicyError("Image checksum mismatch - resend the package")
            self.__policy.check_image(img)
        if not self.__repo.use_nonce(p["nonce"]):
            raise PolicyError("Replay rejected: package already used")
        return art, img


class Publisher:
    def __init__(self, repo, policy, security, vec, shortener):
        self._r, self._p, self._s, self._v, self._sh = repo, policy, security, vec, shortener

    def publish(self, art: dict, img: bytes | None = None) -> str:
        self._p.check_article(art)
        sha = hashlib.sha256(img).hexdigest() if img else None
        vec = self._v.embed(f"{art['title']} {art.get('summary', '')} {art['body']}")
        aid = self._r.upsert_article(art, vec, img, sha)
        code = self._sh.encode(aid)
        self._r.set_code(aid, code)
        return code

    def publish_sealed(self, token: str) -> str:
        art, img = self._s.open(token)
        return self.publish(art, img)


class AuthService:
    """Sign-in for reporters and editors: scrypt password hashes, login throttling, invite-only editor role."""
    def __init__(self, repo: Repository, kv: KV, policy: Policy, invite: str):
        self._repo, self._kv, self._policy, self._invite = repo, kv, policy, invite

    @staticmethod
    def _hash(pw: str, salt: bytes | None = None) -> str:
        salt = salt or os.urandom(16)
        return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()

    def register(self, email, name, pw, code="", kind="reporter") -> dict:
        email = email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not name.strip():
            raise PolicyError("Enter your full name and a valid email address")
        if not self._policy.password_ok(pw):
            raise PolicyError("Use a password of at least 10 characters")
        role = "editor" if self._invite and hmac.compare_digest(code, self._invite) else ("sponsor" if kind == "sponsor" else "reporter")
        user = self._repo.user_add(email, name.strip()[:80], role, self._hash(pw))
        if not user:
            raise PolicyError("That email is already registered. Sign in instead.")
        return dict(user)

    def login(self, email, pw) -> dict:
        email = email.strip().lower()
        if self._kv.incr(f"login:{email}", 600) > 8:
            raise PolicyError("Too many attempts. Try again in 10 minutes.")
        u = self._repo.user_get(email)
        ok = bool(u) and hmac.compare_digest(self._hash(pw, bytes.fromhex(u["pw_hash"].split("$")[0])), u["pw_hash"])
        if not ok:
            raise PolicyError("Wrong email or password")
        return {k: u[k] for k in ("id", "email", "name", "role")}


class Newsroom:
    """Editorial workflow: submit -> review (verification checklist) -> publish / return / reject, plus billing."""
    def __init__(self, repo, policy, publisher, security, shortener, cfg):
        self._r, self._p, self._pub, self._sec, self._sh, self._cfg = repo, policy, publisher, security, shortener, cfg

    def submit(self, user, art, img=None, sid=None) -> int:
        art = {**art, "slug": self._p.slugify(art.get("title", "")), "lang": "en"}
        self._p.check_article(art)
        sha = None
        if img:
            self._p.check_image(img)
            sha = hashlib.sha256(img).hexdigest()
        if sid:
            if not self._r.sub_update(sid, user["id"], art, img, sha):
                raise PolicyError("This story can no longer be edited")
            self._r.ev_add(sid, "resubmitted", user["name"])
            return sid
        if not self._p.can_submit(self._r.sub_pending(user["id"])):
            raise PolicyError("You have too many stories waiting. Wait for a decision first.")
        if not self._p.can_submit_now(self._r.sub_recent(user["id"])):
            raise PolicyError("Hourly submission limit reached. Try again later.")
        sid = self._r.sub_add(user["id"], None, art, img, sha)
        self._r.ev_add(sid, "submitted", user["name"])
        return sid

    def submit_sealed(self, token: str) -> int:
        art, img = self._sec.open(token)
        self._p.check_article(art)
        sha = hashlib.sha256(img).hexdigest() if img else None
        desk = token.partition(".")[0]
        sid = self._r.sub_add(None, desk, art, img, sha)
        self._r.ev_add(sid, "submitted", "desk " + desk)
        return sid

    def _slot(self, when):
        """Rate limit: keep at least `gap` minutes between go-lives by moving a slot past any neighbour."""
        gap = timedelta(minutes=self._p.gap)
        for _ in range(48):
            near = self._r.slot_near(when, gap)
            if not near:
                return when
            when = near + gap
        return when

    def approve(self, sid, editor, checks, fee, note, when=None, breaking=False):
        """Verify -> schedule. Returns (go-live time, article code if it is already live)."""
        if not self._p.can_publish(checks):
            raise PolicyError("Tick every verification check before publishing")
        now = datetime.now(timezone.utc)
        when = max(when or now, now)
        if not breaking:
            when = self._slot(when)
        if not self._r.sub_schedule(sid, editor["id"], when, fee, checks, note):
            raise PolicyError("This story was already handled by another editor")
        local = when.astimezone(ZoneInfo(self._cfg.tz)).strftime("%d %b %H:%M")
        self._r.ev_add(sid, "scheduled", editor["name"], f"go-live {local}" + (" (breaking)" if breaking else ""))
        code = self.release_due().get(sid) if when <= now + timedelta(seconds=5) else None
        return when, code

    def release_due(self, limit=5) -> dict:
        """Publish every scheduled story whose time has come. Safe to call from many pods at once."""
        out = {}
        for r in self._r.sub_due(limit):
            sid = r["id"]
            try:
                s = self._r.sub_get(sid)
                art = {k: s[k] for k in ("slug", "category", "title", "summary", "body", "author")}
                code = self._pub.publish({**art, "lang": "en", "sponsored": s["sponsored"], "sponsor": s["sponsor"]}, self._r.sub_image(sid))
                aid = self._sh.decode(code)
                self._r.art_time(aid, r["publish_at"])
                row = self._r.sub_done(sid, aid, r["fee"], r["checks"], s["editor_note"])
                if row and row["user_id"] and r["fee"] > 0 and not s["sponsored"]:
                    self._r.tx_add(row["user_id"], sid, "earning", r["fee"], "Published: " + s["title"][:80])
                if s["sponsored"] and r["fee"] > 0:
                    self._r.ledger_add(datetime.now(timezone.utc).date(), "income", "Sponsorship", r["fee"], False,
                                       "Sponsored story: " + s["title"][:80], f"sponsor-{sid}")
                self._r.ev_add(sid, "published", "system")
                out[sid] = code
            except Exception as e:
                self._r.ev_add(sid, "release_failed", "system", str(e)[:200])   # stays in the live queue
        return out

    def unschedule(self, sid, editor):
        if self._r.sub_unschedule(sid):
            self._r.ev_add(sid, "unscheduled", editor["name"])

    def decide(self, sid, editor, status, note):
        if status not in ("changes_requested", "rejected"):
            raise PolicyError("Unknown decision")
        if not note.strip():
            raise PolicyError("Add a note so the reporter knows why")
        if not self._r.sub_set(sid, status, note.strip(), editor["id"]):
            raise PolicyError("This story was already handled")
        self._r.ev_add(sid, "returned" if status == "changes_requested" else "rejected", editor["name"], note.strip()[:200])

    def payout(self, uid, amount, ref):
        due = next((b["due"] for b in self._r.balances() if b["id"] == uid), 0)
        if amount <= 0 or amount > due:
            raise PolicyError("Payout must be above zero and not more than the balance due")
        self._r.tx_add(uid, None, "payout", amount, ref or "Payout")


class Polling:
    """Peer review of edits: reporters and editors vote yes / no / not interested; an edit is applied
    automatically once the policy's approval threshold is met (rules live in Policy)."""
    VOTES = ("yes", "no", "not_interested")

    def __init__(self, repo, policy):
        self._r, self._p = repo, policy

    @staticmethod
    def _eligible(user):
        if not user or user["role"] not in ("reporter", "editor"):
            raise PolicyError("Only reporters and editors can take part in polls")

    def propose(self, user, sid, title, summary, body, reason) -> int:
        self._eligible(user)
        s = self._r.sub_get(sid)
        if not s or s["status"] not in ("submitted", "in_review", "changes_requested"):
            raise PolicyError("That story is no longer open for edits")
        if not title.strip() or not body.strip():
            raise PolicyError("Headline and story text are required")
        return self._r.prop_add(sid, user["id"], title.strip(), summary.strip(), body.strip(), reason.strip()[:500])

    def vote(self, user, pid, vote, comment="") -> str:
        self._eligible(user)
        p = self._r.prop_get(pid)
        if not p or p["status"] != "open":
            raise PolicyError("This poll is closed")
        if p["proposer_id"] == user["id"]:
            raise PolicyError("You can't vote on your own proposal")
        if vote not in self.VOTES:
            raise PolicyError("Choose yes, no or not interested")
        self._r.vote_cast(pid, user["id"], vote, comment.strip()[:500])
        t = self._r.vote_tally(pid)
        if self._p.poll_passed(t["yes"], t["no"]) and self._r.prop_close(pid, "applied"):
            if not self._r.sub_edit(p["submission_id"], p["title"], p["summary"], p["body"]):
                raise PolicyError("Poll passed but the story was already decided")
            return "applied"
        return "open"


class Finance:
    """The newsroom's books. Statements are derived (accrual basis); reporter fees come from the billing ledger."""
    FAR = date(2000, 1, 1)

    def __init__(self, repo, policy):
        self._r, self._p = repo, policy

    def add(self, kind, account, amount, day, settled, memo, ref=None) -> bool:
        ok = {"income": self._p.income_accounts, "expense": self._p.expense_accounts,
              "capital": ("Owner capital",), "drawing": ("Owner drawings",)}
        if account not in ok.get(kind, ()):
            raise PolicyError("Unknown account")
        if not amount or amount <= 0:
            raise PolicyError("Amount must be above zero")
        return self._r.ledger_add(day, kind, account, amount, settled, (memo or "").strip()[:200], ref)

    def _sums(self, start, end) -> dict:
        d = {"inc": {}, "exp": {}, "inc_cash": 0.0, "inc_open": 0.0, "exp_cash": 0.0, "exp_open": 0.0, "capital": 0.0, "drawing": 0.0}
        for r in self._r.ledger_sums(start, end):
            amt, k = float(r["amt"]), r["kind"]
            if k in ("income", "expense"):
                key = "inc" if k == "income" else "exp"
                d[key][r["account"]] = d[key].get(r["account"], 0.0) + amt
                d[key[:3] + ("_cash" if r["settled"] else "_open")] += amt
            else:
                d[k] += amt
        t = {r["kind"]: float(r["amt"]) for r in self._r.tx_sums(start, end)}
        d["fees"], d["payouts"] = t.get("earning", 0.0) + t.get("adjustment", 0.0), t.get("payout", 0.0)
        return d

    def income_statement(self, start, end):
        d = self._sums(start, end)
        rev = [(a, d["inc"].get(a, 0.0)) for a in self._p.income_accounts]
        exp = [("Reporter fees", d["fees"])] + [(a, d["exp"].get(a, 0.0)) for a in self._p.expense_accounts]
        tr, te = sum(v for _, v in rev), sum(v for _, v in exp)
        return rev, exp, tr, te, tr - te

    def _cash(self, asof) -> float:
        d = self._sums(self.FAR, asof)
        return d["capital"] - d["drawing"] + d["inc_cash"] - d["exp_cash"] - d["payouts"]

    def cash_flow(self, start, end) -> dict:
        d = self._sums(start, end)
        opening = self._cash(start - timedelta(days=1))
        op_in, op_out, fin = d["inc_cash"], d["exp_cash"] + d["payouts"], d["capital"] - d["drawing"]
        net = op_in - op_out + fin
        return {"opening": opening, "op_in": op_in, "op_out": op_out, "fin": fin, "net": net, "closing": opening + net}

    def balance_sheet(self, asof) -> dict:
        d = self._sums(self.FAR, asof)
        cash = d["capital"] - d["drawing"] + d["inc_cash"] - d["exp_cash"] - d["payouts"]
        recv, payables, reporter = d["inc_open"], d["exp_open"], d["fees"] - d["payouts"]
        retained = sum(d["inc"].values()) - sum(d["exp"].values()) - d["fees"]
        capital = d["capital"] - d["drawing"]
        assets, liab, equity = cash + recv, payables + reporter, capital + retained
        return {"cash": cash, "recv": recv, "assets": assets, "payables": payables, "reporter": reporter, "liab": liab,
                "capital": capital, "retained": retained, "equity": equity, "balanced": abs(assets - liab - equity) < 0.01}

    @staticmethod
    def _month(period):
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period or ""):
            raise PolicyError("Use the format YYYY-MM")
        start = date(int(period[:4]), int(period[5:]), 1)
        return start, (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    def budget_vs_actual(self, period) -> list:
        start, end = self._month(period)
        rev, exp, *_ = self.income_statement(start, end)
        actual, plan = {**dict(rev), **dict(exp)}, self._r.budget_get(period)
        out = []
        for section, line in self._p.budget_lines():
            b, a = plan.get(line, 0.0), actual.get(line, 0.0)
            out.append((section, line, b, a, (a - b) if section == "Income" else (b - a)))
        return out

    def budget_save(self, period, amounts: dict):
        self._month(period)
        valid = {line for _, line in self._p.budget_lines()}
        for line, amt in amounts.items():
            if line in valid and amt >= 0:
                self._r.budget_set(period, line, amt)


class Hr:
    """Employee management for white-collar and blue-collar staff, attendance, reviews, payroll and performance."""
    def __init__(self, repo, policy, finance):
        self._r, self._p, self._fin = repo, policy, finance

    def add_employee(self, collar, name, email, title, dept, salary, hired) -> int:
        if collar not in self._p.collars or not name.strip() or not title.strip():
            raise PolicyError("Name, job title and staff type are required")
        if dept not in self._p.departments or salary < 0:
            raise PolicyError("Choose a department and a valid salary")
        return self._r.emp_add(collar, name.strip()[:80], email.strip().lower()[:120], title.strip()[:80], dept, salary, hired)

    def attendance(self, collar, day, rows):
        if any(st_ not in self._p.attendance_states for _, st_ in rows):
            raise PolicyError("Unknown attendance status")
        self._r.att_save(collar, day, rows)

    def review(self, collar, eid, period, scores, note, reviewer):
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period) or not all(1 <= x <= 5 for x in scores):
            raise PolicyError("Use a period like 2026-11 and scores from 1 to 5")
        self._r.review_add(collar, eid, period, *scores, note.strip()[:500], reviewer)

    def run_payroll(self, month) -> float:
        _, end = self._fin._month(month)
        total = self._r.payroll_total()
        if total <= 0:
            raise PolicyError("There are no active salaries to pay")
        if not self._fin.add("expense", "Salaries", total, end, True, f"Payroll {month}", ref=f"payroll-{month}"):
            raise PolicyError("Payroll for that month is already posted")
        return total

    def performance(self, days=30) -> list:
        start, out = date.today() - timedelta(days=days), []
        for collar in self._p.collars:
            att = {a["employee_id"]: a for a in self._r.att_stats(collar, start)}
            rev = {r["employee_id"]: float(r["score"]) for r in self._r.review_avgs(collar)}
            for e in self._r.emp_list(collar):
                if e["status"] != "active":
                    continue
                a = att.get(e["id"])
                rate = a["present"] / a["days"] if a and a["days"] else None
                sc = rev.get(e["id"])
                out.append({"Employee": e["name"], "Type": collar.title() + "-collar", "Department": e["department"],
                            "Attendance %": round(rate * 100, 1) if rate is not None else None,
                            "Review score": round(sc, 2) if sc else None,
                            "Index": self._p.performance_index(rate, sc) if rate is not None and sc else None})
        return out


class NewsAnalytics:
    """Text analytics with NLTK: VADER sentiment, keyword frequency and length (English stories)."""
    def __init__(self):
        self._sia, self._stop, self._lock = None, None, threading.Lock()

    def _load(self):
        with self._lock:
            if self._sia:
                return
            import nltk
            for pkg, path in (("vader_lexicon", "sentiment/vader_lexicon.zip"), ("stopwords", "corpora/stopwords")):
                try:
                    nltk.data.find(path)
                except LookupError:
                    nltk.download(pkg, quiet=True)
            from nltk.corpus import stopwords
            from nltk.sentiment import SentimentIntensityAnalyzer
            self._sia, self._stop = SentimentIntensityAnalyzer(), set(stopwords.words("english"))

    def analyze(self, rows):
        self._load()
        from nltk import FreqDist
        from nltk.tokenize import RegexpTokenizer
        tok, freq, out = RegexpTokenizer(r"[A-Za-z']{3,}"), FreqDist(), []
        for r in rows:
            words = [w.lower() for w in tok.tokenize(f"{r['title']}. {r['summary'] or ''} {r['body']}")]
            freq.update(w for w in words if w not in self._stop)
            score = self._sia.polarity_scores(f"{r['title']}. {r['summary'] or ''} {r['body']}")["compound"]
            out.append({"id": r["id"], "title": r["title"], "category": r["category"], "published": r["published_at"],
                        "words": len(words), "sentiment": score,
                        "label": "Positive" if score >= 0.05 else "Negative" if score <= -0.05 else "Neutral"})
        return out, freq.most_common(25)


class NewsApp:
    """Composition root, created once per process (st.cache_resource)."""
    def __init__(self):
        self.cfg = Settings()
        self.policy, self.kv, self.pool = Policy(), KV(self.cfg.redis_url), ThreadPoolExecutor(8)
        self.repo = Repository(self.cfg)
        self.repo.init_schema()
        self.short, self.vec = LinkShortener(), Vectorizer()
        self.tr = Translator(self.kv, self.cfg.libre_url, self.pool)
        self.alloc = AllocationManager(self.kv, self.policy)
        self.cart = CartService(self.repo, self.alloc, self.url)
        self.sec = SecurityService(self.cfg.desks, self.repo, self.policy)
        self.pub = Publisher(self.repo, self.policy, self.sec, self.vec, self.short)
        self.auth = AuthService(self.repo, self.kv, self.policy, self.cfg.editor_invite)
        self.news = Newsroom(self.repo, self.policy, self.pub, self.sec, self.short, self.cfg)
        self.poll = Polling(self.repo, self.policy)
        self.fin = Finance(self.repo, self.policy)
        self.hr = Hr(self.repo, self.policy, self.fin)
        self.analytics = NewsAnalytics()
        if self.repo.count() == 0:
            self._seed()

    def url(self, code): return f"{self.cfg.public_url}/?s={code}"

    def _seed(self):
        demo = [("National", "River towns brace as monsoon peaks", "Officials moved 40,000 people to shelters overnight."),
                ("World", "Climate talks open with a fight over finance", "Delegates split on who pays for adaptation."),
                ("Business", "Garment exporters see orders rebound", "Buyers are returning after a two-year slump."),
                ("Tech", "Village clinics try offline-first health apps", "Sync happens when a signal appears."),
                ("Sports", "Underdogs clinch the league on the last day", "A stoppage-time header decided it."),
                ("Culture", "Old Dhaka's printing presses find new readers", "Letterpress shops now sell to designers.")]
        for i, (cat, t, s) in enumerate(demo):
            self.pub.publish({"slug": f"demo-{i}-{cat.lower()}", "category": cat, "title": t, "summary": s,
                              "body": f"{s}\n\nThis is placeholder copy for the demo edition of {cat}.\n\nMore reporting to follow.",
                              "author": "Staff Reporter"})


@st.cache_resource(show_spinner="Starting newsroom…")
def get_app() -> NewsApp:
    return NewsApp()


# large shared caches: hot read paths never touch the DB per rerun
@st.cache_data(ttl=45, max_entries=2000, show_spinner=False)
def c_feed(): return get_app().repo.home_feed()
@st.cache_data(ttl=45, max_entries=2000, show_spinner=False)
def c_section(cat): return get_app().repo.section(cat)
@st.cache_data(ttl=300, max_entries=20000, show_spinner=False)
def c_article(aid): return get_app().repo.article(aid)
@st.cache_data(ttl=3600, max_entries=1500, show_spinner=False)
def c_image(aid): return get_app().repo.image(aid)
@st.cache_data(ttl=120, max_entries=5000, show_spinner=False)
def c_search(q): app = get_app(); return app.repo.search(app.vec.embed(q), q)
@st.cache_data(ttl=600, max_entries=8, show_spinner="Analysing stories…")
def c_analytics(days): app = get_app(); return app.analytics.analyze(app.repo.analytics_rows(days))


# ───────────────────────── UI ─────────────────────────
CSS = """<style>
html,body,[class*="css"]{font-family:'Source Sans 3','Segoe UI',Roboto,sans-serif}
h1,h2,h3,.hl{font-family:'Merriweather','Libre Baskerville',Georgia,serif;letter-spacing:-.01em}
.mast{text-align:center;border-bottom:3px double currentColor;padding:.3rem 0 .7rem;margin-bottom:.6rem}
.mast h1{font-size:2.8rem;margin:0;font-weight:900}.mast span{opacity:.65;font-size:.85rem}
.kicker{color:#c8102e;font-weight:700;font-size:.8rem}
.tag{background:#ffe8a3;color:#5a4300;padding:1px 6px;border-radius:2px;font-size:.75rem;font-weight:600}
.card{border-top:2px solid currentColor;padding:.5rem 0 .8rem}.card h3{font-size:1.15rem;line-height:1.3;margin:.2rem 0}
.lead h2{font-size:2.1rem;line-height:1.15}.meta{opacity:.65;font-size:.82rem}
.body p,.stMarkdown p{line-height:1.7}
</style>"""


class NewsUI:
    def __init__(self, app: NewsApp, uid: str):
        self.a, self.uid = app, uid

    def _open(self, row):
        st.query_params["s"] = row["short_code"]; st.rerun()

    def _card(self, r, lang, ctx, lead=False):
        t, s = self.a.tr.many([r["title"], r["summary"] or ""], lang)
        tag = f"<span class='tag'>Sponsored · {html.escape(r['sponsor'] or '')}</span> " if r["sponsored"] else ""
        st.markdown(f"<div class='card {'lead' if lead else ''}'><span class='kicker'>{html.escape(r['category'])}</span> {tag}"
                    f"<{'h2' if lead else 'h3'}>{html.escape(t)}</{'h2' if lead else 'h3'}>"
                    f"<div class='meta'>{html.escape(s)}</div></div>", unsafe_allow_html=True)
        if st.button("Read story", key=f"r{ctx}-{r['id']}"):
            self._open(r)

    def _grid(self, rows, lang, ctx):
        for i in range(0, len(rows), 3):
            for col, r in zip(st.columns(3, gap="large"), rows[i:i + 3]):
                with col:
                    self._card(r, lang, ctx)

    def home(self, lang):
        rows = c_feed()
        if not rows:
            return st.info("No stories yet. Publish one from the Remote desk tab.")
        lead, rest = rows[0], rows[1:]
        a, b = st.columns([2, 1], gap="large")
        with a:
            if img := c_image(lead["id"]):
                st.image(img, use_container_width=True)
            self._card(lead, lang, "home", lead=True)
        with b:
            for r in rest[:3]:
                self._card(r, lang, "home")
        st.divider()
        self._grid(rest[3:], lang, "home")

    def article(self, code, lang):
        aid = self.a.short.decode(code)
        r = c_article(aid) if aid else None
        if not r:
            return st.error("Story not found. Check the link or return to the home page.")
        title = self.a.tr.one(r["title"], lang)
        st.markdown(f"<span class='kicker'>{html.escape(r['category'])}</span>", unsafe_allow_html=True)
        st.title(title)
        st.caption(f"{r['author'] or 'Staff'} · {r['published_at']:%d %b %Y, %H:%M} · v{r['version']}")
        if r["sponsored"]:
            st.markdown(f"<span class='tag'>Sponsored · {html.escape(r['sponsor'] or '')}</span>", unsafe_allow_html=True)
        if img := c_image(aid):
            st.image(img, use_container_width=True)
        st.markdown(self.a.tr.body(r["body"], lang))
        link = self.a.url(r["short_code"])
        c1, c2, c3, c4 = st.columns(4)
        if c1.button("♡ Save to list", key="fav"):
            self._try(lambda: self.a.cart.add(self.uid, aid), "Saved to your list")
        with c2.popover("✂ Clip"):
            note = st.text_input("Note (optional)", key="clipnote")
            if st.button("Save clipping", key="clip"):
                self.a.repo.clip_add(self.uid, aid, note); st.toast("Clipping saved")
        with c3.popover("Share"):
            st.code(link, language=None)
            q = html.escape(title)
            st.link_button("WhatsApp", f"https://wa.me/?text={q}%20{link}")
            st.link_button("Facebook", f"https://www.facebook.com/sharer/sharer.php?u={link}")
            st.link_button("X", f"https://twitter.com/intent/tweet?url={link}&text={q}")
            st.link_button("Email", f"mailto:?subject={q}&body={link}")
        if c4.button("← Back", key="back"):
            st.query_params.clear(); st.rerun()

    @staticmethod
    def _try(fn, ok):
        try:
            fn(); st.toast(ok)
        except PolicyError as e:
            st.warning(str(e))

    def sidebar(self):
        a = self.a
        with st.sidebar:
            lang_name = st.selectbox("Language", list(Translator.LANGS), key="lang")
            q = st.text_input("Search")
            st.subheader("My reading list")
            items = a.cart.items(self.uid)
            if not items:
                st.caption("Nothing saved yet. Tap ♡ on any story.")
            for i in items:
                st.write(("📖 " if i["state"] == "checked_in" else "") + i["title"])
                x, y = st.columns(2)
                if i["state"] == "saved" and x.button("Check in", key=f"ci{i['id']}"):
                    self._try(lambda: a.cart.check_in(self.uid, i["id"]), "Moved to reading shelf"); st.rerun()
                if y.button("Remove", key=f"rm{i['id']}"):
                    a.cart.remove(self.uid, i["id"]); st.rerun()
            if items and st.button("Check out all", type="primary"):
                st.session_state["bundle"] = a.cart.check_out(self.uid); st.rerun()
            if b := st.session_state.get("bundle"):
                st.download_button("Download list (.md)", b, "reading-list.md")
            with st.expander("My clippings"):
                for c in a.repo.clip_list(self.uid):
                    st.markdown(f"[{c['title']}]({a.url(c['short_code'])})" + (f" — {c['note']}" if c["note"] else ""))
            if a.cfg.adsense_client:
                st.toggle("Allow non-personalised ads", key="ads", help="Off by default. We never track you.")
        return Translator.LANGS[lang_name], q

    # ---------- reporter portal ----------
    def _auth_box(self):
        if self.a.cfg.google and self._g().get("is_logged_in"):
            return self._google_finish()
        mode = st.radio("Account", ["Sign in", "Create account"], horizontal=True, label_visibility="collapsed", key="authmode")
        with st.form("auth"):
            email = st.text_input("Email")
            pw = st.text_input("Password", type="password")
            name = code = ""
            kind = "reporter"
            if mode == "Create account":
                kind = st.selectbox("I am a", ["Reporter", "Sponsor"]).lower()
                name = st.text_input("Full name (or sponsor / company name)")
                code = st.text_input("Editor invite code (editors only)", type="password")
            if st.form_submit_button(mode, type="primary"):
                try:
                    a = self.a.auth
                    st.session_state["user"] = a.register(email, name, pw, code, kind) if mode == "Create account" else a.login(email, pw)
                    st.rerun()
                except PolicyError as e:
                    st.error(str(e))
        if self.a.cfg.google:
            st.divider()
            if st.button("Continue with Google", key="g_login"):
                st.login()

    @staticmethod
    def _g() -> dict:
        try:
            return st.user.to_dict()
        except Exception:
            return {}

    def _google_finish(self):
        g, a = self._g(), self.a.auth
        email = (g.get("email") or "").lower()
        st.markdown(f"Signed in with Google as **{html.escape(email)}**")
        if g.get("email_verified") is False or not email:
            st.error("Google has not verified this email address.")
        else:
            existing = self.a.repo.user_get(email)
            with st.form("gform"):
                pw = st.text_input("Passcode" if existing else "Create a passcode (10+ characters)", type="password")
                kind, name, code = "reporter", g.get("name") or email.split("@")[0], ""
                if not existing:
                    kind = st.selectbox("I am a", ["Reporter", "Sponsor"]).lower()
                    name = st.text_input("Display name", value=name)
                    code = st.text_input("Editor invite code (editors only)", type="password")
                if st.form_submit_button("Unlock" if existing else "Create account", type="primary"):
                    try:
                        st.session_state["user"] = a.login(email, pw) if existing else a.register(email, name, pw, code, kind)
                        st.rerun()
                    except PolicyError as e:
                        st.error(str(e))
        if st.button("Use a different Google account", key="g_out"):
            st.logout()

    def reporter_portal(self):
        u = st.session_state.get("user")
        if not u:
            return self._auth_box()
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"Signed in as **{html.escape(u['name'])}** ({u['role']})")
        if c2.button("Sign out", key="signout"):
            google = bool(self.a.cfg.google and self._g().get("is_logged_in"))
            st.session_state.clear()
            st.logout() if google else st.rerun()
        t1, t2, t3 = st.tabs(["New story", "My stories", "Billing"])
        with t1:
            self._story_form(u)
        with t2:
            self._status_table(u["id"])
            mine = self.a.repo.subs_of(u["id"])
            if mine:
                tl = st.selectbox("Timeline for", mine, format_func=lambda r: r["title"], key="tlpick")
                self._timeline(tl["id"])
            back = [r for r in mine if r["status"] == "changes_requested"]
            if back:
                st.subheader("Returned for changes")
                pick = st.selectbox("Story", back, format_func=lambda r: r["title"], key="fixpick")
                st.info(pick["editor_note"] or "The editor asked for changes.")
                self._story_form(u, self.a.repo.sub_get(pick["id"]))
        with t3:
            self._billing(u)

    def _story_form(self, u, edit=None):
        cats = list(self.a.policy.categories)
        with st.form(f"sf{edit['id'] if edit else 'new'}", clear_on_submit=edit is None):
            cat = st.selectbox("Section", cats, index=cats.index(edit["category"]) if edit else 0)
            title = st.text_input("Headline", value=edit["title"] if edit else "", max_chars=140)
            summary = st.text_input("Summary (one line)", value=(edit["summary"] or "") if edit else "", max_chars=240)
            body = st.text_area("Story", value=edit["body"] if edit else "", height=280)
            photo = st.file_uploader("Photo (JPEG, PNG or WebP, up to 3 MB)", type=["jpg", "jpeg", "png", "webp"])
            if st.form_submit_button("Resubmit for review" if edit else "Submit for review", type="primary"):
                try:
                    art = {"category": cat, "title": title.strip(), "summary": summary.strip(), "body": body.strip(), "author": u["name"],
                           "sponsored": u["role"] == "sponsor", "sponsor": u["name"] if u["role"] == "sponsor" else None}
                    sid = self.a.news.submit(u, art, photo.getvalue() if photo else None, edit["id"] if edit else None)
                    st.success(f"Sent to the newsroom (#{sid}). Track it under My stories.")
                except PolicyError as e:
                    st.error(str(e))

    @st.fragment(run_every="10s")
    def _status_table(self, uid):
        rows = self.a.repo.subs_of(uid)
        if not rows:
            return st.info("No stories yet. Write one under New story.")
        cur = self.a.cfg.currency
        st.dataframe([{"Headline": r["title"], "Section": r["category"], "Status": STATUS[r["status"]],
                       "Editor note": r["editor_note"] or "", "Fee": f"{cur} {r['fee']:,.2f}" if r["status"] == "published" else "",
                       "Goes live": self._t(r["publish_at"]) if r["status"] in ("scheduled", "published") else "",
                       "Updated": self._t(r["updated_at"])} for r in rows],
                     use_container_width=True, hide_index=True)

    def _billing(self, u):
        cur, tx = self.a.cfg.currency, self.a.repo.tx_list(u["id"])
        due = sum((-t["amount"] if t["kind"] == "payout" else t["amount"]) for t in tx)
        st.metric("Balance due", f"{cur} {due:,.2f}")
        if not tx:
            return st.caption("Earnings appear here when an editor publishes your story.")
        st.dataframe([{"Date": t["created_at"].strftime("%d %b %Y"), "Type": t["kind"].title(), "Details": t["note"],
                       "Amount": f"{'-' if t['kind'] == 'payout' else ''}{t['amount']:,.2f}"} for t in tx],
                     use_container_width=True, hide_index=True)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "type", "details", f"amount_{cur}"])
        for t in tx:
            w.writerow([t["created_at"].date(), t["kind"], t["note"], t["amount"]])
        st.download_button("Download statement (CSV)", buf.getvalue(), "statement.csv", "text/csv")

    # ---------- newsroom (editors) ----------
    def newsroom(self):
        u = st.session_state.get("user")
        if not u:
            return st.info("Editors: sign in from the Reporter portal tab.")
        if u["role"] != "editor":
            return st.warning("The newsroom is for editors only.")
        if msg := st.session_state.pop("flash", None):
            st.success(msg)
        self._queue()
        self._scheduled(u)
        if (sid := st.session_state.get("review_id")):
            self._review(u, sid)
        with st.expander("Reporter payouts"):
            self._payouts()

    @st.fragment(run_every="5s")
    def _queue(self):
        if self.a.news.release_due():
            st.cache_data.clear()
        rows = self.a.repo.queue()
        late = sum(1 for r in rows if self._late(r))
        st.subheader(f"Live queue: {len(rows)} waiting" + (f", {late} overdue" if late else ""))
        if not rows:
            return st.caption("Nothing waiting. New stories appear here automatically.")
        now = datetime.now(timezone.utc)
        labels = {r["id"]: f"#{r['id']} {r['title']} ({r['category']}, {r['reporter'] or 'desk ' + str(r['desk'])}, "
                           f"{int((now - r['created_at']).total_seconds() // 60)} min ago{' · OVERDUE' if self._late(r) else ''})" for r in rows}
        pick = st.selectbox("Open a story to review", [None, *labels], key="pick",
                            format_func=lambda i: "Choose a story" if i is None else labels[i])
        if pick and pick != st.session_state.get("review_id"):
            st.session_state["review_id"] = pick
            st.rerun()

    def _review(self, ed, sid):
        s = self.a.repo.sub_get(sid)
        if not s or s["status"] not in ("submitted", "in_review"):
            st.session_state.pop("review_id", None)
            return st.info("That story has already been handled.")
        cfg, nr = self.a.cfg, self.a.news
        st.divider()
        st.subheader(s["title"])
        st.caption(f"{s['category']} · from {s['reporter'] or 'desk ' + str(s['desk'])}" + (" · SPONSORED" if s["sponsored"] else ""))
        with st.expander("Timeline"):
            self._timeline(sid)
        if img := self.a.repo.sub_image(sid):
            st.image(img, use_container_width=True)
        if s["summary"]:
            st.markdown(f"**{s['summary']}**")
        st.markdown(s["body"])
        st.markdown("**Verification checklist**")
        ticked = {k: st.checkbox(label, key=f"ck{sid}{k}") for k, label in self.a.policy.checks}
        fee = st.number_input(f"Fee for this story ({cfg.currency})", min_value=0.0, value=float(cfg.rate), step=5.0, key=f"fee{sid}")
        note = st.text_area("Note to reporter (required to return or reject)", key=f"nt{sid}")
        tz = ZoneInfo(cfg.tz)
        mode = st.radio("Go live", ["Now", "Schedule"], horizontal=True, key=f"wm{sid}")
        when = None
        if mode == "Schedule":
            d, t = st.columns(2)
            day = d.date_input(f"Date ({cfg.tz})", value=datetime.now(tz).date(), key=f"dt{sid}")
            clock = t.time_input("Time", value=(datetime.now(tz) + timedelta(minutes=30)).time().replace(second=0, microsecond=0),
                                 step=300, key=f"tm{sid}")
            when = datetime.combine(day, clock, tzinfo=tz)
        breaking = st.checkbox(f"Breaking news: skip the {self.a.policy.gap}-minute spacing between stories", key=f"bk{sid}")
        a, b, c = st.columns(3)
        try:
            if a.button("Publish now" if when is None else "Schedule", type="primary", key=f"pb{sid}"):
                go, code = nr.approve(sid, ed, ticked, fee, note, when, breaking)
                st.cache_data.clear()
                moved = " Moved later by the publishing rate limit." if go > (when or datetime.now(timezone.utc)) + timedelta(minutes=1) else ""
                st.session_state["flash"] = f"Published: {self.a.url(code)}" if code else f"Scheduled for {self._t(go)}.{moved}"
            elif b.button("Return for changes", key=f"rt{sid}"):
                nr.decide(sid, ed, "changes_requested", note)
                st.session_state["flash"] = "Returned to the reporter."
            elif c.button("Reject", key=f"rj{sid}"):
                nr.decide(sid, ed, "rejected", note)
                st.session_state["flash"] = "Story rejected."
            else:
                return
            st.session_state.pop("review_id", None)
            st.rerun()
        except PolicyError as e:
            st.error(str(e))

    def _t(self, dt):
        return dt.astimezone(ZoneInfo(self.a.cfg.tz)).strftime("%d %b %H:%M") if dt else ""

    def _late(self, r) -> bool:
        return (datetime.now(timezone.utc) - r["created_at"]).total_seconds() / 60 > self.a.policy.sla

    def _timeline(self, sid):
        st.dataframe([{"When": self._t(e["at"]), "Milestone": e["event"].replace("_", " ").title(),
                       "By": e["actor"] or "", "Details": e["detail"] or ""} for e in self.a.repo.ev_list(sid)],
                     use_container_width=True, hide_index=True)

    @st.fragment(run_every="15s")
    def _scheduled(self, ed):
        rows = self.a.repo.scheduled()
        st.subheader(f"Scheduled releases: {len(rows)}")
        if not rows:
            return st.caption("Nothing scheduled.")
        now = datetime.now(timezone.utc)
        st.dataframe([{"Goes live": self._t(r["publish_at"]), "In": f"{max(int((r['publish_at'] - now).total_seconds() // 60), 0)} min",
                       "Headline": r["title"], "Section": r["category"]} for r in rows],
                     use_container_width=True, hide_index=True)
        pick = st.selectbox("Pull a story back to review", [None, *rows], key="unpick",
                            format_func=lambda r: "Choose a story" if r is None else r["title"])
        if pick and st.button("Pull back", key="unsched"):
            self.a.news.unschedule(pick["id"], ed)
            st.rerun()

    def _payouts(self):
        cur, bal = self.a.cfg.currency, self.a.repo.balances()
        if not bal:
            return st.caption("No balances due.")
        who = st.selectbox("Reporter", bal, format_func=lambda b: f"{b['name']} ({b['email']}): {cur} {b['due']:,.2f}")
        amt = st.number_input(f"Amount paid ({cur})", min_value=0.0, value=float(who["due"]), step=10.0, key=f"pa{who['id']}")
        ref = st.text_input("Payment reference (bank or mobile wallet ID)", key=f"pr{who['id']}")
        if st.button("Record payout", key="payout"):
            try:
                self.a.news.payout(who["id"], amt, ref)
                st.success("Payout recorded.")
            except PolicyError as e:
                st.error(str(e))

    # ---------- archive ----------
    def archive(self):
        cur, tz = self.a.cfg.currency, ZoneInfo(self.a.cfg.tz)
        today = datetime.now(tz).date()
        c1, c2, c3, c4 = st.columns([2, 2, 1, 1])
        rng = c1.date_input("Date range", (today - timedelta(days=30), today), key="ar_d")
        cats = c2.multiselect("Category", list(self.a.policy.categories), key="ar_c")
        lo = c3.number_input(f"Min cost ({cur})", min_value=0.0, value=0.0, step=10.0, key="ar_lo")
        hi = c4.number_input(f"Max cost ({cur})", min_value=0.0, value=100000.0, step=10.0, key="ar_hi")
        if not isinstance(rng, tuple) or len(rng) != 2:
            return st.caption("Pick a start and an end date.")
        rows = self.a.repo.archive(rng[0], rng[1], cats)
        fees = self.a.repo.fees([r["id"] for r in rows])
        data = [{"Published": r["published_at"].astimezone(tz).strftime("%d %b %Y %H:%M"), "Section": r["category"],
                 "Headline": r["title"], "By": r["author"] or "Staff", "Sponsored": r["sponsored"],
                 "Cost": float(fees.get(r["id"], 0)), "Link": self.a.url(r["short_code"])} for r in rows]
        data = [d for d in data if lo <= d["Cost"] <= hi]
        st.caption(f"{len(data)} stories · total cost {cur} {sum(d['Cost'] for d in data):,.2f}")
        st.dataframe(data, use_container_width=True, hide_index=True,
                     column_config={"Link": st.column_config.LinkColumn("Link"), "Cost": st.column_config.NumberColumn(f"Cost ({cur})", format="%.2f")})

    # ---------- community poll ----------
    def community_poll(self):
        u = st.session_state.get("user")
        if not u or u["role"] not in ("reporter", "editor"):
            return st.info("Reporters and editors: sign in from the Reporter portal tab to propose edits and vote.")
        po, repo = self.a.poll, self.a.repo
        need, quorum = self.a.policy.poll_rules
        st.caption(f"An edit is applied automatically when {need:.0%} or more of the yes/no votes agree, with at least {quorum} votes. "
                   "Not interested does not count.")
        st.subheader("Open polls")
        props = repo.prop_open()
        names = {r["id"]: r["title"] for r in repo.subs_by_ids([p["submission_id"] for p in props])}
        if not props:
            st.caption("No open polls.")
        for p in props:
            t = repo.vote_tally(p["id"])
            n = t["yes"] + t["no"]
            with st.expander(f"#{p['id']} · edit to: {names.get(p['submission_id'], 'story')}"):
                st.markdown(f"**Proposed headline:** {p['title']}")
                if p["summary"]:
                    st.markdown(f"**Summary:** {p['summary']}")
                st.markdown(p["body"])
                st.caption(f"Reason: {p['reason'] or 'none given'}")
                st.progress(t["yes"] / n if n else 0.0, text=f"Yes {t['yes']} · No {t['no']} · Not interested {t['not_interested']}")
                for c in repo.vote_comments(p["id"]):
                    st.markdown(f"- **{c['vote'].replace('_', ' ')}:** {c['comment']}")
                with st.form(f"vf{p['id']}"):
                    v = st.radio("Your vote", ["Yes", "No", "Not interested"], horizontal=True)
                    cm = st.text_input("Comment")
                    if st.form_submit_button("Cast vote"):
                        try:
                            res = po.vote(u, p["id"], v.lower().replace(" ", "_"), cm)
                            st.success("Poll passed: the edit was applied." if res == "applied" else "Vote recorded.")
                        except PolicyError as e:
                            st.error(str(e))
        with st.expander("Propose an edit to a story that is not yet published"):
            subs = repo.open_subs()
            if not subs:
                return st.caption("No stories are waiting for publication.")
            pick = st.selectbox("Story", subs, format_func=lambda r: r["title"], key="pp")
            cur = repo.sub_get(pick["id"])
            with st.form(f"pf{pick['id']}", clear_on_submit=True):
                title = st.text_input("Headline", value=cur["title"])
                summary = st.text_input("Summary", value=cur["summary"] or "")
                body = st.text_area("Story", value=cur["body"], height=240)
                reason = st.text_input("Why should this change be made?")
                if st.form_submit_button("Start poll"):
                    try:
                        st.success(f"Poll #{po.propose(u, pick['id'], title, summary, body, reason)} is open.")
                    except PolicyError as e:
                        st.error(str(e))

    # ---------- staff areas (editors) ----------
    def _staff(self):
        u = st.session_state.get("user")
        if not u:
            st.info("Staff: sign in from the Reporter portal tab.")
        elif u["role"] != "editor":
            st.warning("This area is for editors only.")
        else:
            return u

    def _table(self, rows):
        st.dataframe([{"Line": l, f"Amount ({self.a.cfg.currency})": "" if v is None else f"{v:,.2f}"} for l, v in rows],
                     hide_index=True, use_container_width=True)

    def finance(self):
        if not self._staff():
            return
        cur, fin, repo = self.a.cfg.currency, self.a.fin, self.a.repo
        today = datetime.now(ZoneInfo(self.a.cfg.tz)).date()
        t1, t2, t3 = st.tabs(["Statements", "Budget", "Ledger"])
        with t1:
            rng = st.date_input("Period", (today.replace(day=1), today), key="fin_rng")
            if not isinstance(rng, tuple) or len(rng) != 2:
                return st.caption("Pick a start and an end date.")
            a, b = rng
            rev, exp, tr, te, ni = fin.income_statement(a, b)
            st.subheader("Income statement")
            self._table([("Revenue", None), *rev, ("Total revenue", tr), ("Expenses", None), *exp, ("Total expenses", te), ("Net income", ni)])
            cf = fin.cash_flow(a, b)
            st.subheader("Cash flow")
            self._table([("Opening cash", cf["opening"]), ("Cash received from income", cf["op_in"]),
                         ("Cash paid for expenses and reporter payouts", -cf["op_out"]),
                         ("Net operating cash flow", cf["op_in"] - cf["op_out"]), ("Owner capital less drawings", cf["fin"]),
                         ("Net change in cash", cf["net"]), ("Closing cash", cf["closing"])])
            bs = fin.balance_sheet(b)
            st.subheader(f"Balance sheet as of {b:%d %b %Y}")
            self._table([("Assets", None), ("Cash", bs["cash"]), ("Receivables (income not yet received)", bs["recv"]),
                         ("Total assets", bs["assets"]), ("Liabilities", None), ("Payables (bills not yet paid)", bs["payables"]),
                         ("Reporter fees owed", bs["reporter"]), ("Total liabilities", bs["liab"]), ("Equity", None),
                         ("Owner capital", bs["capital"]), ("Retained earnings", bs["retained"]), ("Total equity", bs["equity"])])
            st.caption("Assets equal liabilities plus equity." if bs["balanced"] else "Warning: the balance sheet does not balance.")
        with t2:
            period = st.text_input("Month (YYYY-MM)", value=today.strftime("%Y-%m"), key="bud_p")
            try:
                rows = fin.budget_vs_actual(period)
            except PolicyError as e:
                return st.error(str(e))
            df = pd.DataFrame(rows, columns=["Type", "Line", "Budget", "Actual", "Variance"])
            ed = st.data_editor(df, hide_index=True, use_container_width=True, disabled=["Type", "Line", "Actual", "Variance"],
                                key=f"bud{period}", column_config={"Budget": st.column_config.NumberColumn(min_value=0.0, format="%.2f")})
            if st.button("Save budget", key="bud_save"):
                fin.budget_save(period, {r.Line: float(r.Budget) for r in ed.itertuples()})
                st.success("Budget saved. Variance is positive when you are ahead of plan.")
        with t3:
            kind = st.radio("Entry type", ["income", "expense", "capital", "drawing"], horizontal=True, key="led_k", format_func=str.title)
            accounts = {"income": self.a.policy.income_accounts, "expense": self.a.policy.expense_accounts,
                        "capital": ("Owner capital",), "drawing": ("Owner drawings",)}[kind]
            with st.form("ledger"):
                c1, c2, c3 = st.columns(3)
                acct = c1.selectbox("Account", accounts)
                amt = c2.number_input(f"Amount ({cur})", min_value=0.0, step=10.0)
                day = c3.date_input("Date", today)
                settled = st.checkbox("Already received / paid", value=True)
                memo = st.text_input("Memo")
                if st.form_submit_button("Add entry", type="primary"):
                    try:
                        fin.add(kind, acct, amt, day, settled, memo)
                        st.success("Entry added.")
                    except PolicyError as e:
                        st.error(str(e))
            for r in repo.ledger_unsettled():
                c1, c2 = st.columns([5, 1])
                c1.write(f"{r['entry_date']:%d %b} · {r['kind'].title()} · {r['account']} · {cur} {r['amount']:,.2f} · {r['memo'] or ''}")
                if c2.button("Settle", key=f"st{r['id']}"):
                    repo.ledger_settle(r["id"]); st.rerun()
            st.subheader("Recent entries")
            st.dataframe([{"Date": r["entry_date"], "Type": r["kind"].title(), "Account": r["account"], "Amount": float(r["amount"]),
                           "Settled": r["settled"], "Memo": r["memo"]} for r in repo.ledger_recent()], hide_index=True, use_container_width=True)

    def hr(self):
        u = self._staff()
        if not u:
            return
        hr, repo, pol, cur = self.a.hr, self.a.repo, self.a.policy, self.a.cfg.currency
        today = datetime.now(ZoneInfo(self.a.cfg.tz)).date()
        emps = [{**e, "collar": c} for c in pol.collars for e in repo.emp_list(c)]
        label = lambda e: f"{e['name']} ({e['collar']}-collar, {e['department']})"
        t1, t2, t3, t4, t5 = st.tabs(["Employees", "Attendance", "Reviews", "Payroll", "Performance"])
        with t1:
            with st.form("emp_add", clear_on_submit=True):
                c1, c2 = st.columns(2)
                name, email = c1.text_input("Full name"), c2.text_input("Email")
                title, dept = c1.text_input("Job title"), c2.selectbox("Department", pol.departments)
                collar = c1.radio("Staff type", list(pol.collars), horizontal=True, format_func=lambda c:
                                  "White-collar (office, editorial)" if c == "white" else "Blue-collar (press, delivery, maintenance)")
                salary = c2.number_input(f"Monthly salary ({cur})", min_value=0.0, step=100.0)
                hired = c1.date_input("Hired on", today)
                if st.form_submit_button("Add employee", type="primary"):
                    try:
                        hr.add_employee(collar, name, email, title, dept, salary, hired)
                        st.success("Employee added.")
                    except PolicyError as e:
                        st.error(str(e))
            if emps:
                st.dataframe([{"Name": e["name"], "Type": e["collar"].title() + "-collar", "Title": e["title"], "Department": e["department"],
                               "Salary": float(e["salary"]), "Hired": e["hired_on"], "Status": e["status"].title()} for e in emps],
                             hide_index=True, use_container_width=True)
                pick = st.selectbox("Change status", emps, format_func=label, key="emp_pick")
                new = "left" if pick["status"] == "active" else "active"
                if st.button(f"Mark as {new}", key="emp_status"):
                    repo.emp_status(pick["collar"], pick["id"], new); st.rerun()
        with t2:
            collar = st.radio("Staff type", list(pol.collars), horizontal=True, key="at_c", format_func=str.title)
            day = st.date_input("Day", today, key="at_d")
            staff = [e for e in emps if e["collar"] == collar and e["status"] == "active"]
            if not staff:
                st.caption("No active staff of this type.")
            else:
                saved = repo.att_day(collar, day)
                df = pd.DataFrame({"Employee": [e["name"] for e in staff], "Status": [saved.get(e["id"], "present") for e in staff]})
                ed = st.data_editor(df, hide_index=True, disabled=["Employee"], key=f"att{collar}{day}",
                                    column_config={"Status": st.column_config.SelectboxColumn(options=list(pol.attendance_states), required=True)})
                if st.button("Save attendance", key="att_save"):
                    try:
                        hr.attendance(collar, day, [(e["id"], v) for e, v in zip(staff, ed["Status"])])
                        st.success("Attendance saved.")
                    except PolicyError as e:
                        st.error(str(e))
        with t3:
            active = [e for e in emps if e["status"] == "active"]
            if not active:
                st.caption("Add employees first.")
            else:
                with st.form("review"):
                    who = st.selectbox("Employee", active, format_func=label)
                    period = st.text_input("Period (YYYY-MM)", value=today.strftime("%Y-%m"))
                    cols = st.columns(4)
                    scores = [cols[i].slider(n, 1, 5, 3) for i, n in enumerate(["Quality", "Punctuality", "Teamwork", "Output"])]
                    note = st.text_input("Comment")
                    if st.form_submit_button("Save review", type="primary"):
                        try:
                            hr.review(who["collar"], who["id"], period, scores, note, u["name"])
                            st.success("Review saved.")
                        except PolicyError as e:
                            st.error(str(e))
        with t4:
            month = st.text_input("Month (YYYY-MM)", value=today.strftime("%Y-%m"), key="pay_m")
            st.metric("Monthly payroll (active staff)", f"{cur} {repo.payroll_total():,.2f}")
            if st.button("Post payroll to the ledger", key="pay_go"):
                try:
                    st.success(f"Payroll of {cur} {hr.run_payroll(month):,.2f} posted as a Salaries expense.")
                except PolicyError as e:
                    st.error(str(e))
        with t5:
            st.subheader("Employee performance (attendance over 30 days, average review score)")
            perf = pd.DataFrame(hr.performance())
            if perf.empty:
                st.caption("Add employees, attendance and reviews to see performance.")
            else:
                st.dataframe(perf, hide_index=True, use_container_width=True)
                rated = perf.dropna(subset=["Index"])
                if not rated.empty:
                    st.markdown("**Performance index (0 to 100)**")
                    st.bar_chart(rated.set_index("Employee")["Index"])
                    st.markdown("**Average index by department**")
                    st.bar_chart(rated.groupby("Department")["Index"].mean())
            st.subheader("Reporter productivity")
            rs = pd.DataFrame(repo.reporter_stats())
            if rs.empty:
                st.caption("No reporter activity yet.")
            else:
                rs["approval %"] = (rs["published"] / rs["submitted"] * 100).round(1)
                rs["hours_to_live"] = rs["hours_to_live"].astype(float).round(1)
                rs["earned"] = rs["earned"].astype(float)
                st.dataframe(rs, hide_index=True, use_container_width=True)
                st.bar_chart(rs.set_index("name")[["published", "rejected"]])

    def analytics(self):
        if not self._staff():
            return
        days = st.selectbox("Period", [7, 30, 90, 365], index=1, format_func=lambda d: f"Last {d} days", key="an_days")
        try:
            rows, kw = c_analytics(days)
        except Exception:
            return st.error("Text analytics is unavailable: the NLTK data could not be loaded.")
        if not rows:
            return st.info("No English stories in this period.")
        df = pd.DataFrame(rows)
        a, b, c, d = st.columns(4)
        a.metric("Stories", len(df)); b.metric("Average length (words)", int(df["words"].mean()))
        c.metric("Average sentiment", f"{df['sentiment'].mean():+.2f}"); d.metric("Reading time (min)", f"{df['words'].mean() / 230:.1f}")
        st.subheader("Average sentiment by section"); st.bar_chart(df.groupby("category")["sentiment"].mean())
        st.subheader("Stories per day"); st.line_chart(df.assign(day=pd.to_datetime(df["published"]).dt.date).groupby("day").size())
        st.subheader("Sentiment mix"); st.bar_chart(df["label"].value_counts())
        st.subheader("Top keywords"); st.bar_chart(pd.DataFrame(kw, columns=["word", "count"]).set_index("word"))
        cols, (l, r) = ["title", "category", "sentiment"], st.columns(2)
        l.markdown("**Most positive**"); l.dataframe(df.nlargest(5, "sentiment")[cols], hide_index=True)
        r.markdown("**Most negative**"); r.dataframe(df.nsmallest(5, "sentiment")[cols], hide_index=True)
        st.caption("NLTK VADER sentiment on English stories. Scores run from -1 (negative) to +1 (positive).")

    def remote_desk(self):
        st.caption("Reporters in the field seal stories offline with desk_client.py and paste or upload the package here.")
        tok = st.text_area("Sealed package", height=100)
        up = st.file_uploader("…or upload .dmpkg", type=["dmpkg"])
        if st.button("Verify and send to newsroom", type="primary"):
            try:
                token = up.getvalue().decode() if up else tok
                st.success(f"Package verified and queued for editorial review (#{self.a.news.submit_sealed(token)}).")
            except PolicyError as e:
                st.error(str(e))

    def ads(self):
        cid = self.a.cfg.adsense_client
        if cid and st.session_state.get("ads"):
            components.html(f"""<script>window.adsbygoogle=window.adsbygoogle||[];window.adsbygoogle.requestNonPersonalizedAds=1;</script>
            <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client={html.escape(cid)}" crossorigin="anonymous"></script>
            <ins class="adsbygoogle" style="display:block" data-ad-client="{html.escape(cid)}" data-ad-format="auto"></ins>
            <script>(adsbygoogle=window.adsbygoogle||[]).push({{}});</script>""", height=120)


FOOTER_CSS = """<style>
.site-footer{margin-top:3rem;border-radius:6px;overflow:hidden;font-family:'Source Sans 3','Segoe UI',Roboto,sans-serif;color:#d9d9d9}
.site-footer a{color:#8ab4ff;text-decoration:none}.site-footer a:hover{text-decoration:underline}
.ft-top{background:#444;padding:2.2rem 1rem 2rem;text-align:center}
.ft-top h4{color:#fff;font-size:1.15rem;margin:0 0 .9rem;font-weight:700}
.ft-top p{margin:.35rem 0;font-size:1rem;line-height:1.5}
.ft-social{display:flex;justify-content:center;flex-wrap:wrap;gap:.6rem;margin-top:1.3rem}
.ft-social a{width:2.6rem;height:2.6rem;border-radius:50%;display:flex;align-items:center;justify-content:center;
 color:#fff;font-weight:700;font-size:.95rem;text-decoration:none}
.ft-bottom{background:#2a2a2a;padding:1.1rem 1rem;text-align:center;font-size:.92rem;line-height:1.7}
</style>"""
FOOTER_SOCIAL = {"facebook": ("f", "#3b5998"), "x": ("X", "#000000"), "pinterest": ("P", "#bd081c"), "youtube": ("▶", "#c4302b"),
                 "linkedin": ("in", "#0e76a8"), "instagram": ("IG", "#6f42c1"), "news": ("G", "#5b7fe6")}


def footer_html(cfg, year: int) -> str:
    """Site footer in the style of a major newspaper: centred contacts and social links over a dark band,
    with copyright, address and masthead names beneath. Every item is optional."""
    e = html.escape
    rows = []
    phones = [x.strip() for x in cfg.footer_phone.split(",") if x.strip()]
    if phones:
        links = ", ".join('<a href="tel:%s">%s</a>' % (re.sub(r"[^0-9+]", "", x), e(x)) for x in phones)
        rows.append("Office: " + links)
    if cfg.footer_factcheck.strip():
        rows.append("Fake News Checker: " + e(cfg.footer_factcheck.strip()))
    mails = [x.strip() for x in cfg.footer_emails.split(",") if re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", x.strip())]
    if mails:
        rows.append(", ".join('<a href="mailto:%s">%s</a>' % (e(m), e(m)) for m in mails))
    try:
        social = json.loads(cfg.footer_social) if cfg.footer_social else {}
    except ValueError:
        social = {}
    icons = "".join(
        '<a href="%s" target="_blank" rel="noopener noreferrer" aria-label="%s" style="background:%s">%s</a>'
        % (e(url), e(name), FOOTER_SOCIAL[name][1], FOOTER_SOCIAL[name][0])
        for name, url in social.items() if name in FOOTER_SOCIAL and str(url).startswith("https://"))
    top = "<h4>Connect With Us</h4>" + "".join(f"<p>{r}</p>" for r in rows) + (f"<div class='ft-social'>{icons}</div>" if icons else "")
    since = f"{e(cfg.footer_since)}-{year}" if cfg.footer_since and cfg.footer_since != str(year) else str(year)
    lines = [f"Copyright © {since} {e(cfg.footer_company)}. All Rights Reserved."]
    place = ", ".join(x for x in (cfg.footer_address.strip(), cfg.footer_region.strip()) if x)
    if place:
        lines.append(e(place))
    names = [f"{label}: {e(v.strip())}" for label, v in (("Editor", cfg.footer_editor), ("Publisher", cfg.footer_publisher)) if v.strip()]
    if names:
        lines.append(", ".join(names))
    return f"{FOOTER_CSS}<footer class='site-footer'><div class='ft-top'>{top}</div><div class='ft-bottom'>{'<br>'.join(lines)}</div></footer>"


def user_id(secure: bool) -> str:
    """Cookie-backed anonymous id (random, no personal data). First visit falls back to session id."""
    uid = st.context.cookies.get("dm_uid", "")
    if not re.fullmatch(r"[0-9a-f]{32}", uid):
        uid = st.session_state.setdefault("uid", uuid.uuid4().hex)
        flag = ";Secure" if secure else ""
        components.html(f"<script>parent.document.cookie='dm_uid={uid};max-age=31536000;path=/;SameSite=Lax{flag}'</script>", height=0)
    return uid


def main():
    st.set_page_config(page_title="Meridian Daily", page_icon="📰", layout="wide")
    app = get_app()
    if app.kv.incr("tick", 10) == 1 and app.news.release_due():
        st.cache_data.clear()
    st.markdown(CSS + (f"<link rel='stylesheet' href='{html.escape(app.cfg.font_css_url)}'>" if app.cfg.font_css_url else ""),
                unsafe_allow_html=True)
    uid = user_id(app.cfg.cookie_secure)
    if not app.alloc.allow(uid):
        st.warning("Too many requests. Please wait a moment."); st.stop()
    ui = NewsUI(app, uid)
    lang, q = ui.sidebar()
    st.markdown("<div class='mast'><h1>Meridian Daily</h1><span>Independent · No tracking · Every language</span></div>",
                unsafe_allow_html=True)
    if code := st.query_params.get("s"):
        ui.article(code, lang)
        return st.markdown(footer_html(app.cfg, datetime.now().year), unsafe_allow_html=True)
    tabs = st.tabs(["Home", *app.policy.categories, "Archive", "Reporter portal", "Community poll", "Newsroom", "Finance", "HR", "Analytics", "Remote desk"])
    with tabs[0]:
        if q.strip():
            ui._grid(c_search(q.strip()[:200]), lang, "search")
        else:
            ui.home(lang)
    for t, cat in zip(tabs[1:1 + len(app.policy.categories)], app.policy.categories):
        with t:
            ui._grid(c_section(cat), lang, cat)
    with tabs[-8]:
        ui.archive()
    with tabs[-7]:
        ui.reporter_portal()
    with tabs[-6]:
        ui.community_poll()
    with tabs[-5]:
        ui.newsroom()
    with tabs[-4]:
        ui.finance()
    with tabs[-3]:
        ui.hr()
    with tabs[-2]:
        ui.analytics()
    with tabs[-1]:
        ui.remote_desk()
    ui.ads()
    st.markdown(footer_html(app.cfg, datetime.now().year), unsafe_allow_html=True)


if __name__ == "__main__":
    main()
