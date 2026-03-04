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

- **Incremental sync** from Microsoft Graph `/auditLogs/signIns` using an OData `$filter` cursor – fetches only new records each hour.
- **Idempotent upserts** – `ON CONFLICT DO UPDATE` on the Graph event `id` field.
- **Exponential-backoff retry** for HTTP 429 rate-limit and 5xx transient errors.
- **PostgreSQL** with trigram GIN indexes for sub-300 ms keyword search.
- **Dashboard** with Chart.js timeline, top users/IPs by failures, and anomaly table.
- **Four anomaly heuristics** (new IP, unfamiliar country, impossible travel, repeated CA failures) – all run as SQL against the local DB.
- **`/admin/sync-status`** page with Sync Now button.
- **Docker Compose** one-liner startup; Alembic runs migrations automatically on container start.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Docker Compose                                          │
│                                                          │
│  ┌──────────────────────────┐   ┌──────────────────────┐ │
│  │  web (Flask + APScheduler)│──▶│  db (PostgreSQL 16)  │ │
│  │  port 8080               │   │  port 5432 (internal)│ │
│  └──────────┬───────────────┘   └──────────────────────┘ │
│             │ HTTPS / Graph API                           │
│             ▼                                             │
│       Microsoft Graph                                     │
│       /auditLogs/signIns                                  │
└──────────────────────────────────────────────────────────┘
```

**Project layout**

```
M365-Log-Master/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── config.py            # Environment-based configuration
│   ├── extensions.py        # SQLAlchemy engine / session
│   ├── models.py            # ORM models (SignInEvent, SyncCursor, SyncLog)
│   ├── graph_client.py      # MSAL auth + Graph API pagination + retry
│   ├── sync_service.py      # Incremental sync logic (cursor, upsert, logging)
│   ├── queries.py           # Dashboard metrics + anomaly SQL
│   ├── routes.py            # Server-rendered HTML pages
│   ├── api.py               # JSON API endpoints
│   ├── scheduler.py         # APScheduler hourly job
│   ├── auth.py              # Optional HTTP Basic Auth middleware
│   ├── templates/
│   │   ├── base.html
│   │   ├── search.html
│   │   ├── dashboard.html
│   │   ├── event_detail.html
│   │   ├── sync_status.html
│   │   └── 404.html
│   └── static/{css,js}/
├── migrations/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/001_initial_schema.py
├── tests/
│   ├── conftest.py
│   ├── test_graph_client.py
│   ├── test_sync_service.py
│   └── test_queries.py
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

---

## Pages & API Endpoints

### Web pages

| Path | Description |
|---|---|
| `/` | Redirects to `/dashboard` |
| `/dashboard?window=24h` | Metrics + anomalies. `window` = `24h` / `7d` / `30d` |
| `/search` | Filter + keyword search with pagination |
| `/event/<id>` | Full event detail + raw JSON |
| `/admin/sync-status` | Cursor state, last run stats, Sync Now button |

### JSON API

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/search` | Same params as `/search` page |
| `GET` | `/api/dashboard?window=24h` | Metrics + anomalies JSON |
| `POST` | `/api/sync-now` | Trigger manual sync; returns stats |
| `GET` | `/api/sync-status` | Cursor + last 10 sync runs |

**`GET /api/search` query params**: `q`, `upn`, `ip`, `app`, `country`, `error_code`, `from` (ISO datetime), `to`, `page`, `per_page`.

---

## Anomaly Heuristics

All heuristics are implemented as SQL queries in `app/queries.py` against the local DB (no live Graph calls).

| Heuristic | Logic |
|---|---|
| **new_ip_for_user** | IP seen for this user in the analysis window but NOT in the preceding `lookback_days` days |
| **unfamiliar_country** | Country seen in the analysis window but NOT in the preceding `lookback_days` days |
| **impossible_travel** | Same user signs in from two different countries within 60 minutes (consecutive events, ordered by time) |
| **repeated_ca_failure** | User has ≥ 3 conditional-access failures (`conditionalAccessStatus = 'failure'`) in the window |

Heuristics are intentionally **simple and transparent** – they surface interesting events without requiring ML models or external enrichment services.

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
