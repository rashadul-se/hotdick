"""Daily FoT - Streamlit newspaper (production skeleton).

Layers: Policy/Security (hidden rules) -> Repository (PostgreSQL+pgvector) ->
services (Shortener, Vectorizer, Translator, Cart, Allocation, Publisher) -> NewsUI.
Run:  streamlit run app.py   (env vars: see Settings)
"""
from __future__ import annotations

import base64, csv, hashlib, hmac, html, io, json, os, re, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    desks: dict = field(default_factory=lambda: json.loads(os.getenv("DESK_KEYS", "{}")))


class PolicyError(Exception):
    pass


STATUS = {"submitted": "Waiting for review", "in_review": "Being reviewed", "changes_requested": "Changes requested",
          "published": "Published", "rejected": "Not accepted"}


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

    @property
    def checks(self) -> tuple:
        return self.__CHECKS

    def can_publish(self, ticked: dict) -> bool:
        return all(ticked.get(k) for k, _ in self.__CHECKS)

    def can_submit(self, pending: int) -> bool:
        return pending < self.__MAX_PENDING

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
  user_id TEXT, article_id BIGINT REFERENCES articles(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'saved' CHECK (state IN ('saved','checked_in','checked_out')),
  updated_at TIMESTAMPTZ DEFAULT now(), PRIMARY KEY(user_id, article_id));
CREATE TABLE IF NOT EXISTS clippings(
  id BIGSERIAL PRIMARY KEY, user_id TEXT, article_id BIGINT REFERENCES articles(id) ON DELETE CASCADE,
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
"""
LIST_COLS = "id, short_code, slug, category, title, summary, author, sponsored, sponsor, published_at, version"


class Repository:
    def __init__(self, dsn: str, pool_max: int):
        self._pool = ConnectionPool(dsn, min_size=2, max_size=pool_max, open=True,
                                    kwargs={"row_factory": dict_row}, check=ConnectionPool.check_connection)

    def _q(self, sql, params=(), one=False):
        with self._pool.connection() as c:
            cur = c.execute(sql, params)
            if cur.description is None:
                return None
            return cur.fetchone() if one else cur.fetchall()

    def init_schema(self):
        with self._pool.connection() as c:
            c.execute("SELECT pg_advisory_xact_lock(884422)")   # safe with many pods booting
            c.execute(SCHEMA)

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
        return self._q("""INSERT INTO submissions(user_id,desk,slug,category,title,summary,body,author,image,image_sha256)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                       (uid, desk, a["slug"], a["category"], a["title"], a.get("summary"), a["body"], a.get("author"), img, sha),
                       one=True)["id"]

    def sub_update(self, sid, uid, a, img, sha):
        return self._q("""UPDATE submissions SET category=%s,title=%s,summary=%s,body=%s,image=COALESCE(%s,image),
            image_sha256=COALESCE(%s,image_sha256),status='submitted',editor_note=NULL,updated_at=now()
            WHERE id=%s AND user_id=%s AND status='changes_requested' RETURNING id""",
                       (a["category"], a["title"], a.get("summary"), a["body"], img, sha, sid, uid), one=True)

    def sub_pending(self, uid) -> int:
        return self._q("SELECT count(*) n FROM submissions WHERE user_id=%s AND status IN "
                       "('submitted','in_review','changes_requested')", (uid,), one=True)["n"]

    def subs_of(self, uid):
        return self._q("SELECT id,title,category,status,editor_note,fee,updated_at FROM submissions "
                       "WHERE user_id=%s ORDER BY created_at DESC LIMIT 100", (uid,))

    def queue(self):
        return self._q("""SELECT s.id,s.title,s.category,s.status,s.desk,s.created_at,u.name AS reporter
            FROM submissions s LEFT JOIN users u ON u.id=s.user_id
            WHERE s.status IN ('submitted','in_review') ORDER BY s.created_at LIMIT 200""")

    def sub_get(self, sid):
        return self._q("""SELECT s.id,s.user_id,s.desk,s.slug,s.category,s.title,s.summary,s.body,s.author,s.status,
            s.editor_note,u.name AS reporter FROM submissions s LEFT JOIN users u ON u.id=s.user_id WHERE s.id=%s""",
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

    # favourites / cart
    def fav_set(self, uid, aid, state):
        self._q("""INSERT INTO favorites(user_id,article_id,state) VALUES(%s,%s,%s)
                   ON CONFLICT(user_id,article_id) DO UPDATE SET state=EXCLUDED.state, updated_at=now()""", (uid, aid, state))

    def fav_del(self, uid, aid):
        self._q("DELETE FROM favorites WHERE user_id=%s AND article_id=%s", (uid, aid))

    def fav_list(self, uid):
        return self._q("""SELECT a.id, a.short_code, a.title, f.state FROM favorites f JOIN articles a ON a.id=f.article_id
                          WHERE f.user_id=%s AND f.state<>'checked_out' ORDER BY f.updated_at DESC""", (uid,))

    def clip_add(self, uid, aid, note):
        self._q("INSERT INTO clippings(user_id,article_id,note) VALUES(%s,%s,%s)", (uid, aid, note[:500]))

    def clip_list(self, uid):
        return self._q("""SELECT c.id, c.note, a.short_code, a.title FROM clippings c JOIN articles a ON a.id=c.article_id
                          WHERE c.user_id=%s ORDER BY c.created_at DESC LIMIT 100""", (uid,))


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

    def register(self, email, name, pw, code="") -> dict:
        email = email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not name.strip():
            raise PolicyError("Enter your full name and a valid email address")
        if not self._policy.password_ok(pw):
            raise PolicyError("Use a password of at least 10 characters")
        role = "editor" if self._invite and hmac.compare_digest(code, self._invite) else "reporter"
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
            return sid
        if not self._p.can_submit(self._r.sub_pending(user["id"])):
            raise PolicyError("You have too many stories waiting. Wait for a decision first.")
        return self._r.sub_add(user["id"], None, art, img, sha)

    def submit_sealed(self, token: str) -> int:
        art, img = self._sec.open(token)
        self._p.check_article(art)
        sha = hashlib.sha256(img).hexdigest() if img else None
        return self._r.sub_add(None, token.partition(".")[0], art, img, sha)

    def publish(self, sid, editor, checks, fee, note) -> str:
        if not self._p.can_publish(checks):
            raise PolicyError("Tick every verification check before publishing")
        if not self._r.sub_claim(sid, editor["id"]):
            raise PolicyError("This story was already handled by another editor")
        s = self._r.sub_get(sid)
        art = {k: s[k] for k in ("slug", "category", "title", "summary", "body", "author")}
        code = self._pub.publish({**art, "lang": "en"}, self._r.sub_image(sid))
        row = self._r.sub_done(sid, self._sh.decode(code), fee, checks, note)
        if row and row["user_id"] and fee > 0:
            self._r.tx_add(row["user_id"], sid, "earning", fee, "Published: " + s["title"][:80])
        return code

    def decide(self, sid, editor, status, note):
        if status not in ("changes_requested", "rejected"):
            raise PolicyError("Unknown decision")
        if not note.strip():
            raise PolicyError("Add a note so the reporter knows why")
        if not self._r.sub_set(sid, status, note.strip(), editor["id"]):
            raise PolicyError("This story was already handled")

    def payout(self, uid, amount, ref):
        due = next((b["due"] for b in self._r.balances() if b["id"] == uid), 0)
        if amount <= 0 or amount > due:
            raise PolicyError("Payout must be above zero and not more than the balance due")
        self._r.tx_add(uid, None, "payout", amount, ref or "Payout")


class NewsApp:
    """Composition root, created once per process (st.cache_resource)."""
    def __init__(self):
        self.cfg = Settings()
        self.policy, self.kv, self.pool = Policy(), KV(self.cfg.redis_url), ThreadPoolExecutor(8)
        self.repo = Repository(self.cfg.dsn, self.cfg.pool_max)
        self.repo.init_schema()
        self.short, self.vec = LinkShortener(), Vectorizer()
        self.tr = Translator(self.kv, self.cfg.libre_url, self.pool)
        self.alloc = AllocationManager(self.kv, self.policy)
        self.cart = CartService(self.repo, self.alloc, self.url)
        self.sec = SecurityService(self.cfg.desks, self.repo, self.policy)
        self.pub = Publisher(self.repo, self.policy, self.sec, self.vec, self.short)
        self.auth = AuthService(self.repo, self.kv, self.policy, self.cfg.editor_invite)
        self.news = Newsroom(self.repo, self.policy, self.pub, self.sec, self.short, self.cfg)
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
        mode = st.radio("Account", ["Sign in", "Create account"], horizontal=True, label_visibility="collapsed", key="authmode")
        with st.form("auth"):
            email = st.text_input("Email")
            pw = st.text_input("Password", type="password")
            name = code = ""
            if mode == "Create account":
                name = st.text_input("Full name")
                code = st.text_input("Editor invite code (editors only)", type="password")
            if st.form_submit_button(mode, type="primary"):
                try:
                    a = self.a.auth
                    st.session_state["user"] = a.register(email, name, pw, code) if mode == "Create account" else a.login(email, pw)
                    st.rerun()
                except PolicyError as e:
                    st.error(str(e))

    def reporter_portal(self):
        u = st.session_state.get("user")
        if not u:
            return self._auth_box()
        c1, c2 = st.columns([4, 1])
        c1.markdown(f"Signed in as **{html.escape(u['name'])}** ({u['role']})")
        if c2.button("Sign out", key="signout"):
            st.session_state.clear(); st.rerun()
        t1, t2, t3 = st.tabs(["New story", "My stories", "Billing"])
        with t1:
            self._story_form(u)
        with t2:
            self._status_table(u["id"])
            back = [r for r in self.a.repo.subs_of(u["id"]) if r["status"] == "changes_requested"]
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
                    art = {"category": cat, "title": title.strip(), "summary": summary.strip(), "body": body.strip(), "author": u["name"]}
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
                       "Updated": r["updated_at"].strftime("%d %b %H:%M")} for r in rows],
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
        if (sid := st.session_state.get("review_id")):
            self._review(u, sid)
        with st.expander("Reporter payouts"):
            self._payouts()

    @st.fragment(run_every="5s")
    def _queue(self):
        rows = self.a.repo.queue()
        st.subheader(f"Live queue: {len(rows)} waiting")
        if not rows:
            return st.caption("Nothing waiting. New stories appear here automatically.")
        now = datetime.now(timezone.utc)
        labels = {r["id"]: f"#{r['id']} {r['title']} ({r['category']}, {r['reporter'] or 'desk ' + str(r['desk'])}, "
                           f"{int((now - r['created_at']).total_seconds() // 60)} min ago)" for r in rows}
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
        st.caption(f"{s['category']} · from {s['reporter'] or 'desk ' + str(s['desk'])}")
        if img := self.a.repo.sub_image(sid):
            st.image(img, use_container_width=True)
        if s["summary"]:
            st.markdown(f"**{s['summary']}**")
        st.markdown(s["body"])
        st.markdown("**Verification checklist**")
        ticked = {k: st.checkbox(label, key=f"ck{sid}{k}") for k, label in self.a.policy.checks}
        fee = st.number_input(f"Fee for this story ({cfg.currency})", min_value=0.0, value=float(cfg.rate), step=5.0, key=f"fee{sid}")
        note = st.text_area("Note to reporter (required to return or reject)", key=f"nt{sid}")
        a, b, c = st.columns(3)
        try:
            if a.button("Publish now", type="primary", key=f"pb{sid}"):
                code = nr.publish(sid, ed, ticked, fee, note)
                st.cache_data.clear()
                st.session_state["flash"] = f"Published: {self.a.url(code)}"
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


def user_id(secure: bool) -> str:
    """Cookie-backed anonymous id (random, no personal data). First visit falls back to session id."""
    uid = st.context.cookies.get("dm_uid", "")
    if not re.fullmatch(r"[0-9a-f]{32}", uid):
        uid = st.session_state.setdefault("uid", uuid.uuid4().hex)
        flag = ";Secure" if secure else ""
        components.html(f"<script>parent.document.cookie='dm_uid={uid};max-age=31536000;path=/;SameSite=Lax{flag}'</script>", height=0)
    return uid


def main():
    st.set_page_config(page_title="Daily Flaws of Tomorrow (DFoT)", page_icon="📰", layout="wide")
    app = get_app()
    st.markdown(CSS + (f"<link rel='stylesheet' href='{html.escape(app.cfg.font_css_url)}'>" if app.cfg.font_css_url else ""),
                unsafe_allow_html=True)
    uid = user_id(app.cfg.cookie_secure)
    if not app.alloc.allow(uid):
        st.warning("Too many requests. Please wait a moment."); st.stop()
    ui = NewsUI(app, uid)
    lang, q = ui.sidebar()
    st.markdown("<div class='mast'><h1>Daily FoT</h1><span>Localized · Premium · All about personalization</span></div>",
                unsafe_allow_html=True)
    if code := st.query_params.get("s"):
        return ui.article(code, lang)
    tabs = st.tabs(["Home", *app.policy.categories, "Reporter portal", "Newsroom", "Remote desk"])
    with tabs[0]:
        if q.strip():
            ui._grid(c_search(q.strip()[:200]), lang, "search")
        else:
            ui.home(lang)
    for t, cat in zip(tabs[1:1 + len(app.policy.categories)], app.policy.categories):
        with t:
            ui._grid(c_section(cat), lang, cat)
    with tabs[-3]:
        ui.reporter_portal()
    with tabs[-2]:
        ui.newsroom()
    with tabs[-1]:
        ui.remote_desk()
    ui.ads()


if __name__ == "__main__":
    main()
