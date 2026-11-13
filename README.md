# Daily Flaws of Tomorrow (dFoT)

An independent online newspaper built with Python and Streamlit. It covers the whole path from a reporter's draft to a
published story: submission, editorial review, scheduling, publishing, reader tools, community polls, analytics, finance
and HR. It is private by default: no tracking and no ads unless a visitor opts in.

[![CI](https://github.com/rashadul-se/hotdick/actions/workflows/ci.yml/badge.svg)](https://github.com/rashadul-se/hotdick/actions/workflows/ci.yml)

Live site: <https://dailyfot.onrender.com> · Publisher: Rasha · Contact: info.dfot@gmail.com

---

## Contents
1. [Features](#features)
2. [How it is built](#how-it-is-built)
3. [Run it on your computer](#run-it-on-your-computer)
4. [Configuration](#configuration)
5. [Deploy on Render](#deploy-on-render)
6. [Roles and the publishing workflow](#roles-and-the-publishing-workflow)
7. [Offline reporting from remote areas](#offline-reporting-from-remote-areas)
8. [Privacy, consent, analytics and ads](#privacy-consent-analytics-and-ads)
9. [Security and DevSecOps](#security-and-devsecops)
10. [Testing](#testing)
11. [Project layout](#project-layout)
12. [Known limits](#known-limits)
13. [Contributing, licence and contact](#contributing-licence-and-contact)

---

## Features

**Readers** (no account needed)
- Home page with a lead story, section pages (National, World, Business, Sports, Tech, Culture, Opinion), a **Daily preview**
  by date, and an **Archive** filtered by date, section and cost.
- Search that combines meaning-based matching with keyword ranking.
- Eight languages (English, Bangla, Hindi, Urdu, Arabic, French, Spanish, Chinese), translated paragraph by paragraph and cached.
- A reading list (save, check in to a reading shelf, check out to a Markdown file), clippings with notes, and share links
  (short links plus WhatsApp, Facebook, X and email).
- Every story shows its location, recording and publication time, reading time, readability and sentiment scores, a
  Dewey Decimal code, a political-lean bar and a loaded-language level (see [Known limits](#known-limits)).

**Reporters and sponsors** (sign in with email, or with Google plus a passcode)
- Write a story, or **import one from a link** (headline, text, author, date, photo and source are collected for review).
- Track each story's status live, fix stories that were returned, and see earnings and payouts in a billing statement
  with a CSV download.
- Sponsors submit sponsored posts, which are always labelled "Sponsored".

**Editors**
- A live review queue with a verification checklist (sources, facts, media, right of reply, legal, rights), a review
  milestone, scheduled go-live times with a minimum gap between stories, and rejection or return with a note.
- Control over the political-lean bar shown with each story: keep the estimate, set your own percentages, or hide it.
- **Finance:** income statement, cash flow, balance sheet, monthly budget against actuals and a ledger.
- **HR:** employees (white-collar and blue-collar), attendance, reviews, payroll and a performance dashboard.
- **Analytics:** text analysis with NLTK and a political-lean heat map by section and by day or week.

**Community**
- Reporters and editors can propose an edit to a story that is not yet published. Others vote Yes, No or Not interested
  with a comment. The edit is applied automatically at 85% yes (of yes and no votes) with at least 3 votes.

## How it is built

```
 Readers / reporters / editors  ──►  Streamlit app (app.py)  ──►  four PostgreSQL databases
                                       │  ├─ consent.py        (privacy pages, consent, Google tags)
                                       │  ├─ lean.py           (political-lean estimator)
                                       │  ├─ observability.py  (JSON logs and audit trail)
                                       │  └─ Redis / Key Value (shared cache and rate limits, optional)
                                       └─►  hosted on Render, built with Podman or Render's builder
```

The code is layered: rules live in `Policy` (so the interface cannot bypass them), `Repository` is the only code that
talks to databases, and small services (`Newsroom`, `Finance`, `Hr`, `Polling`, `AuthService`, `Importer` and others) hold
the business logic. The page code in `NewsUI` only calls those services.

**Four database "zones"**, each with its own connection setting. If a zone is not set it falls back to `DATABASE_URL`,
so one database is enough to start.

| Zone | Holds |
|---|---|
| `white` | Published articles, finance ledger and budget, white-collar staff records |
| `blue` | Blue-collar staff records |
| `reporters` | Accounts, submissions, billing transactions, audit of review events |
| `community` | Favourites, clippings, polls and votes, consent records |

Zones only share ids, never foreign keys, so any of them can be moved to another server.

## Run it on your computer

You need Python 3.12 and PostgreSQL with the `pgvector` extension.

```bash
# 1. A database (Podman shown; Docker works the same)
podman run -d --name dfot-db -e POSTGRES_USER=news -e POSTGRES_PASSWORD=news -e POSTGRES_DB=news \
  -p 5432:5432 docker.io/pgvector/pgvector:pg16

# 2. The app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m nltk.downloader vader_lexicon stopwords

export DATABASE_URL=postgresql://news:news@localhost:5432/news
export PUBLIC_URL=http://localhost:8501
export COOKIE_SECURE=0            # needed on plain http
streamlit run app.py
```

The tables are created automatically on first start, and six demo stories are added to an empty database. To get an editor
account, set `EDITOR_INVITE_CODE` to any secret value, then choose **Create account** in the Reporter portal and enter it.

## Configuration

Everything is set with environment variables. Only `DATABASE_URL` and `PUBLIC_URL` are needed to start.

| Group | Variable | Default | Purpose |
|---|---|---|---|
| Core | `DATABASE_URL` | local Postgres | Database used by every zone that has no setting of its own |
| | `DATABASE_URL_WHITE`, `_BLUE`, `_REPORTERS`, `_COMMUNITY` | falls back to `DATABASE_URL` | One database per zone |
| | `PUBLIC_URL` | `http://localhost:8501` | Public address, used in share links and sign-in redirects |
| | `COOKIE_SECURE` | `1` | Set `0` only for local http |
| | `DB_POOL_MAX` | `20` | Maximum database connections per instance |
| | `REDIS_URL` | empty | Shared cache and rate limits across instances (in-memory if empty) |
| People | `EDITOR_INVITE_CODE` | empty | Secret code that creates editor accounts |
| | `DESK_KEYS` | `{}` | JSON of offline reporting desks, their keys and scopes |
| Newsroom | `NEWSROOM_TZ` | `Asia/Dhaka` | Time zone for scheduling and display |
| | `PUBLISH_GAP_MIN` | `15` | Minimum minutes between scheduled go-lives |
| | `REVIEW_SLA_MIN` | `60` | Minutes before a waiting story is marked overdue |
| | `SUBMITS_PER_HOUR` | `5` | Stories one reporter may submit per hour |
| | `RATE_PER_STORY`, `CURRENCY` | `20`, `BDT` | Default reporter fee and currency |
| Sign-in | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `AUTH_COOKIE_SECRET` | empty | Turn on Google sign-in |
| Footer | `FOOTER_COMPANY`, `FOOTER_EDITOR`, `FOOTER_PUBLISHER`, `FOOTER_REGION`, `FOOTER_ADDRESS`, `FOOTER_PHONE`, `FOOTER_FACTCHECK`, `FOOTER_EMAILS`, `FOOTER_SOCIAL`, `FOOTER_SINCE` | see `app.py` | Text and links in the footer; blank items are hidden |
| Privacy | `GA_MEASUREMENT_ID`, `ADSENSE_PUB_ID` | empty | Google Analytics and AdSense (only ever loaded after consent) |
| | `PRIVACY_CONTACT`, `PRIVACY_OFFICER`, `GOVERNING_LAW`, `CONSENT_VERSION`, `CONSENT_SALT`, `LEGAL_REVIEWED` | see `consent.py` | Legal pages and consent records |
| Tools | `LEAN_LEXICON`, `DEWEY_MODULE` | empty | Extra political-lean vocabulary; your own Dewey script |
| | `LIBRETRANSLATE_URL`, `LIBRETRANSLATE_KEY`, `FONT_CSS_URL` | empty | Self-hosted translation and fonts (avoids third parties) |
| Operations | `LOG_LEVEL`, `SENTRY_DSN` | `INFO`, empty | Logging and optional error tracking |

Leave `ADSENSE_CLIENT` empty: it is the old toggle that `ADSENSE_PUB_ID` replaces.

## Deploy on Render

1. Put every file in this repository at the top level of a GitHub repo.
2. In Render choose **New → Blueprint** and select the repo. `render.yaml` creates the web service, a key-value store and the
   databases. Delete the extra `DATABASE_URL_*` lines and databases in `render.yaml` if you want a single database.
3. Fill in the prompted values: `PUBLIC_URL`, `DESK_KEYS`, `EDITOR_INVITE_CODE`, and optionally the Google, footer and
   analytics settings. Generate desk keys and secrets with `python gen_env.py`.
4. After the first deploy, set `PUBLIC_URL` to the real address and save so share links and sign-in use it.
5. Create your editor account (see above), then open the **Newsroom**, **Finance**, **HR** and **Analytics** tabs.

**Google sign-in:** in Google Cloud Console create an OAuth client (Web application) with the redirect URI
`https://YOUR-SITE/oauth2callback`, then set `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

**Building the image with Podman instead:** `podman build -t dfot:1.0 -f Containerfile .` The image runs as a non-root user and
also works on OpenShift. `k8s.yaml` has sample Kubernetes manifests; sticky sessions are needed when running several instances.

## Roles and the publishing workflow

| Role | How to get it | Can do |
|---|---|---|
| Reader | Nothing | Read, search, translate, save, clip, share |
| Reporter | Create account | Submit and import stories, see status and billing, propose and vote on edits |
| Sponsor | Create account and choose Sponsor | Submit sponsored posts (labelled, never paid a fee) |
| Editor | Create account with the invite code | Review, schedule, publish, finance, HR, analytics |

**Story path:** submit → wait in the review queue → editor ticks the verification checklist → editor approves and picks a
go-live time (a minimum gap is kept between stories; breaking news can skip it) → the story is released on time → it can
still be corrected later. Editors can also return a story with a note, or reject it. Reporters can resubmit returned stories.

## Offline reporting from remote areas

A reporter with no signal seals a story and photo on their own computer, then sends the file by any channel:

```bash
DESK_ID=sylhet DESK_KEY=<desk key> python desk_client.py story.json photo.jpg > story.dmpkg
```

`story.json` holds `slug`, `category`, `title`, `summary`, `body`, `author`, and optionally `location` and `recorded_at`.
The newsroom pastes or uploads the package in the **Remote desk** tab. The package is encrypted and authenticated, so a
tampered, expired, replayed or out-of-scope package is rejected, and the photo's checksum must match.

## Privacy, consent, analytics and ads

- Nothing is loaded from Google until a visitor opts in. The banner gives equal weight to Accept and Reject, honours the
  Global Privacy Control signal, and keeps a pseudonymous record of the choice.
- Privacy Policy, Terms of Use and Cookie Notice are at `?page=privacy`, `?page=terms` and `?page=cookies`. They are
  **templates marked as draft** until you review them with a lawyer and set `LEGAL_REVIEWED=1`.
- For visitors in the EEA, UK and Switzerland, Google requires a Google-certified consent tool for AdSense. Turn on Google's
  own "European regulations" message in your AdSense account.
- This is not legal advice. Bangladesh's Personal Data Protection Ordinance, the GDPR and other Asian laws apply
  differently to different sites.

## Security and DevSecOps

| What | Where |
|---|---|
| Tests, secret scanning and policy checks on every change | `.github/workflows/ci.yml` |
| Code, dependency and image scans (report-only until `ENFORCE_SECURITY=true`) | same file, plus `security-weekly.yml` |
| Half-hourly smoke test of the live site | `.github/workflows/synthetic.yml`, `scripts/smoke_test.py` |
| Encrypted daily database backups and a restore drill | `backup.yml`, `scripts/backup.sh`, `scripts/restore_test.sh` |
| Repository policy rules (leaked keys, risky code, blueprint and container rules) | `scripts/check_repo_hygiene.py` |
| Structured logs and an audit trail with no personal text | `observability.py` |
| Incidents, rollback, restore and secret rotation | `docs/RUNBOOK.md` |
| Reporting a vulnerability | `SECURITY.md` |

Other built-in protections: passwords are stored as salted scrypt hashes, sign-in attempts are throttled, imports from links
cannot reach private networks, and signed-in actions are rate limited.

To report a vulnerability please email daily_fot@gmail.com instead of opening a public issue.

## Testing

```bash
pip install pytest pytest-cov
pytest -q                          # offline tests of the rules that matter most
python scripts/check_repo_hygiene.py
bash scripts/security_check.sh     # the same checks as CI, if the tools are installed
```

## Project layout

```
app.py               the web app and all services
lean.py              political-lean estimator (also runs alone: python lean.py story.txt)
consent.py           consent banner, privacy pages, Google Analytics and AdSense loading
legal_texts.py       Privacy Policy, Terms of Use and Cookie Notice templates
observability.py     JSON logging, audit trail, optional error tracking
desk_client.py       offline tool for reporters to seal stories
gen_env.py           generates desk keys and secrets
entrypoint.sh        starts the app and builds the Google sign-in settings
Containerfile        image build (Podman or Docker)
render.yaml          Render Blueprint
k8s.yaml             sample Kubernetes manifests
scripts/             policy checker, smoke test, backup and restore, local security check
tests/               pytest suite
docs/RUNBOOK.md      operations and incident guide
.github/             workflows, code owners, dependency updates, pull request template
```

## Known limits

- **Streamlit** keeps one live connection per reader, so very large audiences need many instances or static article pages
  in front. Streamlit cannot set security headers such as HSTS; put a proxy such as Cloudflare in front for those.
- **Search engines and AdSense** may not read Streamlit pages well because the text loads by script. Plan static pages if
  search traffic or ad income matters.
- **Political lean, sentiment, readability, loaded language and Dewey codes** are automatic estimates. The lean estimator is
  rule-based, English only, reflects Western left-right vocabulary, and is a rough guide that editors can override. They are
  not verdicts about any author or outlet.
- **Importing from a link** respects robots.txt and needs the submitter's confirmation of republishing rights. Pages that
  load their text by script or sit behind a paywall may not import.
- **Accounting** is simplified: "settled" means paid on the entry date, with no tax or depreciation.
- **Sessions:** a signed-in session ends when the page is reloaded.
- **Payouts** are bookkeeping entries. The app does not move money.

## Contributing, licence and contact

Open a pull request. CI must pass and the checklist in the pull request template should be complete. Please add or update
tests with any change to rules, money, privacy or security.

**Licence:** CC-BC-ND 4.0

**Contact:** info.dfot@gmail.com
