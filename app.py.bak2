"""Meridian Daily - Streamlit newspaper (production skeleton).

Layers: Policy/Security (hidden rules) -> Repository (PostgreSQL+pgvector) ->
services (Shortener, Vectorizer, Translator, Cart, Allocation, Publisher) -> NewsUI.
Run:  streamlit run app.py   (env vars: see Settings)
"""
from __future__ import annotations

import base64, hashlib, html, io, json, os, re, threading, time, uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

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
    desks: dict = field(default_factory=lambda: json.loads(os.getenv("DESK_KEYS", "{}")))


class PolicyError(Exception):
    pass


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

    @staticmethod
    def scope_ok(scopes: list, category: str) -> bool:
        return "publish:*" in scopes or f"publish:{category.lower()}" in scopes


# ───────────────────────── cache / shared state ─────────────────────────
class KV:
    """Redis when REDIS_URL is set (shared by all pods), else bounded in-process TTL map."""
    def __init__(self, url: str):
        self._r, self._d, self._lock = None, {}, threading.Lock()
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
        self.pub = Publisher(self.repo, self.policy, SecurityService(self.cfg.desks, self.repo, self.policy), self.vec, self.short)
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

    def _card(self, r, lang, lead=False):
        t, s = self.a.tr.many([r["title"], r["summary"] or ""], lang)
        tag = f"<span class='tag'>Sponsored · {html.escape(r['sponsor'] or '')}</span> " if r["sponsored"] else ""
        st.markdown(f"<div class='card {'lead' if lead else ''}'><span class='kicker'>{html.escape(r['category'])}</span> {tag}"
                    f"<{'h2' if lead else 'h3'}>{html.escape(t)}</{'h2' if lead else 'h3'}>"
                    f"<div class='meta'>{html.escape(s)}</div></div>", unsafe_allow_html=True)
        if st.button("Read story", key=f"r{r['id']}{lead}"):
            self._open(r)

    def _grid(self, rows, lang):
        for i in range(0, len(rows), 3):
            for col, r in zip(st.columns(3, gap="large"), rows[i:i + 3]):
                with col:
                    self._card(r, lang)

    def home(self, lang):
        rows = c_feed()
        if not rows:
            return st.info("No stories yet. Publish one from the Remote desk tab.")
        lead, rest = rows[0], rows[1:]
        a, b = st.columns([2, 1], gap="large")
        with a:
            if img := c_image(lead["id"]):
                st.image(img, use_container_width=True)
            self._card(lead, lang, lead=True)
        with b:
            for r in rest[:3]:
                self._card(r, lang)
        st.divider()
        self._grid(rest[3:], lang)

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

    def remote_desk(self):
        st.caption("Reporters in the field seal stories offline with desk_client.py and paste or upload the package here.")
        tok = st.text_area("Sealed package", height=100)
        up = st.file_uploader("…or upload .dmpkg", type=["dmpkg"])
        if st.button("Verify and publish", type="primary"):
            try:
                token = up.getvalue().decode() if up else tok
                st.success(f"Published: {self.a.url(self.a.pub.publish_sealed(token))}")
                st.cache_data.clear()
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
    st.set_page_config(page_title="Meridian Daily", page_icon="📰", layout="wide")
    app = get_app()
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
        return ui.article(code, lang)
    tabs = st.tabs(["Home", *app.policy.categories, "Remote desk"])
    with tabs[0]:
        if q.strip():
            ui._grid(c_search(q.strip()[:200]), lang)
        else:
            ui.home(lang)
    for t, cat in zip(tabs[1:-1], app.policy.categories):
        with t:
            ui._grid(c_section(cat), lang)
    with tabs[-1]:
        ui.remote_desk()
    ui.ads()


if __name__ == "__main__":
    main()
