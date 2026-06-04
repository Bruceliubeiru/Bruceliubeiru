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
| POST /geo/audit | GEO scoring |
| POST /geo/url-audit | URL page fetch + GEO scoring + growth plan |
| POST /geo/analyze | URL page fetch + GEO content package |
| POST /geo/improve | GEO improvement workflow + injection payload |
| POST /geo/version/save | Save an editable review version |
| POST /geo/version/review | Approve or reject a saved version |
| POST /geo/retest | Retest the URL after approved injection |
| GET /geo/history | In-memory task, version, and retest history |
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

## Supported Providers

- openai
- gemini
- deepseek
