# Local Run Guide

## 1. Clone Repository

```bash
git clone <your_repo_url>
cd GEO
```

## 2. Create Virtual Environment

```bash
python -m venv venv
```

### Mac / Linux

```bash
source venv/bin/activate
```

### Windows

```bash
venv\\Scripts\\activate
```

## 3. Install Dependencies

```bash
pip install -r requirements.txt
```

## 4. Configure API Keys

Create environment variables:

```bash
OPENAI_API_KEY=YOUR_OPENAI_API_KEY
GEMINI_API_KEY=YOUR_GEMINI_API_KEY
DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY
```

Protected APIs now require an API key by default. Configure role-based tokens
with:

```bash
export GEO_API_KEYS='{"viewer-token":{"name":"viewer@example.com","role":"viewer"},"operator-token":{"name":"operator@example.com","role":"operator"},"reviewer-token":{"name":"reviewer@example.com","role":"reviewer"},"admin-token":{"name":"admin@example.com","role":"admin"}}'
```

Clients can send either `Authorization: Bearer <token>` or
`X-GEO-API-Key: <token>`. Roles are hierarchical: `viewer`, `operator`,
`reviewer`, `admin`. Review approval requires `reviewer`; write workflows
require `operator`; admin/history reads require `viewer`. The admin console
stores its entered API Key in browser session storage only.

For local-only development you can explicitly opt into an unsafe bypass:

```bash
export GEO_UNSAFE_LOCAL_DEV=true
```

This bypass only works for loopback hosts such as `127.0.0.1`, `localhost`, or
`[::1]`, and should never be enabled in shared or deployed environments.

Browser CORS is restricted to loopback origins by default. To allow additional
browser frontends, set:

```bash
export GEO_CORS_ALLOWED_ORIGINS='https://admin.example.com,https://ops.example.com'
```

Commercial pilots should also configure persistence and browser session
settings explicitly:

```bash
export DATABASE_URL='sqlite:///backend/geo_growth.db'
export GEO_BROWSER_BASE_URL='https://admin.example.com'
export GEO_SESSION_COOKIE_NAME='geo_session'
export GEO_SESSION_COOKIE_SECURE='true'
export GEO_SESSION_COOKIE_SAMESITE='lax'
export GEO_SESSION_TTL_HOURS='168'
```

If you are bootstrapping a fresh pilot environment, you can override the
default internal tenancy labels used for legacy backfill:

```bash
export GEO_BOOTSTRAP_ORG_NAME='GEO Internal'
export GEO_BOOTSTRAP_WORKSPACE_NAME='Internal Ops'
export GEO_BOOTSTRAP_CUSTOMER_NAME='Internal Pilot'
export GEO_BOOTSTRAP_MARKET='Hong Kong/Japan'
export GEO_BOOTSTRAP_LANGUAGE='zh-HK'
```

## 5. Run FastAPI

```bash
uvicorn backend.main:app --reload
```

## 6. Open API Docs

```text
http://127.0.0.1:8000/docs
```

Open the operational admin console:

```text
http://127.0.0.1:8000/admin
```

## Current APIs

| Endpoint | Purpose |
|---|---|
| GET / | health check |
| GET /health | Runtime health and persistence paths |
| POST /auth/invites | Create a scoped browser invite for workspace/customer users |
| POST /auth/invites/accept | Exchange an invite token for a browser session cookie |
| GET /auth/session/me | Resolve the current browser session or scoped API key identity |
| POST /auth/session/logout | Revoke the current browser session cookie |
| GET /workspaces | List workspaces visible to the current identity |
| POST /workspaces | Create a workspace for a pilot tenant |
| GET /customers | List customers visible to the current identity or scoped workspace |
| POST /customers | Create a customer under a workspace |
| GET /customers/{customer_id}/members | List active memberships for one customer |
| POST /customers/{customer_id}/members | Create or update a membership for one customer |
| GET /customers/{customer_id}/reports | List reports for one customer scope |
| POST /customers/{customer_id}/reports | Generate one scoped report for a customer task |
| GET /admin/api/overview | Operational metrics and attention queues |
| GET /admin/api/tasks | Search and filter task summaries |
| GET /admin/api/tasks/{task_id} | Admin task detail |
| GET /admin/api/audit-logs | Search recent critical operation audit records |
| GET /admin/api/jobs | List persisted async and scheduled jobs |
| POST /admin/api/jobs/run-due | Run scheduled jobs that are due |
| POST /admin/api/jobs/{job_id}/retry | Retry a failed or waiting job |
| POST /geo/audit | GEO scoring |
| POST /geo/url-audit | URL page fetch + GEO scoring + growth plan |
| POST /geo/analyze | URL page fetch + GEO content package |
| POST /geo/improve | GEO improvement workflow + injection payload |
| POST /geo/version/save | Save an editable review version |
| POST /geo/version/review | Approve or reject a saved version |
| POST /geo/inject | Deliver an approved version to JSON file or CMS webhook |
| POST /geo/retest | Retest the URL after a completed delivery/injection |
| POST /geo/retest/schedule | Persist an immediate or scheduled retest job |
| GET /geo/projects | List long-running URL promotion projects with owner, target, stage, and next action |
| POST /geo/projects/{task_id} | Update project owner, target score, and todos |
| POST /geo/versions/{version_id}/quality-check | Run content completeness, claim, and Schema quality checks |
| GET /cms/targets | List CMS publishing targets without exposing credentials |
| POST /cms/targets | Configure a CMS target using an environment-variable credential reference |
| POST /cms/publications/preview | Create an approved, quality-gated publishing preview |
| POST /cms/publications/confirm | Explicitly confirm and execute a CMS publication |
| POST /cms/publications/verify | Verify that expected published content is visible on the live URL |
| POST /cms/publications/verify/schedule | Persist an automatic live publication verification job |
| POST /cms/publications/{publication_id}/retry | Return a failed publication to the confirmation queue |
| GET /geo/history | Persistent task, version, injection, and retest history |
| GET /geo/tasks/{task_id} | Restore a complete task workflow |
| GET /geo/versions/{version_id} | Load one review version |
| POST /geo/export/json | Export approved payload to a local JSON file |
| POST /llm/generate | multi-LLM generation |
| POST /geo/rewrite | GEO rewrite |
| POST /geo/faq | FAQ generation |

## Mini Program

Open this repository in WeChat DevTools. The mini program entry is:

```text
pages/index/index
```

For local testing, start the backend first:

```bash
uvicorn backend.main:app --reload
```

The mini program calls:

```text
develop -> http://127.0.0.1:8000
trial -> https://staging.geo.example.com
release -> https://api.geo.example.com
```

You can override the API base, operator API key, workspace ID, and customer ID
through `wx` storage keys:

- `geoApiBaseOverride`
- `geoApiKey`
- `geoWorkspaceId`
- `geoCustomerId`

The mini-program request helper now forwards `X-GEO-API-Key`,
`X-GEO-Workspace-ID`, and `X-GEO-Customer-ID` automatically when those values
exist in `app.globalData`.

## Persistence and Exports

Tasks, review versions, injection deliveries, and retest records are persisted locally with SQLite:

```text
backend/geo_growth.db
```

JSON export artifacts are written to:

```text
exports/
```

Both paths are ignored by Git because they are runtime data.

## AI JSON Analysis

`POST /geo/analyze` supports optional AI analysis:

```json
{
  "url": "https://example.com",
  "workspace_id": "ws_xxx",
  "customer_id": "cust_xxx",
  "use_ai": true,
  "provider": "openai"
}
```

If the model call fails, the backend falls back to the local rule-based content package generator.

## Closed-loop Workflow

The enforced URL workflow is:

```text
analyze -> improve -> save version -> approve -> inject/deliver -> retest -> history
```

`POST /geo/inject` only accepts approved versions. `POST /geo/retest` only accepts
a completed injection record for the same task and URL.

The `json_file` injection target creates a reviewable delivery artifact. The
`webhook` target posts the approved payload to a public CMS endpoint and stores
the response status. Private/local webhook addresses are blocked.

## Async And Scheduled Retests

`POST /geo/retest/schedule` persists a retest job in SQLite. Immediate jobs are
dispatched after the HTTP response. Future jobs remain queued until an external
scheduler calls:

```bash
curl -X POST -H "Authorization: Bearer $GEO_OPERATOR_TOKEN" \
  http://127.0.0.1:8000/admin/api/jobs/run-due
```

Failed jobs wait five minutes per attempt and retry up to `max_attempts` (default
3, maximum 10). Operators can inspect and retry jobs from the admin console.

## Promotion Projects And CMS Publishing

Every analyzed URL is also a long-running promotion project. Projects track an
owner, target score, current workflow stage, todos, next action, and retest
effectiveness. Saved versions receive a quality report; blocking completeness,
Schema, or unsupported-claim issues prevent approval and publication.

CMS targets store webhook configuration and an optional environment variable
name for credentials. Credential values are read only at publish time and are
never persisted or returned by the API. Publishing requires an approved version,
a passed quality report, a preview record, and explicit `PUBLISH` confirmation.
Failed publications remain visible in the operations console and can be returned
to the confirmation queue after configuration or credential fixes.

Published records can be verified immediately or scheduled through the durable
job queue. Automatic verification retries transient page-fetch failures and
records matched and missing expected terms before the project advances to
post-publication retesting.

AI-generated improvement modules include the approved knowledge item IDs used
in their prompt context. Version quality reports expose knowledge citation
coverage and warn when available brand knowledge is not traceable from a module.

Run the automated closed-loop checks with:

```bash
python -m unittest discover -s tests -v
```

## Supported Providers

- openai
- gemini
- deepseek
