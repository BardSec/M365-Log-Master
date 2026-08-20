# M365 Log Master

A self-hosted MVP that ingests **Microsoft 365 sign-in logs** from Microsoft Graph, stores them in a local PostgreSQL database, and provides a fast web UI for search and anomaly detection.

---

## Table of Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Azure AD App Registration](#azure-ad-app-registration)
4. [Quick Start (Docker Compose)](#quick-start-docker-compose)
5. [Environment Variables](#environment-variables)
6. [Pages & API Endpoints](#pages--api-endpoints)
7. [Anomaly Heuristics](#anomaly-heuristics)
8. [Running Tests](#running-tests)
9. [Development Notes & Assumptions](#development-notes--assumptions)
10. [Security Notes](#security-notes)
 
---

## Features

- **Incremental sync** from Microsoft Graph `/beta/auditLogs/signIns` using an OData `$filter` cursor – fetches only new records each hour.
- **All four sign-in event types** – interactive user, non-interactive user, service principal, and managed identity. Dashboard has a button-group filter to view any combination.
- **Idempotent upserts** – `ON CONFLICT DO UPDATE` on the Graph event `id` field.
- **Exponential-backoff retry** for HTTP 429 rate-limit and 5xx transient errors.
- **PostgreSQL** with trigram GIN indexes for sub-300 ms keyword search.
- **Dashboard** with Chart.js timeline, top users/IPs by failures, and anomaly table. Anomalies load async with a 5-min TTL cache so the shell paints in ~1 second.
- **Four anomaly heuristics** (new IP, unfamiliar country, impossible travel, repeated CA failures) – all run as SQL against the local DB, scoped to interactive user sign-ins for signal-to-noise.
- **Cloudflare R2 archive** – nightly sweep at 03:15 UTC offloads sign-in events older than 35 days to R2 as gzipped JSON Lines (`signins/YYYY/MM/YYYY-MM-DD.jsonl.gz`), then deletes from Postgres. Postgres stays bounded; full year+ history lives in R2 for pennies/month.
- **Browse archive** UI at `/admin/archive` — list of archived days, paginated day view fetched on demand from R2, click-through to event detail.
- **Cross-archive search** at `/admin/archive/search` — backed by a local DuckDB index built from R2 archives. Sub-second queries across the full archive, with filters for UPN / IP / app / country / error code / result / type / date range.
- **CSV export** of any archive search result, streamed via DuckDB `fetchmany` to keep memory flat for arbitrarily large exports.
- **`/admin/sync-status`** page with Sync Now button and R2 archive status panel (Archive Now, Rebuild Index buttons).
- **Docker Compose** one-liner startup; Alembic runs migrations automatically on container start.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Docker Compose                                                          │
│                                                                          │
│  ┌────────────────────────────────────┐    ┌────────────────────────┐    │
│  │  web (Flask + APScheduler)         │───▶│  db (PostgreSQL 16)    │    │
│  │    - hourly sync (Graph → PG)      │    │  35-day hot window     │    │
│  │    - nightly archive (PG → R2)     │    └────────────────────────┘    │
│  │    - DuckDB index file (/app/data) │                                  │
│  └─────┬────────────────────┬─────────┘                                  │
│        │ Graph API           │ S3 API                                    │
│        ▼                     ▼                                           │
│  Microsoft Graph        Cloudflare R2                                    │
│  /beta/auditLogs/       signins/YYYY/MM/                                 │
│  signIns (all 4 types)  YYYY-MM-DD.jsonl.gz                              │
└──────────────────────────────────────────────────────────────────────────┘
```

**Project layout**

```
M365-Log-Master/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── config.py            # Environment-based configuration
│   ├── extensions.py        # SQLAlchemy engine / session
│   ├── models.py            # ORM models (SignInEvent, ArchiveLog, SyncCursor, SyncLog)
│   ├── graph_client.py      # MSAL auth + Graph API pagination + retry (uses /beta)
│   ├── sync_service.py      # Incremental sync logic (cursor, upsert, logging)
│   ├── queries.py           # Dashboard metrics + anomaly SQL (with TTL cache)
│   ├── archive_service.py   # R2 archive upload + Postgres purge + browse
│   ├── archive_index.py     # DuckDB-backed cross-archive search index
│   ├── routes.py            # Server-rendered HTML pages
│   ├── api.py               # JSON API endpoints
│   ├── scheduler.py         # APScheduler: hourly sync + nightly archive sweep
│   ├── auth.py              # Optional HTTP Basic Auth middleware
│   ├── templates/
│   │   ├── base.html
│   │   ├── search.html
│   │   ├── dashboard.html
│   │   ├── event_detail.html
│   │   ├── sync_status.html
│   │   ├── archive_list.html
│   │   ├── archive_day.html
│   │   ├── archive_search.html
│   │   └── 404.html
│   └── static/{css,js}/
├── migrations/versions/
│   ├── 001_initial_schema.py
│   ├── 002_signin_event_type.py
│   ├── 003_archive_log.py
│   └── 004_anomaly_composite_index.py
├── tests/
│   ├── conftest.py
│   ├── test_graph_client.py
│   ├── test_sync_service.py
│   ├── test_queries.py
│   ├── test_archive_service.py
│   └── test_archive_index.py
├── data/                    # mounted volume; holds archive_index.duckdb
├── docker/
│   ├── Dockerfile
│   └── entrypoint.sh
├── docker-compose.yml
├── alembic.ini
├── requirements.txt
├── Makefile
└── README.md
```

---

## Azure AD App Registration

Follow these steps **once** to create the service principal used by M365 Log Master.

### 1. Register the application

1. Go to [Azure Portal → Azure Active Directory → App registrations](https://portal.azure.com/#blade/Microsoft_AAD_IAM/ActiveDirectoryMenuBlade/RegisteredApps).
2. Click **New registration**.
3. Name it (e.g. `M365LogMaster`), choose **Accounts in this organisational directory only**, then **Register**.
4. Note the **Application (client) ID** and **Directory (tenant) ID** – these become `CLIENT_ID` and `TENANT_ID`.

### 2. Create a client secret

1. In the app → **Certificates & secrets** → **New client secret**.
2. Choose an expiry and click **Add**.
3. **Copy the secret value immediately** – it is only shown once. This is `CLIENT_SECRET`.

### 3. Grant API permissions

1. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**.
2. Add:
   - `AuditLog.Read.All` – read sign-in logs.
   - `Directory.Read.All` – resolve user/group display names if needed.
3. Click **Grant admin consent** (requires a Global Administrator).

> **Assumption**: `AuditLog.Read.All` (application permission) is sufficient. Delegated permissions would require a signed-in user and are not compatible with the client-credentials flow used here.

### 4. Verify

Run a quick test (requires `requests` and your credentials):

```bash
TOKEN=$(curl -s -X POST \
  "https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/token" \
  -d "grant_type=client_credentials&client_id=${CLIENT_ID}&client_secret=${CLIENT_SECRET}&scope=https://graph.microsoft.com/.default" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl -s -H "Authorization: Bearer $TOKEN" \
  "https://graph.microsoft.com/v1.0/auditLogs/signIns?\$top=1" | python3 -m json.tool
```

You should see a JSON object with a `value` array.

---

## Quick Start (Docker Compose)

### Prerequisites

- Docker ≥ 24 and Docker Compose v2.
- An Azure AD app registration (see above).

### Steps

```bash
# 1. Clone
git clone <repo-url> M365-Log-Master
cd M365-Log-Master

# 2. Configure credentials
cp .env.example .env
$EDITOR .env   # fill in TENANT_ID, CLIENT_ID, CLIENT_SECRET

# 3. Start everything (builds image, runs migrations, starts web + db)
docker compose up --build

# 4. Open the dashboard
open http://localhost:8080
```

The first time the container starts, Alembic automatically creates all tables and indexes (including `pg_trgm`).

The **first sync** back-fills the last 24 hours. Subsequent syncs run **hourly** and fetch only records since the last cursor time (minus a 5-minute lookback overlap).

### Trigger a manual sync

```bash
make sync-now
# or:
curl -X POST http://localhost:8080/api/sync-now
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TENANT_ID` | *(required)* | Azure AD tenant / directory ID |
| `CLIENT_ID` | *(required)* | App registration client ID |
| `CLIENT_SECRET` | *(required)* | App registration client secret |
| `GRAPH_SCOPE` | `https://graph.microsoft.com/.default` | OAuth scope |
| `DATABASE_URL` | `postgresql://m365user:m365pass@db:5432/m365logs` | Postgres DSN |
| `SECRET_KEY` | `dev-secret-change-me` | Flask session secret – **change in production** |
| `FLASK_ENV` | `production` | Set to `development` for debug mode |
| `SYNC_INTERVAL_MINUTES` | `60` | How often the background job runs |
| `SYNC_LOOKBACK_MINUTES` | `5` | Overlap window to catch out-of-order events |
| `GRAPH_PAGE_SIZE` | `500` | `$top` value per Graph page |
| `BASIC_AUTH_USERNAME` | *(empty = disabled)* | Enable HTTP Basic Auth |
| `BASIC_AUTH_PASSWORD` | *(empty = disabled)* | HTTP Basic Auth password |
| `OAUTH_ENABLED` | `false` | Set `true` to require Microsoft SSO |
| `OAUTH_REDIRECT_URI` | `http://localhost:8080/auth/callback` | Must match Web redirect URI in the Azure AD app |
| `ARCHIVE_ENABLED` | `false` | Set `true` to enable the nightly R2 archive sweep |
| `ARCHIVE_HOT_DAYS` | `35` | Days of sign-in events to keep in Postgres; older days get archived |
| `ARCHIVE_INDEX_PATH` | `/app/data/archive_index.duckdb` | DuckDB file backing cross-archive search |
| `R2_ACCOUNT_ID` | *(empty = archive off)* | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` | *(empty = archive off)* | R2 S3-compatible access key (scope to one bucket) |
| `R2_SECRET_ACCESS_KEY` | *(empty = archive off)* | R2 secret access key |
| `R2_BUCKET` | *(empty = archive off)* | R2 bucket name |
| `R2_ENDPOINT` | *(empty)* | `https://<account-id>.r2.cloudflarestorage.com` |

---

## Pages & API Endpoints

### Web pages

| Path | Description |
|---|---|
| `/` | Redirects to `/dashboard` |
| `/dashboard?window=24h&types=interactive` | Metrics + async-loaded anomalies. `window` = `24h` / `7d` / `30d`. `types` = `interactive` / `noninteractive` / `serviceprincipal` / `managedidentity` / `users` / `all` |
| `/search` | Filter + keyword search over the **hot** (35-day) window in Postgres |
| `/event/<id>` | Full event detail + raw JSON |
| `/admin/archive` | List of all archived days in R2, with Browse / Search Archive / Rebuild Index buttons |
| `/admin/archive/<YYYY-MM-DD>` | Paginated view of one archived day (fetched on demand from R2) |
| `/admin/archive/<day>/event/<id>` | Event detail for an archived event |
| `/admin/archive/search` | Cross-archive search backed by the DuckDB index — UPN / IP / app / country / error_code / result / type / date range filters with CSV export |
| `/admin/archive/search.csv` | Streaming CSV download of the current search |
| `/admin/sync-status` | Cursor state, last run stats, Sync Now + Archive Now buttons, R2 storage panel |

### JSON API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/search` | Same params as `/search` page |
| `GET` | `/api/dashboard?window=24h` | Metrics + anomalies JSON (synchronous) |
| `GET` | `/api/dashboard/anomalies?window=24h` | Just anomalies — used by the async-loading dashboard |
| `POST` | `/api/sync-now` | Trigger manual sync; returns stats |
| `GET` | `/api/sync-status` | Cursor + last 10 sync runs |
| `POST` | `/api/archive-now?max_days=N` | Trigger an immediate archive sweep (optionally limited to N days) |
| `GET` | `/api/archive-status` | Hot row count + last archive run + R2 totals |
| `POST` | `/api/archive-rebuild-index` | Rebuild the DuckDB search index from R2 (background job) |

**`GET /api/search` query params**: `q`, `upn`, `ip`, `app`, `country`, `error_code`, `from` (ISO datetime), `to`, `page`, `per_page`.

---

## Anomaly Heuristics

All heuristics are implemented as SQL queries in `app/queries.py` against the local DB (no live Graph calls). All queries are scoped to `signin_event_type='interactiveUser'` — service principal and managed identity sign-ins don't make sense for these checks, and non-interactive refresh tokens create too much noise.

| Heuristic | Logic |
|---|---|
| **new_ip_for_user** | IP seen for this user in the analysis window but NOT in the preceding `lookback_days` days |
| **unfamiliar_country** | Country seen in the analysis window but NOT in the preceding `lookback_days` days |
| **impossible_travel** | Same user signs in from two different countries within 60 minutes (consecutive events, ordered by time) |
| **repeated_ca_failure** | User has ≥ 3 conditional-access failures (`conditionalAccessStatus = 'failure'`) in the window |

Heuristics are intentionally **simple and transparent** – they surface interesting events without requiring ML models or external enrichment services.

Results are cached in-process for 5 minutes per `(hours, lookback_days)` key and loaded asynchronously by the dashboard so the page shell paints in ~1 second regardless of how long the heuristics take.

---

## Running Tests

Tests mock all database and HTTP calls – no live services required.

```bash
# Inside Docker
make test

# Locally (needs Python 3.11+, pip install -r requirements.txt)
SKIP_SCHEDULER=1 pytest tests/ -v
```

Test coverage includes:

- `test_graph_client.py` – token acquisition, 429 retry, pagination.
- `test_sync_service.py` – cursor/lookback logic, event coercion, deduplication path, failure rollback.
- `test_queries.py` – all four anomaly heuristics in pure Python.

---

## Development Notes & Assumptions

1. **Entra ID P2 not required** – `riskDetail` and `riskLevelAggregated` fields are present on the `signIns` response for P2 tenants; for standard tenants they will be `null` / `"none"`. The schema stores them as nullable columns.

2. **Sign-in log retention** – Microsoft retains sign-in logs for 30 days in the API for most SKUs. The first sync back-fills 24 hours by default. Adjust by seeding the cursor manually if you need more history.

3. **`$orderby` on `createdDateTime`** – This is supported by the Graph `signIns` endpoint with `ConsistencyLevel: eventual`. Some tenants may see an error if the index is not yet available; the code retries on 5xx.

4. **Impossible travel distance** – We approximate "distance" by checking `countryOrRegion` changes rather than computing haversine distance from coordinates. This avoids requiring a geo-IP database while still catching obvious cases.

5. **Basic Auth** – Set `BASIC_AUTH_USERNAME` and `BASIC_AUTH_PASSWORD` in `.env` to enable. For production, place the app behind a reverse proxy (nginx/Caddy) with TLS.

6. **`AuditLog.Read.All` vs `AuditLog.ReadWrite.All`** – Read-only is sufficient. We never write back to Graph.

7. **Multi-tenant** – This app is designed for a single tenant. Multi-tenant support would require isolating data per tenant, which is outside the MVP scope.

---

## Security Notes

- **Never commit `.env`** – it is gitignored by default.
- The `SECRET_KEY` is used for Flask session signing. Use a long random value in production (`python3 -c "import secrets; print(secrets.token_hex(32))"`).
- The PostgreSQL port (5432) is exposed locally for development. Remove or restrict it in production by removing the `ports:` block from `docker-compose.yml`.
- Store `CLIENT_SECRET` in a secrets manager (Azure Key Vault, AWS Secrets Manager, etc.) rather than a plain `.env` file in production.
