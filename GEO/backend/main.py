import json
import hashlib
import ipaddress
import re
import socket
import sqlite3
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    from backend.geo_scoring import score_content
except ImportError:
    from geo_scoring import score_content

try:
    from backend.admin_service import build_admin_overview, filter_admin_tasks
except ImportError:
    from admin_service import build_admin_overview, filter_admin_tasks

try:
    from backend.llm_providers import MultiLLMClient, LLMProviderError
except ImportError as llm_import_error:
    try:
        from llm_providers import MultiLLMClient, LLMProviderError
    except ImportError:
        class LLMProviderError(Exception):
            pass

        class MultiLLMClient:
            def __init__(self):
                raise LLMProviderError(
                    f"LLM dependencies are not installed: {llm_import_error}"
                )


app = FastAPI(title="GEO Growth OS")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = Path(__file__).with_name("geo_growth.db")
EXPORT_DIR = Path(__file__).resolve().parent.parent / "exports"
ADMIN_INDEX = Path(__file__).resolve().parent.parent / "admin" / "index.html"
TASK_STORE: dict[str, dict] = {}
VERSION_STORE: dict[str, dict] = {}
RETEST_STORE: dict[str, list[dict]] = {}


class GEOAuditRequest(BaseModel):
    content: str


class GEOUrlAuditRequest(BaseModel):
    url: str


class GEOAnalyzeRequest(BaseModel):
    url: str
    page_type: str = "transport_pass"
    page_goal: str = "推广页"
    market: str = "Hong Kong"
    language: str = "zh-HK"
    output_format: str = "json"
    generate_faq: bool = True
    generate_schema: bool = True
    generate_conversion_tips: bool = True
    use_ai: bool = False
    provider: str = "openai"
    model: str | None = None


class GEOImproveRequest(BaseModel):
    result: dict
    provider: str | None = None
    model: str | None = None
    use_ai: bool = False


class GEOVersionSaveRequest(BaseModel):
    task_id: str
    url: str
    modules: list[dict]
    workflow: dict | None = None
    editor: str = "operator"


class GEOReviewRequest(BaseModel):
    version_id: str
    action: str = "approve"
    reviewer: str = "operator"
    comment: str | None = None


class GEORetestRequest(BaseModel):
    task_id: str
    url: str
    previous_score: int = 0
    approved_payload: dict | None = None
    version_id: str | None = None
    injection_id: str | None = None


class GEOInjectRequest(BaseModel):
    version_id: str
    target: str = "json_file"
    webhook_url: str | None = None
    headers: dict[str, str] | None = None


class GEOExportRequest(BaseModel):
    task_id: str
    payload: dict
    target: str = "json_file"


class LLMGenerateRequest(BaseModel):
    provider: str = "openai"
    prompt: str
    model: str | None = None
    temperature: float = 0.3


class GEORewriteRequest(BaseModel):
    provider: str = "openai"
    content: str
    model: str | None = None


class GEOFAQRequest(BaseModel):
    provider: str = "openai"
    product_description: str
    count: int = 20
    model: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, fallback):
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _init_db() -> None:
    EXPORT_DIR.mkdir(exist_ok=True)
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT,
                status TEXT,
                latest_result TEXT,
                latest_workflow TEXT,
                latest_version_id TEXT,
                latest_retest TEXT,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS versions (
                version_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                url TEXT NOT NULL,
                status TEXT,
                editor TEXT,
                reviewer TEXT,
                review_comment TEXT,
                modules TEXT,
                workflow TEXT,
                injection_payload TEXT,
                created_at TEXT,
                updated_at TEXT,
                approved_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS retests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                version_id TEXT,
                injection_id TEXT,
                url TEXT NOT NULL,
                title TEXT,
                previous_score INTEGER,
                current_score INTEGER,
                score_delta INTEGER,
                status TEXT,
                breakdown TEXT,
                recommendations TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS injections (
                injection_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                url TEXT NOT NULL,
                target TEXT NOT NULL,
                status TEXT NOT NULL,
                response_summary TEXT,
                artifact_path TEXT,
                created_at TEXT,
                completed_at TEXT
            )
            """
        )
        retest_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(retests)").fetchall()
        }
        if "version_id" not in retest_columns:
            conn.execute("ALTER TABLE retests ADD COLUMN version_id TEXT")
        if "injection_id" not in retest_columns:
            conn.execute("ALTER TABLE retests ADD COLUMN injection_id TEXT")


def _db_upsert_task(task: dict) -> None:
    TASK_STORE[task["task_id"]] = task
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, url, title, status, latest_result, latest_workflow,
                latest_version_id, latest_retest, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                url=excluded.url,
                title=excluded.title,
                status=excluded.status,
                latest_result=excluded.latest_result,
                latest_workflow=excluded.latest_workflow,
                latest_version_id=excluded.latest_version_id,
                latest_retest=excluded.latest_retest,
                updated_at=excluded.updated_at
            """,
            (
                task.get("task_id"),
                task.get("url"),
                task.get("title"),
                task.get("status"),
                _json_dumps(task.get("latest_result")) if task.get("latest_result") else None,
                _json_dumps(task.get("latest_workflow")) if task.get("latest_workflow") else None,
                task.get("latest_version_id"),
                _json_dumps(task.get("latest_retest")) if task.get("latest_retest") else None,
                task.get("created_at") or _now_iso(),
                task.get("updated_at") or _now_iso(),
            ),
        )


def _db_get_task(task_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return TASK_STORE.get(task_id)
    return _task_from_row(row)


def _task_from_row(row: sqlite3.Row) -> dict:
    return {
        "task_id": row["task_id"],
        "url": row["url"],
        "title": row["title"],
        "status": row["status"],
        "latest_result": _json_loads(row["latest_result"], None),
        "latest_workflow": _json_loads(row["latest_workflow"], None),
        "latest_version_id": row["latest_version_id"],
        "latest_retest": _json_loads(row["latest_retest"], None),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _version_from_row(row: sqlite3.Row) -> dict:
    return {
        "version_id": row["version_id"],
        "task_id": row["task_id"],
        "url": row["url"],
        "status": row["status"],
        "editor": row["editor"],
        "reviewer": row["reviewer"],
        "review_comment": row["review_comment"],
        "modules": _json_loads(row["modules"], []),
        "workflow": _json_loads(row["workflow"], {}),
        "injection_payload": _json_loads(row["injection_payload"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "approved_at": row["approved_at"],
    }


def _db_save_version(version: dict) -> None:
    VERSION_STORE[version["version_id"]] = version
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO versions (
                version_id, task_id, url, status, editor, reviewer, review_comment,
                modules, workflow, injection_payload, created_at, updated_at, approved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version_id) DO UPDATE SET
                status=excluded.status,
                reviewer=excluded.reviewer,
                review_comment=excluded.review_comment,
                modules=excluded.modules,
                workflow=excluded.workflow,
                injection_payload=excluded.injection_payload,
                updated_at=excluded.updated_at,
                approved_at=excluded.approved_at
            """,
            (
                version.get("version_id"),
                version.get("task_id"),
                version.get("url"),
                version.get("status"),
                version.get("editor"),
                version.get("reviewer"),
                version.get("review_comment"),
                _json_dumps(version.get("modules", [])),
                _json_dumps(version.get("workflow", {})),
                _json_dumps(version.get("injection_payload", {})),
                version.get("created_at") or _now_iso(),
                version.get("updated_at") or _now_iso(),
                version.get("approved_at"),
            ),
        )


def _db_get_version(version_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM versions WHERE version_id = ?", (version_id,)).fetchone()
    if not row:
        return VERSION_STORE.get(version_id)
    return _version_from_row(row)


def _db_count_versions(task_id: str) -> int:
    with _db() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM versions WHERE task_id = ?", (task_id,)).fetchone()
    return int(row["count"]) if row else 0


def _db_add_retest(retest: dict) -> None:
    RETEST_STORE.setdefault(retest["task_id"], []).append(retest)
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO retests (
                task_id, version_id, injection_id, url, title, previous_score, current_score, score_delta,
                status, breakdown, recommendations, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                retest.get("task_id"),
                retest.get("version_id"),
                retest.get("injection_id"),
                retest.get("url"),
                retest.get("title"),
                retest.get("previous_score"),
                retest.get("current_score"),
                retest.get("score_delta"),
                retest.get("status"),
                _json_dumps(retest.get("breakdown")),
                _json_dumps(retest.get("recommendations")),
                retest.get("created_at"),
            ),
        )


def _db_save_injection(injection: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO injections (
                injection_id, task_id, version_id, url, target, status,
                response_summary, artifact_path, created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(injection_id) DO UPDATE SET
                status=excluded.status,
                response_summary=excluded.response_summary,
                artifact_path=excluded.artifact_path,
                completed_at=excluded.completed_at
            """,
            (
                injection.get("injection_id"),
                injection.get("task_id"),
                injection.get("version_id"),
                injection.get("url"),
                injection.get("target"),
                injection.get("status"),
                injection.get("response_summary"),
                injection.get("artifact_path"),
                injection.get("created_at"),
                injection.get("completed_at"),
            ),
        )


def _injection_from_row(row: sqlite3.Row) -> dict:
    return {
        "injection_id": row["injection_id"],
        "task_id": row["task_id"],
        "version_id": row["version_id"],
        "url": row["url"],
        "target": row["target"],
        "status": row["status"],
        "response_summary": row["response_summary"],
        "artifact_path": row["artifact_path"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def _db_get_injection(injection_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM injections WHERE injection_id = ?",
            (injection_id,),
        ).fetchone()
    return _injection_from_row(row) if row else None


def _db_latest_successful_injection(task_id: str, version_id: str | None = None) -> dict | None:
    query = "SELECT * FROM injections WHERE task_id = ? AND status = 'completed'"
    params: list[str] = [task_id]
    if version_id:
        query += " AND version_id = ?"
        params.append(version_id)
    query += " ORDER BY completed_at DESC LIMIT 1"
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _injection_from_row(row) if row else None


def _db_history() -> dict:
    with _db() as conn:
        task_rows = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
        version_rows = conn.execute("SELECT * FROM versions ORDER BY updated_at DESC").fetchall()
        retest_rows = conn.execute("SELECT * FROM retests ORDER BY created_at DESC").fetchall()
        injection_rows = conn.execute("SELECT * FROM injections ORDER BY created_at DESC").fetchall()

    retests: dict[str, list[dict]] = {}
    for row in retest_rows:
        item = {
            "task_id": row["task_id"],
            "version_id": row["version_id"],
            "injection_id": row["injection_id"],
            "url": row["url"],
            "title": row["title"],
            "previous_score": row["previous_score"],
            "current_score": row["current_score"],
            "score_delta": row["score_delta"],
            "status": row["status"],
            "breakdown": _json_loads(row["breakdown"], {}),
            "recommendations": _json_loads(row["recommendations"], []),
            "created_at": row["created_at"],
        }
        retests.setdefault(row["task_id"], []).append(item)

    return {
        "tasks": [_task_from_row(row) for row in task_rows],
        "versions": [_version_from_row(row) for row in version_rows],
        "retests": retests,
        "injections": [_injection_from_row(row) for row in injection_rows],
    }


def _update_stage_status(workflow: dict, key: str, status: str) -> dict:
    stages = workflow.get("stages") or []
    for stage in stages:
        if stage.get("key") == key:
            stage["status"] = status
    workflow["stages"] = stages
    return workflow


def _build_version_id(task_id: str) -> str:
    existing_count = _db_count_versions(task_id)
    return f"{task_id}_v{existing_count + 1}"


def _build_task_id(url: str) -> str:
    return f"geo_{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"


def _build_injection_id(version_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"inject_{version_id}_{stamp}"


def _validate_public_url(raw_url: str, label: str) -> str:
    parsed = urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} URL must use http or https.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except socket.gaierror as exc:
        raise ValueError(f"{label} host cannot be resolved: {exc}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError(f"{label} URL must resolve to a public network address.")
    return raw_url


def _validate_public_webhook_url(raw_url: str) -> str:
    return _validate_public_url(raw_url, "Webhook")


_init_db()


class _ReadableTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self._parts.append(text)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._parts)).strip()


def _normalize_url(raw_url: str) -> str:
    value = raw_url.strip()
    if not value:
        raise ValueError("URL cannot be empty.")
    if not value.startswith(("http://", "https://")):
        value = f"https://{value}"

    parsed = urlparse(value)
    if not parsed.netloc or not parsed.hostname or "." not in parsed.hostname:
        raise ValueError("Please provide a valid website URL.")
    return _validate_public_url(value, "Page")


def _fetch_page_text(url: str) -> tuple[str, str]:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; GEOGrowthOS/1.0; "
                "+https://example.com/geo-audit)"
            )
        },
    )
    with urlopen(request, timeout=12) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset, errors="ignore")

    parser = _ReadableTextParser()
    parser.feed(html)

    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else url
    return title, parser.text[:12000]


def _extract_terms(title: str, content: str) -> dict:
    source = f"{title} {content[:4000]}"
    capitalized = re.findall(r"\b[A-Z][A-Za-z0-9+.-]{1,}(?:\s+[A-Z][A-Za-z0-9+.-]{1,}){0,3}", source)
    known_terms = [
        "JR Pass",
        "Japan Rail Pass",
        "Shinkansen",
        "Tokyo",
        "Osaka",
        "Kyoto",
        "Hokkaido",
        "Kyushu",
        "Trip.com",
    ]
    lower_source = source.lower()
    matched_known_terms = [term for term in known_terms if term.lower() in lower_source]
    stop_entity_words = {"This", "Avoid", "Learn", "More", "Book", "Now"}
    entities = []
    for term in [title] + matched_known_terms + capitalized:
        clean = term.strip(" -|,.;:")
        if any(part in stop_entity_words for part in clean.split()):
            continue
        if clean and clean not in entities and len(clean) <= 48:
            entities.append(clean)

    title_words = [
        word.strip("-_/|,.").lower()
        for word in re.findall(r"[A-Za-z][A-Za-z0-9+-]{2,}", title)
    ]
    keyword_base = [word for word in title_words if word not in {"www", "com", "html"}]
    keywords = []
    for term in entities[:6] + keyword_base:
        value = term if isinstance(term, str) else str(term)
        if value and value not in keywords:
            keywords.append(value)

    return {
        "entities": entities[:12] or [title],
        "keywords": keywords[:12] or [title, "travel pass", "booking guide"],
    }


def _infer_target_users(title: str, content: str) -> list[str]:
    lower = f"{title} {content}".lower()
    users = []
    if any(term in lower for term in ["jr pass", "rail", "train", "shinkansen"]):
        users.extend(["Japan free independent travelers", "multi-city train travelers"])
    if any(term in lower for term in ["family", "kids", "children"]):
        users.append("families planning flexible routes")
    if any(term in lower for term in ["tourist", "travel", "trip"]):
        users.append("travelers comparing ticket value before booking")
    return users[:4] or ["users researching this page before purchase"]


def _build_content_package(
    url: str,
    title: str,
    content: str,
    request: GEOAnalyzeRequest,
    score_result: dict,
) -> dict:
    terms = _extract_terms(title, content)
    target_users = _infer_target_users(title, content)
    primary_entity = terms["entities"][0]
    summary = (
        f"{title} is a {request.page_goal} for users evaluating {primary_entity}. "
        "The page should make the product easy for AI systems to identify, summarize, "
        "compare, and cite with clear definitions, FAQ answers, proof points, and "
        "structured data."
    )

    page_summary = {
        "theme": title,
        "product_type": request.page_type,
        "target_user": target_users,
        "market": request.market,
        "language": request.language,
        "current_score": score_result.get("geo_score"),
    }
    search_intents = ["comparison", "price", "how-to", "booking", "eligibility"]
    content_gaps = score_result.get("recommendations") or [
        "Add an AI-readable summary near the top of the page.",
        "Add FAQ answers for high-intent user questions.",
        "Add comparison and proof points for purchase confidence.",
    ]

    injection_modules = [
        {
            "module_type": "hero",
            "title": f"Plan smarter with {primary_entity}",
            "body": f"Use this page to compare options, understand eligibility, and choose the right {primary_entity} for your trip.",
            "target_position": "top hero",
            "priority": "high",
            "cta": "Find My Pass",
        },
        {
            "module_type": "ai_summary",
            "title": "AI-readable page summary",
            "body": summary,
            "target_position": "below hero",
            "priority": "high",
        },
        {
            "module_type": "who_should_buy",
            "title": "Who should use this page",
            "body": "; ".join(target_users),
            "target_position": "before product cards",
            "priority": "high",
        },
        {
            "module_type": "how_to_use",
            "title": "How to use this offer",
            "body": "Choose the right option, check route coverage, book online, receive confirmation, then follow the redemption and usage rules shown on the page.",
            "target_position": "after product cards",
            "priority": "medium",
        },
    ]

    faq_items = [
        {
            "question": f"What is {primary_entity}?",
            "answer": f"{primary_entity} is the main product or topic on this page. The page should explain what it covers, who it is for, and when it is worth choosing.",
            "source_type": "generated",
            "priority": "high",
        },
        {
            "question": f"Who should consider {primary_entity}?",
            "answer": "It is most useful for travelers or users comparing multiple options, checking eligibility, and looking for a clear booking path.",
            "source_type": "generated",
            "priority": "high",
        },
        {
            "question": "What should users compare before booking?",
            "answer": "Users should compare coverage, price, route fit, redemption steps, restrictions, and whether the offer matches their itinerary.",
            "source_type": "generated",
            "priority": "medium",
        },
    ]

    schema_suggestions = [
        {
            "schema_type": "WebPage",
            "validation_status": "draft",
            "json": {
                "@context": "https://schema.org",
                "@type": "WebPage",
                "name": title,
                "url": url,
                "description": summary[:240],
            },
        },
        {
            "schema_type": "FAQPage",
            "validation_status": "draft",
            "json": {
                "@context": "https://schema.org",
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": item["question"],
                        "acceptedAnswer": {
                            "@type": "Answer",
                            "text": item["answer"],
                        },
                    }
                    for item in faq_items
                ],
            },
        },
    ]

    conversion_tips = [
        "Place an AI-readable summary directly below the hero area.",
        "Add a selector module that helps users choose by route, city, duration, or use case.",
        "Use trust labels near product cards, such as instant confirmation, route coverage, and redemption clarity.",
        "Keep a sticky mobile CTA visible after users scroll past the first product section.",
    ]

    return {
        "page_summary": page_summary,
        "geo_assets": {
            "entities": terms["entities"],
            "keywords": terms["keywords"],
            "search_intents": search_intents,
            "use_cases": target_users,
            "product_attributes": ["coverage", "eligibility", "route fit", "redemption", "booking confidence"],
        },
        "content_gaps": content_gaps,
        "injection_modules": injection_modules,
        "faq_items": faq_items if request.generate_faq else [],
        "schema_suggestions": schema_suggestions if request.generate_schema else [],
        "conversion_tips": conversion_tips if request.generate_conversion_tips else [],
    }


def _extract_json_object(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise


def _validate_content_package(package: dict) -> dict:
    required = {
        "page_summary": {},
        "geo_assets": {},
        "content_gaps": [],
        "injection_modules": [],
        "faq_items": [],
        "schema_suggestions": [],
        "conversion_tips": [],
    }
    for key, fallback in required.items():
        package.setdefault(key, fallback)
    package["analysis_source"] = package.get("analysis_source", "ai")
    return package


def _try_ai_content_package(
    url: str,
    title: str,
    content: str,
    request: GEOAnalyzeRequest,
    score_result: dict,
) -> dict | None:
    if not request.use_ai:
        return None

    prompt = f"""
You are a GEO URL content injection analyst.
Analyze the page and return strict JSON only. Do not include markdown fences.

Page config:
- url: {url}
- title: {title}
- page_type: {request.page_type}
- page_goal: {request.page_goal}
- market: {request.market}
- language: {request.language}
- current_geo_score: {score_result.get("geo_score")}

Page text:
{content[:9000]}

Return JSON with exactly these top-level fields:
page_summary, geo_assets, content_gaps, injection_modules, faq_items, schema_suggestions, conversion_tips.

Rules:
- Do not invent price, rating, inventory, review count, or policy details not visible in the text.
- injection_modules must include module_type, title, body, target_position, priority.
- faq_items must include question, answer, source_type, priority.
- schema_suggestions must include schema_type, validation_status, json.
- Copy should be directly usable for page review and CMS injection.
"""
    try:
        output = MultiLLMClient().generate_text(
            provider=request.provider,  # type: ignore[arg-type]
            prompt=prompt,
            model=request.model,
            temperature=0.2,
        )
        package = _extract_json_object(output)
        package = _validate_content_package(package)
        package["ai_status"] = "generated"
        package["analysis_source"] = "ai"
        return package
    except Exception as exc:
        return {
            "analysis_source": "rules",
            "ai_status": f"fallback_rules_used: {exc}",
        }


def _improve_module(module: dict, entities: list[str], gaps: list[str]) -> dict:
    primary_entity = entities[0] if entities else "this offer"
    module_type = module.get("module_type", "content")
    title = module.get("title") or module_type.replace("_", " ").title()
    body = module.get("body") or ""

    improvements = {
        "hero": {
            "title": f"{primary_entity}: compare, choose, and book with confidence",
            "body": (
                f"Use this page to understand what {primary_entity} covers, who it is best for, "
                "how to choose the right option, and what to check before booking."
            ),
        },
        "ai_summary": {
            "title": f"What users and AI systems should know about {primary_entity}",
            "body": (
                f"{primary_entity} is presented as a decision page for users who need clear "
                "coverage, eligibility, usage, and booking guidance. The page should answer "
                "what it is, who should consider it, how to choose, how to use it, and what "
                "proof supports the recommendation."
            ),
        },
        "who_should_buy": {
            "title": f"Who should consider {primary_entity}",
            "body": (
                "Best for users comparing multiple options, planning a route or itinerary, "
                "checking eligibility, and looking for a simple booking path with clear usage rules."
            ),
        },
        "how_to_use": {
            "title": f"How to use {primary_entity}",
            "body": (
                "Choose the option that matches your route or scenario, confirm coverage and "
                "restrictions, complete booking, save the confirmation, and follow the redemption "
                "or activation steps shown on the page."
            ),
        },
    }
    improved = improvements.get(module_type, {"title": title, "body": body})
    rationale = gaps[0] if gaps else "Improve AI readability and conversion clarity."

    return {
        **module,
        "title": improved["title"],
        "body": improved["body"],
        "status": "draft",
        "review_status": "pending_review",
        "injection_field": f"cms.{module_type}",
        "change_reason": rationale,
        "acceptance_check": "The module is specific, quote-ready, and consistent with visible page facts.",
    }


def _build_improvement_workflow(result: dict) -> dict:
    geo_assets = result.get("geo_assets") or {}
    entities = geo_assets.get("entities") or []
    gaps = result.get("content_gaps") or result.get("recommendations") or []
    modules = result.get("injection_modules") or []
    improved_modules = [_improve_module(module, entities, gaps) for module in modules]

    current_score = int(result.get("geo_score") or 0)
    predicted_score = min(100, current_score + 12)
    workflow_id = f"wf_{abs(hash((result.get('url'), current_score))) % 100000000}"

    return {
        "workflow_id": workflow_id,
        "status": "draft_ready",
        "current_score": current_score,
        "predicted_score": predicted_score,
        "score_delta": predicted_score - current_score,
        "stages": [
            {
                "key": "diagnose",
                "name": "诊断",
                "status": "completed",
                "output": "GEO 分数、内容缺口、页面资产",
            },
            {
                "key": "improve",
                "name": "AI 改进",
                "status": "completed",
                "output": "改进版注入模块草稿",
            },
            {
                "key": "review",
                "name": "人工审核",
                "status": "pending",
                "output": "确认可上线版本",
            },
            {
                "key": "inject",
                "name": "注入",
                "status": "pending",
                "output": "CMS 字段映射 JSON",
            },
            {
                "key": "retest",
                "name": "复测",
                "status": "pending",
                "output": "上线后同 URL 复测",
            },
        ],
        "improved_modules": improved_modules,
        "injection_payload": {
            "url": result.get("url"),
            "task_id": result.get("task_id"),
            "version_status": "pending_review",
            "cms_fields": [
                {
                    "field": module["injection_field"],
                    "module_type": module.get("module_type"),
                    "title": module.get("title"),
                    "body": module.get("body"),
                    "target_position": module.get("target_position"),
                    "priority": module.get("priority"),
                }
                for module in improved_modules
            ],
        },
        "retest_plan": [
            "将审核后的模块注入测试页面或 CMS 草稿。",
            "确认页面展示内容与 Schema/FAQ 保持一致。",
            "发布后重新输入同一 URL 运行 GEO 诊断。",
            "对比 GEO 总分、FAQ 覆盖、引用友好、权威信号是否提升。",
        ],
    }


def _build_injection_payload(task_id: str, url: str, modules: list[dict], version_status: str) -> dict:
    return {
        "url": url,
        "task_id": task_id,
        "version_status": version_status,
        "cms_fields": [
            {
                "field": module.get("injection_field") or f"cms.{module.get('module_type', 'content')}",
                "module_type": module.get("module_type"),
                "title": module.get("title"),
                "body": module.get("body"),
                "target_position": module.get("target_position"),
                "priority": module.get("priority"),
            }
            for module in modules
        ],
    }


def _build_growth_plan(score_result: dict, title: str, url: str) -> list[dict]:
    recommendations = score_result.get("recommendations") or []
    plan = [
        {
            "title": "补齐 AI 可引用定义",
            "impact": "让 AI 能用一句话准确解释你的业务",
            "action": "在首页首屏加入“我们是谁、解决什么问题、适合谁”的短定义。",
        },
        {
            "title": "生成 FAQ 问答资产",
            "impact": "覆盖用户会直接问 AI 的高意图问题",
            "action": "围绕价格、适用人群、竞品差异、使用场景先生成 10 个问答。",
        },
        {
            "title": "增加对比和证据",
            "impact": "提高被 AI 推荐时的可信度和可比较性",
            "action": "添加案例、数据、客户结果、竞品/替代方案对比表。",
        },
    ]

    for recommendation in recommendations[:3]:
        plan.append(
            {
                "title": recommendation,
                "impact": f"提升 {title} 在 AI 答案里的可读性",
                "action": f"针对 {url} 的页面内容执行这项优化，并重新发布。",
            }
        )

    return plan[:5]


@app.get("/")
def root():
    return {
        "project": "GEO Growth OS",
        "status": "running",
        "message": "AI Native Growth Operating System",
        "providers": ["openai", "gemini", "deepseek"],
    }


@app.get("/health")
def health():
    return {"status": "ok", "database": str(DB_PATH), "export_dir": str(EXPORT_DIR)}


@app.get("/admin", include_in_schema=False)
def admin_console():
    return FileResponse(ADMIN_INDEX)


@app.get("/admin/api/overview")
def admin_overview():
    return build_admin_overview(_db_history())


@app.get("/admin/api/tasks")
def admin_tasks(status: str | None = None, q: str | None = None, limit: int = 50):
    return {"items": filter_admin_tasks(_db_history(), status, q, limit)}


@app.get("/admin/api/tasks/{task_id}")
def admin_task_detail(task_id: str):
    return geo_task_detail(task_id)


@app.post("/geo/audit")
def geo_audit(request: GEOAuditRequest):
    return score_content(request.content)


@app.post("/geo/url-audit")
def geo_url_audit(request: GEOUrlAuditRequest):
    try:
        url = _normalize_url(request.url)
        title, content = _fetch_page_text(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (HTTPError, URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {exc}") from exc

    if len(content.split()) < 20:
        raise HTTPException(
            status_code=422,
            detail="The page text is too short to audit. Try a content-rich page.",
        )

    score_result = score_content(content)
    return {
        "url": url,
        "title": title,
        "content_preview": content[:600],
        **score_result,
        "growth_plan": _build_growth_plan(score_result, title, url),
    }


@app.post("/geo/analyze")
def geo_analyze(request: GEOAnalyzeRequest):
    try:
        url = _normalize_url(request.url)
        title, content = _fetch_page_text(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (HTTPError, URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {exc}") from exc

    if len(content.split()) < 20:
        raise HTTPException(
            status_code=422,
            detail="The page text is too short to analyze. Try a content-rich public page.",
        )

    score_result = score_content(content)
    ai_package = _try_ai_content_package(url, title, content, request, score_result)
    if ai_package and ai_package.get("analysis_source") == "ai":
        package = ai_package
    else:
        package = _build_content_package(url, title, content, request, score_result)
        package["analysis_source"] = "rules"
        if ai_package and ai_package.get("ai_status"):
            package["ai_status"] = ai_package["ai_status"]
    task_id = _build_task_id(url)
    result = {
        "task_id": task_id,
        "status": "completed",
        "url": url,
        "title": title,
        "content_preview": content[:600],
        **score_result,
        "growth_plan": _build_growth_plan(score_result, title, url),
        **package,
    }
    existing_task = _db_get_task(task_id) or {}
    _db_upsert_task({
        **existing_task,
        "task_id": task_id,
        "url": url,
        "title": title,
        "status": "analyzed",
        "latest_result": result,
        "created_at": existing_task.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
    })
    return result


@app.post("/geo/improve")
def geo_improve(request: GEOImproveRequest):
    workflow = _build_improvement_workflow(request.result)

    if request.use_ai and request.provider:
        prompt = f"""
You are a GEO content injection editor.
Improve the following content package so it can be injected into a landing page CMS.
Keep all claims consistent with the provided page facts. Do not invent price, rating, inventory, or review counts.
Return strict JSON only with one top-level field: improved_modules.
Each improved module must include module_type, title, body, target_position, priority,
change_reason, and acceptance_check.

Content package:
{request.result}
"""
        try:
            output = MultiLLMClient().generate_text(
                provider=request.provider,  # type: ignore[arg-type]
                prompt=prompt,
                model=request.model,
                temperature=0.25,
            )
            ai_result = _extract_json_object(output)
            ai_modules = ai_result.get("improved_modules") or []
            if not ai_modules:
                raise ValueError("AI response did not include improved_modules.")

            normalized_modules = []
            for module in ai_modules:
                module_type = module.get("module_type", "content")
                normalized_modules.append(
                    {
                        **module,
                        "status": "draft",
                        "review_status": "pending_review",
                        "injection_field": module.get("injection_field") or f"cms.{module_type}",
                    }
                )
            workflow["improved_modules"] = normalized_modules
            workflow["injection_payload"] = _build_injection_payload(
                request.result.get("task_id", ""),
                request.result.get("url", ""),
                normalized_modules,
                "pending_review",
            )
            workflow["ai_status"] = "generated"
        except Exception as exc:
            workflow["ai_status"] = f"fallback_rules_used: {exc}"

    task_id = request.result.get("task_id")
    if task_id:
        task = _db_get_task(task_id) or {
            "task_id": task_id,
            "url": request.result.get("url"),
            "title": request.result.get("title"),
            "created_at": _now_iso(),
        }
        task["status"] = "draft_ready"
        task["latest_workflow"] = workflow
        task["updated_at"] = _now_iso()
        _db_upsert_task(task)

    return workflow


@app.post("/geo/version/save")
def geo_version_save(request: GEOVersionSaveRequest):
    if not request.modules:
        raise HTTPException(status_code=400, detail="No modules to save.")

    workflow = request.workflow or {}
    workflow = _update_stage_status(workflow, "review", "pending")
    version_id = _build_version_id(request.task_id)
    payload = _build_injection_payload(
        request.task_id,
        request.url,
        request.modules,
        "pending_review",
    )
    payload["version_id"] = version_id
    payload["version_status"] = "pending_review"

    version = {
        "version_id": version_id,
        "task_id": request.task_id,
        "url": request.url,
        "status": "pending_review",
        "editor": request.editor,
        "modules": request.modules,
        "workflow": workflow,
        "injection_payload": payload,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _db_save_version(version)

    task = _db_get_task(request.task_id) or {"task_id": request.task_id, "url": request.url, "created_at": _now_iso()}
    task["status"] = "pending_review"
    task["latest_version_id"] = version_id
    task["latest_workflow"] = workflow
    task["updated_at"] = _now_iso()
    _db_upsert_task(task)

    return version


@app.post("/geo/version/review")
def geo_version_review(request: GEOReviewRequest):
    version = _db_get_version(request.version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found.")

    if request.action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Action must be approve or reject.")
    if version.get("status") not in {"pending_review", "rejected"}:
        raise HTTPException(
            status_code=409,
            detail=f"Version in status {version.get('status')} cannot be reviewed again.",
        )

    status = "approved" if request.action == "approve" else "rejected"
    workflow = version.get("workflow") or {}
    workflow = _update_stage_status(workflow, "review", "completed" if status == "approved" else "rejected")
    workflow = _update_stage_status(workflow, "inject", "ready" if status == "approved" else "blocked")
    payload = version.get("injection_payload") or {}
    payload["version_status"] = status

    version.update(
        {
            "status": status,
            "reviewer": request.reviewer,
            "review_comment": request.comment,
            "approved_at": _now_iso() if status == "approved" else None,
            "workflow": workflow,
            "injection_payload": payload,
            "updated_at": _now_iso(),
        }
    )

    _db_save_version(version)

    task_id = version.get("task_id")
    task = _db_get_task(task_id) if task_id else None
    if task:
        task["status"] = status
        task["latest_version_id"] = request.version_id
        task["latest_workflow"] = workflow
        task["updated_at"] = _now_iso()
        _db_upsert_task(task)

    return version


@app.post("/geo/inject")
def geo_inject(request: GEOInjectRequest):
    version = _db_get_version(request.version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found.")
    if version.get("status") != "approved":
        raise HTTPException(status_code=409, detail="Only approved versions can be injected.")
    if request.target not in {"json_file", "webhook"}:
        raise HTTPException(status_code=400, detail="Target must be json_file or webhook.")

    injection_id = _build_injection_id(request.version_id)
    created_at = _now_iso()
    injection = {
        "injection_id": injection_id,
        "task_id": version["task_id"],
        "version_id": version["version_id"],
        "url": version["url"],
        "target": request.target,
        "status": "running",
        "created_at": created_at,
    }
    _db_save_injection(injection)

    payload = {
        "injection_id": injection_id,
        "task_id": version["task_id"],
        "version_id": version["version_id"],
        "url": version["url"],
        "approved_at": version.get("approved_at"),
        "payload": version.get("injection_payload") or {},
    }
    try:
        if request.target == "json_file":
            file_path = EXPORT_DIR / f"{injection_id}.json"
            file_path.write_text(_json_dumps(payload), encoding="utf-8")
            injection["artifact_path"] = str(file_path)
            injection["response_summary"] = "Approved payload written to JSON delivery artifact."
        else:
            if not request.webhook_url:
                raise ValueError("webhook_url is required for webhook target.")
            webhook_url = _validate_public_webhook_url(request.webhook_url)
            headers = {"Content-Type": "application/json", **(request.headers or {})}
            webhook_request = Request(
                webhook_url,
                data=_json_dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urlopen(webhook_request, timeout=15) as response:
                response_body = response.read(1000).decode("utf-8", errors="ignore")
                injection["response_summary"] = (
                    f"HTTP {response.status}: {response_body[:500]}" if response_body else f"HTTP {response.status}"
                )
        injection["status"] = "completed"
        injection["completed_at"] = _now_iso()
    except (ValueError, HTTPError, URLError, TimeoutError) as exc:
        injection["status"] = "failed"
        injection["response_summary"] = str(exc)
        injection["completed_at"] = _now_iso()
        _db_save_injection(injection)
        raise HTTPException(status_code=502, detail=f"Injection failed: {exc}") from exc

    _db_save_injection(injection)
    workflow = version.get("workflow") or {}
    workflow = _update_stage_status(workflow, "inject", "completed")
    workflow = _update_stage_status(workflow, "retest", "ready")
    version["workflow"] = workflow
    version["updated_at"] = _now_iso()
    _db_save_version(version)

    task = _db_get_task(version["task_id"])
    if task:
        task["status"] = "injected"
        task["latest_workflow"] = workflow
        task["updated_at"] = _now_iso()
        _db_upsert_task(task)
    return injection


@app.post("/geo/retest")
def geo_retest(request: GEORetestRequest):
    injection = None
    if request.injection_id:
        injection = _db_get_injection(request.injection_id)
        if not injection or injection.get("task_id") != request.task_id:
            raise HTTPException(status_code=404, detail="Injection record not found for this task.")
        if injection.get("status") != "completed":
            raise HTTPException(status_code=409, detail="Retest requires a completed injection.")
    else:
        injection = _db_latest_successful_injection(request.task_id, request.version_id)
    if not injection:
        raise HTTPException(
            status_code=409,
            detail="Retest requires a completed injection or delivery record.",
        )

    try:
        url = _normalize_url(request.url)
        if url != injection.get("url"):
            raise HTTPException(
                status_code=409,
                detail="Retest URL must match the URL of the completed injection.",
            )
        title, content = _fetch_page_text(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (HTTPError, URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch URL: {exc}") from exc

    score_result = score_content(content)
    current_score = int(score_result.get("geo_score") or 0)
    delta = current_score - int(request.previous_score or 0)
    retest = {
        "task_id": request.task_id,
        "version_id": injection.get("version_id"),
        "injection_id": injection.get("injection_id"),
        "url": url,
        "title": title,
        "previous_score": request.previous_score,
        "current_score": current_score,
        "score_delta": delta,
        "breakdown": score_result.get("breakdown"),
        "recommendations": score_result.get("recommendations"),
        "status": "improved" if delta > 0 else "needs_more_work",
        "created_at": _now_iso(),
    }
    _db_add_retest(retest)

    task = _db_get_task(request.task_id)
    if task:
        task["status"] = "retested"
        task["latest_retest"] = retest
        workflow = task.get("latest_workflow") or {}
        task["latest_workflow"] = _update_stage_status(workflow, "retest", "completed")
        task["updated_at"] = _now_iso()
        _db_upsert_task(task)

    return retest


@app.get("/geo/history")
def geo_history():
    return _db_history()


@app.get("/geo/tasks/{task_id}")
def geo_task_detail(task_id: str):
    task = _db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    history = _db_history()
    return {
        "task": task,
        "versions": [item for item in history["versions"] if item["task_id"] == task_id],
        "injections": [item for item in history["injections"] if item["task_id"] == task_id],
        "retests": history["retests"].get(task_id, []),
    }


@app.get("/geo/versions/{version_id}")
def geo_version_detail(version_id: str):
    version = _db_get_version(version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found.")
    return version


@app.post("/geo/export/json")
def geo_export_json(request: GEOExportRequest):
    if not request.payload:
        raise HTTPException(status_code=400, detail="No payload to export.")

    export_id = f"{request.task_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    file_path = EXPORT_DIR / f"{export_id}.json"
    export_payload = {
        "export_id": export_id,
        "target": request.target,
        "task_id": request.task_id,
        "created_at": _now_iso(),
        "payload": request.payload,
    }
    file_path.write_text(_json_dumps(export_payload), encoding="utf-8")
    return {
        "export_id": export_id,
        "target": request.target,
        "file_path": str(file_path),
        "payload": export_payload,
    }


@app.post("/llm/generate")
def llm_generate(request: LLMGenerateRequest):
    client = MultiLLMClient()
    try:
        output = client.generate_text(
            provider=request.provider,  # type: ignore[arg-type]
            prompt=request.prompt,
            model=request.model,
            temperature=request.temperature,
        )
        return {
            "provider": request.provider,
            "model": request.model,
            "output": output,
        }
    except (LLMProviderError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM call failed: {exc}") from exc


@app.post("/geo/rewrite")
def geo_rewrite(request: GEORewriteRequest):
    prompt = f"""
You are a GEO expert and marketing strategist.
Rewrite the following content so AI systems can better understand, quote, compare, and recommend it.

Rules:
- Use a clear definition first.
- Add structured headings.
- Add FAQ-style sections where useful.
- Add comparison-ready wording.
- Make claims specific and easy to cite.
- Remove vague marketing language.

Content:
{request.content}
"""
    client = MultiLLMClient()
    try:
        output = client.generate_text(
            provider=request.provider,  # type: ignore[arg-type]
            prompt=prompt,
            model=request.model,
            temperature=0.25,
        )
        return {
            "provider": request.provider,
            "output": output,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"GEO rewrite failed: {exc}") from exc


@app.post("/geo/faq")
def geo_faq(request: GEOFAQRequest):
    prompt = f"""
You are building FAQ assets for GEO.
Generate {request.count} high-intent FAQ questions and answers.

For each FAQ, include:
- question
- user intent
- short AI-friendly answer
- proof needed

Product or service:
{request.product_description}
"""
    client = MultiLLMClient()
    try:
        output = client.generate_text(
            provider=request.provider,  # type: ignore[arg-type]
            prompt=prompt,
            model=request.model,
            temperature=0.35,
        )
        return {
            "provider": request.provider,
            "output": output,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"FAQ generation failed: {exc}") from exc
