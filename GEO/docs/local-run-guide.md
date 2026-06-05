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

Production API authentication is optional and disabled by default for local
development. Enable API Key authentication with:

```bash
export GEO_AUTH_REQUIRED=true
export GEO_API_KEYS='{"viewer-token":{"name":"viewer@example.com","role":"viewer"},"operator-token":{"name":"operator@example.com","role":"operator"},"reviewer-token":{"name":"reviewer@example.com","role":"reviewer"},"admin-token":{"name":"admin@example.com","role":"admin"}}'
```

Clients can send either `Authorization: Bearer <token>` or
`X-GEO-API-Key: <token>`. Roles are hierarchical: `viewer`, `operator`,
`reviewer`, `admin`. Review approval requires `reviewer`; write workflows
require `operator`; admin/history reads require `viewer`. The admin console
stores its entered API Key in browser session storage only.

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
http://127.0.0.1:8000
```

If you test on a real device or production build, replace `apiBase` in `app.js` with your deployed HTTPS API domain.

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

Run the automated closed-loop checks with:

```bash
python -m unittest discover -s tests -v
```

## Supported Providers

- openai
- gemini
- deepseek
