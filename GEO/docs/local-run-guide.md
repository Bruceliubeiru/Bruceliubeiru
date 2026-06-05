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

## 5. Run FastAPI

```bash
uvicorn backend.main:app --reload
```

## 6. Open API Docs

```text
http://127.0.0.1:8000/docs
```

## Current APIs

| Endpoint | Purpose |
|---|---|
| GET / | health check |
| GET /health | Runtime health and persistence paths |
| POST /geo/audit | GEO scoring |
| POST /geo/url-audit | URL page fetch + GEO scoring + growth plan |
| POST /geo/analyze | URL page fetch + GEO content package |
| POST /geo/improve | GEO improvement workflow + injection payload |
| POST /geo/version/save | Save an editable review version |
| POST /geo/version/review | Approve or reject a saved version |
| POST /geo/inject | Deliver an approved version to JSON file or CMS webhook |
| POST /geo/retest | Retest the URL after a completed delivery/injection |
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

Run the automated closed-loop checks with:

```bash
python -m unittest discover -s tests -v
```

## Supported Providers

- openai
- gemini
- deepseek
