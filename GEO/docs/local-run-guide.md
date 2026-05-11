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
| POST /llm/generate | multi-LLM generation |
| POST /geo/rewrite | GEO rewrite |
| POST /geo/faq | FAQ generation |

## Supported Providers

- openai
- gemini
- deepseek
