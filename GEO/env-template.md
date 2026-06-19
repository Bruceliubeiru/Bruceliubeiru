# Environment Variables Template

## OpenAI

OPENAI_API_KEY=YOUR_OPENAI_API_KEY

## Gemini

GEMINI_API_KEY=YOUR_GEMINI_API_KEY

## DeepSeek

DEEPSEEK_API_KEY=YOUR_DEEPSEEK_API_KEY

## API authentication and browser access

GEO_API_KEYS={"viewer-token":{"name":"viewer@example.com","role":"viewer"},"operator-token":{"name":"operator@example.com","role":"operator"},"reviewer-token":{"name":"reviewer@example.com","role":"reviewer"},"admin-token":{"name":"admin@example.com","role":"admin"}}
GEO_UNSAFE_LOCAL_DEV=false
GEO_CORS_ALLOWED_ORIGINS=https://admin.example.com,https://ops.example.com
DATABASE_URL=sqlite:///backend/geo_growth.db
GEO_BROWSER_BASE_URL=https://admin.example.com
GEO_SESSION_COOKIE_NAME=geo_session
GEO_SESSION_COOKIE_SECURE=true
GEO_SESSION_COOKIE_SAMESITE=lax
GEO_SESSION_TTL_HOURS=168
GEO_BOOTSTRAP_ORG_NAME=GEO Internal
GEO_BOOTSTRAP_WORKSPACE_NAME=Internal Ops
GEO_BOOTSTRAP_CUSTOMER_NAME=Internal Pilot
GEO_BOOTSTRAP_MARKET=Hong Kong/Japan
GEO_BOOTSTRAP_LANGUAGE=zh-HK
