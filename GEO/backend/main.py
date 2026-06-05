import json
import hashlib
import ipaddress
import os
import re
import socket
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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
    from backend.auth import (
        current_identity,
        extract_api_key,
        has_role,
        required_role,
        reset_current_identity,
        resolve_identity,
        set_current_identity,
    )
except ImportError:
    from auth import (
        current_identity,
        extract_api_key,
        has_role,
        required_role,
        reset_current_identity,
        resolve_identity,
        set_current_identity,
    )

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


@app.middleware("http")
async def api_key_rbac(request: FastAPIRequest, call_next):
    role = required_role(request.method, request.url.path)
    if role is None:
        return await call_next(request)

    api_key = extract_api_key(
        request.headers.get("authorization"),
        request.headers.get("x-geo-api-key"),
    )
    try:
        identity = resolve_identity(api_key)
    except ValueError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    if identity is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "API key required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not has_role(identity, role):
        try:
            _db_add_audit(
                identity.name,
                "access_denied",
                "endpoint",
                request.url.path,
                outcome="denied",
                detail={"method": request.method, "required_role": role, "actor_role": identity.role},
            )
        except (OSError, sqlite3.Error):
            pass
        return JSONResponse(status_code=403, content={"detail": f"Role {role} or higher required."})

    context_token = set_current_identity(identity)
    try:
        response = await call_next(request)
        response.headers["X-GEO-Actor"] = identity.name
        response.headers["X-GEO-Role"] = identity.role
        return response
    finally:
        reset_current_identity(context_token)

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


class GEORetestScheduleRequest(GEORetestRequest):
    run_at: str | None = None
    max_attempts: int = 3


class GEOInjectRequest(BaseModel):
    version_id: str
    target: str = "json_file"
    webhook_url: str | None = None
    headers: dict[str, str] | None = None


class GEOExportRequest(BaseModel):
    task_id: str
    payload: dict
    target: str = "json_file"


class GEOProjectUpdateRequest(BaseModel):
    owner: str | None = None
    target_score: int | None = None
    todos: list[str] | None = None


class CMSPublishTargetRequest(BaseModel):
    name: str
    webhook_url: str
    environment: str = "staging"
    auth_header: str = "Authorization"
    auth_env_var: str | None = None
    enabled: bool = True


class CMSPublishPreviewRequest(BaseModel):
    version_id: str
    target_id: str


class CMSPublishConfirmRequest(BaseModel):
    publication_id: str
    confirmation: str


class CMSPublicationVerifyRequest(BaseModel):
    publication_id: str
    expected_terms: list[str] | None = None
    notes: str | None = None


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


class GEOKnowledgeItemRequest(BaseModel):
    brand: str
    category: str = "positioning"
    title: str
    content: str
    source: str | None = None
    status: str = "approved"


class GEOFeedbackRequest(BaseModel):
    task_id: str
    version_id: str | None = None
    publication_id: str | None = None
    verdict: str
    notes: str
    source: str = "miniapp"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_actor() -> str:
    return current_identity().name


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
                owner TEXT,
                target_score INTEGER,
                todos TEXT,
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
                effect_details TEXT,
                created_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cms_targets (
                target_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                webhook_url TEXT NOT NULL,
                environment TEXT NOT NULL,
                auth_header TEXT,
                auth_env_var TEXT,
                enabled INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publications (
                publication_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                status TEXT NOT NULL,
                preview TEXT NOT NULL,
                quality_report TEXT NOT NULL,
                injection_id TEXT,
                confirmed_by TEXT,
                confirmed_at TEXT,
                response_summary TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_items (
                knowledge_id TEXT PRIMARY KEY,
                brand TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_entries (
                feedback_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                version_id TEXT,
                publication_id TEXT,
                verdict TEXT NOT NULL,
                notes TEXT NOT NULL,
                source TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS llm_logs (
                log_id TEXT PRIMARY KEY,
                task_id TEXT,
                action TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT,
                status TEXT NOT NULL,
                prompt_excerpt TEXT,
                response_excerpt TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                task_id TEXT,
                outcome TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT,
                attempts INTEGER NOT NULL,
                max_attempts INTEGER NOT NULL,
                run_at TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
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
        if "effect_details" not in retest_columns:
            conn.execute("ALTER TABLE retests ADD COLUMN effect_details TEXT")
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        for column, definition in {
            "owner": "TEXT",
            "target_score": "INTEGER",
            "todos": "TEXT",
        }.items():
            if column not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        version_columns = {row["name"] for row in conn.execute("PRAGMA table_info(versions)").fetchall()}
        if "quality_report" not in version_columns:
            conn.execute("ALTER TABLE versions ADD COLUMN quality_report TEXT")
        publication_columns = {row["name"] for row in conn.execute("PRAGMA table_info(publications)").fetchall()}
        for column, definition in {
            "live_status": "TEXT",
            "live_summary": "TEXT",
            "live_confirmed_by": "TEXT",
            "live_confirmed_at": "TEXT",
        }.items():
            if column not in publication_columns:
                conn.execute(f"ALTER TABLE publications ADD COLUMN {column} {definition}")


def _db_upsert_task(task: dict) -> None:
    TASK_STORE[task["task_id"]] = task
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, url, title, status, latest_result, latest_workflow,
                latest_version_id, latest_retest, owner, target_score, todos, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                url=excluded.url,
                title=excluded.title,
                status=excluded.status,
                latest_result=excluded.latest_result,
                latest_workflow=excluded.latest_workflow,
                latest_version_id=excluded.latest_version_id,
                latest_retest=excluded.latest_retest,
                owner=excluded.owner,
                target_score=excluded.target_score,
                todos=excluded.todos,
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
                task.get("owner"),
                task.get("target_score"),
                _json_dumps(task.get("todos", [])),
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
        "owner": row["owner"],
        "target_score": row["target_score"],
        "todos": _json_loads(row["todos"], []),
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
        "quality_report": _json_loads(row["quality_report"], None),
    }


def _db_save_version(version: dict) -> None:
    VERSION_STORE[version["version_id"]] = version
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO versions (
                version_id, task_id, url, status, editor, reviewer, review_comment,
                modules, workflow, injection_payload, created_at, updated_at, approved_at, quality_report
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version_id) DO UPDATE SET
                status=excluded.status,
                reviewer=excluded.reviewer,
                review_comment=excluded.review_comment,
                modules=excluded.modules,
                workflow=excluded.workflow,
                injection_payload=excluded.injection_payload,
                updated_at=excluded.updated_at,
                approved_at=excluded.approved_at
                ,quality_report=excluded.quality_report
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
                _json_dumps(version.get("quality_report")) if version.get("quality_report") else None,
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
                status, breakdown, recommendations, effect_details, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _json_dumps(retest.get("effect_details", {})),
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


def _db_add_audit(
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    task_id: str | None = None,
    outcome: str = "success",
    detail: dict | None = None,
) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs (
                actor, action, entity_type, entity_id, task_id, outcome, detail, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor,
                action,
                entity_type,
                entity_id,
                task_id,
                outcome,
                _json_dumps(detail or {}),
                _now_iso(),
            ),
        )


def _db_audit_logs(
    limit: int = 50,
    task_id: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    actor: str | None = None,
) -> list[dict]:
    query = "SELECT * FROM audit_logs"
    params: list = []
    filters: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if action:
        filters.append("action = ?")
        params.append(action)
    if outcome:
        filters.append("outcome = ?")
        params.append(outcome)
    if actor:
        filters.append("actor = ?")
        params.append(actor)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "actor": row["actor"],
            "action": row["action"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "task_id": row["task_id"],
            "outcome": row["outcome"],
            "detail": _json_loads(row["detail"], {}),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _job_from_row(row: sqlite3.Row) -> dict:
    return {
        "job_id": row["job_id"],
        "job_type": row["job_type"],
        "status": row["status"],
        "payload": _json_loads(row["payload"], {}),
        "result": _json_loads(row["result"], None),
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "run_at": row["run_at"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def _db_save_job(job: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, job_type, status, payload, result, attempts, max_attempts,
                run_at, last_error, created_at, updated_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                result=excluded.result,
                attempts=excluded.attempts,
                max_attempts=excluded.max_attempts,
                run_at=excluded.run_at,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at
            """,
            (
                job["job_id"],
                job["job_type"],
                job["status"],
                _json_dumps(job["payload"]),
                _json_dumps(job.get("result")) if job.get("result") is not None else None,
                job["attempts"],
                job["max_attempts"],
                job["run_at"],
                job.get("last_error"),
                job["created_at"],
                job["updated_at"],
                job.get("completed_at"),
            ),
        )


def _db_get_job(job_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _job_from_row(row) if row else None


def _db_jobs(status: str | None = None, limit: int = 50, due_only: bool = False) -> list[dict]:
    query = "SELECT * FROM jobs"
    filters: list[str] = []
    params: list = []
    if status:
        filters.append("status = ?")
        params.append(status)
    if due_only:
        filters.append("status IN ('queued', 'retry_wait')")
        filters.append("run_at <= ?")
        params.append(_now_iso())
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_job_from_row(row) for row in rows]


def _quality_report(modules: list[dict], workflow: dict | None = None) -> dict:
    issues: list[dict] = []
    blocking_claim = re.compile(r"\b(guarantee(?:d)?|always|never|number\s*one|#1|100%)\b", re.I)
    warning_claim = re.compile(r"\b(best|cheapest)\b", re.I)
    required_fields = ("module_type", "title", "body", "target_position")
    fact_checks: list[dict] = []
    for index, module in enumerate(modules):
        for field in required_fields:
            if not str(module.get(field) or "").strip():
                issues.append({"severity": "blocking", "code": "missing_field", "module": index, "field": field})
        body = str(module.get("body") or "")
        if len(body.strip()) < 24:
            issues.append({"severity": "blocking", "code": "body_too_short", "module": index})
        if blocking_claim.search(body):
            issues.append({"severity": "blocking", "code": "unsupported_sensitive_claim", "module": index})
        elif warning_claim.search(body):
            issues.append({"severity": "warning", "code": "review_comparative_claim", "module": index})
        fact_checks.append(
            {
                "module": index,
                "title_present": bool(str(module.get("title") or "").strip()),
                "body_length": len(body.strip()),
                "status": "review" if warning_claim.search(body) else "pass",
            }
        )

    schemas = (workflow or {}).get("schema_suggestions") or []
    schema_checks: list[dict] = []
    for index, schema in enumerate(schemas):
        schema_json = schema.get("json") if isinstance(schema, dict) else None
        if schema_json and (not schema_json.get("@context") or not schema_json.get("@type")):
            issues.append({"severity": "blocking", "code": "invalid_schema", "schema": index})
        schema_checks.append(
            {
                "schema": index,
                "schema_type": (schema or {}).get("schema_type"),
                "status": "pass" if schema_json and schema_json.get("@context") and schema_json.get("@type") else "invalid",
            }
        )

    blocking = [item for item in issues if item["severity"] == "blocking"]
    warnings = [item for item in issues if item["severity"] == "warning"]
    score = max(0, 100 - len(blocking) * 20 - len(warnings) * 5)
    return {
        "status": "passed" if not blocking else "blocked",
        "score": score,
        "checks": {
            "module_completeness": not any(item["code"] in {"missing_field", "body_too_short"} for item in blocking),
            "sensitive_claims": not any(item["code"] == "unsupported_sensitive_claim" for item in blocking),
            "schema_validity": not any(item["code"] == "invalid_schema" for item in blocking),
        },
        "fact_checks": fact_checks,
        "schema_checks": schema_checks,
        "issues": issues,
        "checked_at": _now_iso(),
    }


def _log_llm_call(
    *,
    action: str,
    provider: str,
    model: str | None,
    status: str,
    prompt: str,
    task_id: str | None = None,
    response: str | None = None,
    error: str | None = None,
) -> None:
    _db_add_llm_log(
        {
            "log_id": f"log_{uuid.uuid4().hex[:16]}",
            "task_id": task_id,
            "action": action,
            "provider": provider,
            "model": model,
            "status": status,
            "prompt_excerpt": prompt[:800],
            "response_excerpt": (response or "")[:800] or None,
            "error_message": error,
            "created_at": _now_iso(),
        }
    )


def _knowledge_context(url: str, title: str, limit: int = 4) -> list[dict]:
    hostname = (urlparse(url).hostname or "").lower()
    title_text = title.lower()
    matches = []
    for item in _db_knowledge_items(status="approved", limit=50):
        brand = (item.get("brand") or "").strip().lower()
        if not brand:
            continue
        if brand in hostname or brand in title_text:
            matches.append(item)
    return matches[:limit]


def _project_view(task: dict, versions: list[dict] | None = None, injections: list[dict] | None = None, retests: list[dict] | None = None) -> dict:
    versions = versions or []
    injections = injections or []
    retests = retests or []
    latest_version = versions[0] if versions else None
    latest_injection = injections[0] if injections else None
    latest_retest = retests[0] if retests else task.get("latest_retest")
    score = int((task.get("latest_result") or {}).get("geo_score") or 0)
    target_score = int(task.get("target_score") or 80)
    status = task.get("status") or "analyzed"
    action_map = {
        "analyzed": ("生成改进内容", "improve"),
        "draft_ready": ("保存待审核版本", "save_version"),
        "pending_review": ("人工审核版本", "review"),
        "approved": ("创建发布预览", "publish_preview"),
        "injected": ("安排发布后复测", "schedule_retest"),
        "retested": ("根据效果继续优化", "improve"),
    }
    next_action, next_action_key = action_map.get(status, ("检查项目异常", "inspect"))
    if latest_version and (latest_version.get("quality_report") or {}).get("status") == "blocked":
        next_action, next_action_key = "修复内容质量问题", "fix_quality"
    effect = "尚未复测"
    if latest_retest:
        delta = int(latest_retest.get("score_delta") or 0)
        effect = "有效优化" if delta > 0 else "未见提升"
    todos = task.get("todos") or []
    if not todos:
        todos = [next_action]
        if score < target_score:
            todos.append(f"将 GEO 分数从 {score} 提升到 {target_score}")
    return {
        **task,
        "project_id": task["task_id"],
        "owner": task.get("owner") or "待分配",
        "target_score": target_score,
        "current_stage": status,
        "next_action": next_action,
        "next_action_key": next_action_key,
        "todos": todos,
        "effectiveness": effect,
        "latest_version": latest_version,
        "latest_injection": latest_injection,
        "latest_retest": latest_retest,
    }


def _db_cms_targets() -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM cms_targets ORDER BY updated_at DESC").fetchall()
    return [
        {
            "target_id": row["target_id"],
            "name": row["name"],
            "webhook_url": row["webhook_url"],
            "environment": row["environment"],
            "auth_header": row["auth_header"],
            "auth_env_var": row["auth_env_var"],
            "credential_configured": bool(row["auth_env_var"] and os.getenv(row["auth_env_var"])),
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _db_get_cms_target(target_id: str) -> dict | None:
    return next((item for item in _db_cms_targets() if item["target_id"] == target_id), None)


def _db_save_knowledge_item(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_items (
                knowledge_id, brand, category, title, content, source, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(knowledge_id) DO UPDATE SET
                brand=excluded.brand,
                category=excluded.category,
                title=excluded.title,
                content=excluded.content,
                source=excluded.source,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                item["knowledge_id"],
                item["brand"],
                item["category"],
                item["title"],
                item["content"],
                item.get("source"),
                item["status"],
                item["created_at"],
                item["updated_at"],
            ),
        )


def _db_knowledge_items(status: str | None = None, brand: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM knowledge_items"
    filters: list[str] = []
    params: list[str | int] = []
    if status:
        filters.append("status = ?")
        params.append(status)
    if brand:
        filters.append("brand = ?")
        params.append(brand)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "knowledge_id": row["knowledge_id"],
            "brand": row["brand"],
            "category": row["category"],
            "title": row["title"],
            "content": row["content"],
            "source": row["source"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _db_add_feedback(entry: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO feedback_entries (
                feedback_id, task_id, version_id, publication_id, verdict, notes, source, actor, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["feedback_id"],
                entry["task_id"],
                entry.get("version_id"),
                entry.get("publication_id"),
                entry["verdict"],
                entry["notes"],
                entry["source"],
                entry["actor"],
                entry["created_at"],
            ),
        )


def _db_feedback(task_id: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM feedback_entries"
    params: list[str | int] = []
    if task_id:
        query += " WHERE task_id = ?"
        params.append(task_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "feedback_id": row["feedback_id"],
            "task_id": row["task_id"],
            "version_id": row["version_id"],
            "publication_id": row["publication_id"],
            "verdict": row["verdict"],
            "notes": row["notes"],
            "source": row["source"],
            "actor": row["actor"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _db_add_llm_log(entry: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO llm_logs (
                log_id, task_id, action, provider, model, status, prompt_excerpt, response_excerpt,
                error_message, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["log_id"],
                entry.get("task_id"),
                entry["action"],
                entry["provider"],
                entry.get("model"),
                entry["status"],
                entry.get("prompt_excerpt"),
                entry.get("response_excerpt"),
                entry.get("error_message"),
                entry["created_at"],
            ),
        )


def _db_llm_logs(task_id: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM llm_logs"
    params: list[str | int] = []
    if task_id:
        query += " WHERE task_id = ?"
        params.append(task_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "log_id": row["log_id"],
            "task_id": row["task_id"],
            "action": row["action"],
            "provider": row["provider"],
            "model": row["model"],
            "status": row["status"],
            "prompt_excerpt": row["prompt_excerpt"],
            "response_excerpt": row["response_excerpt"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _db_save_publication(publication: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO publications (
                publication_id, task_id, version_id, target_id, status, preview,
                quality_report, injection_id, confirmed_by, confirmed_at,
                response_summary, live_status, live_summary, live_confirmed_by,
                live_confirmed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(publication_id) DO UPDATE SET
                status=excluded.status,
                injection_id=excluded.injection_id,
                confirmed_by=excluded.confirmed_by,
                confirmed_at=excluded.confirmed_at,
                response_summary=excluded.response_summary,
                live_status=excluded.live_status,
                live_summary=excluded.live_summary,
                live_confirmed_by=excluded.live_confirmed_by,
                live_confirmed_at=excluded.live_confirmed_at,
                updated_at=excluded.updated_at
            """,
            (
                publication["publication_id"], publication["task_id"], publication["version_id"],
                publication["target_id"], publication["status"], _json_dumps(publication["preview"]),
                _json_dumps(publication["quality_report"]), publication.get("injection_id"),
                publication.get("confirmed_by"), publication.get("confirmed_at"),
                publication.get("response_summary"), publication.get("live_status"),
                _json_dumps(publication.get("live_summary")) if publication.get("live_summary") is not None else None,
                publication.get("live_confirmed_by"),
                publication.get("live_confirmed_at"), publication["created_at"], publication["updated_at"],
            ),
        )


def _db_publications(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM publications"
    params: list[str] = []
    if task_id:
        query += " WHERE task_id = ?"
        params.append(task_id)
    query += " ORDER BY created_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "publication_id": row["publication_id"], "task_id": row["task_id"],
            "version_id": row["version_id"], "target_id": row["target_id"], "status": row["status"],
            "preview": _json_loads(row["preview"], {}), "quality_report": _json_loads(row["quality_report"], {}),
            "injection_id": row["injection_id"], "confirmed_by": row["confirmed_by"],
            "confirmed_at": row["confirmed_at"], "response_summary": row["response_summary"],
            "live_status": row["live_status"], "live_summary": _json_loads(row["live_summary"], None),
            "live_confirmed_by": row["live_confirmed_by"], "live_confirmed_at": row["live_confirmed_at"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _db_get_publication(publication_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM publications WHERE publication_id = ?", (publication_id,)).fetchone()
    if not row:
        return None
    return {
        "publication_id": row["publication_id"],
        "task_id": row["task_id"],
        "version_id": row["version_id"],
        "target_id": row["target_id"],
        "status": row["status"],
        "preview": _json_loads(row["preview"], {}),
        "quality_report": _json_loads(row["quality_report"], {}),
        "injection_id": row["injection_id"],
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
        "response_summary": row["response_summary"],
        "live_status": row["live_status"],
        "live_summary": _json_loads(row["live_summary"], None),
        "live_confirmed_by": row["live_confirmed_by"],
        "live_confirmed_at": row["live_confirmed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
def _run_job(job_id: str) -> dict:
    job = _db_get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] not in {"queued", "retry_wait"}:
        return job
    if job["run_at"] > _now_iso():
        raise HTTPException(status_code=409, detail="Job is not due yet.")

    claimed_at = _now_iso()
    with _db() as conn:
        claimed = conn.execute(
            """
            UPDATE jobs
            SET status = 'running', attempts = attempts + 1, updated_at = ?
            WHERE job_id = ?
              AND status IN ('queued', 'retry_wait')
              AND run_at <= ?
            """,
            (claimed_at, job_id, claimed_at),
        ).rowcount
    if not claimed:
        return _db_get_job(job_id) or job
    job = _db_get_job(job_id) or job
    try:
        if job["job_type"] != "retest":
            raise ValueError(f"Unsupported job type: {job['job_type']}")
        job["result"] = geo_retest(GEORetestRequest(**job["payload"]))
        job["status"] = "completed"
        job["last_error"] = None
        job["completed_at"] = _now_iso()
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        job["last_error"] = str(detail)
        if job["attempts"] < job["max_attempts"]:
            job["status"] = "retry_wait"
            job["run_at"] = (datetime.now(timezone.utc) + timedelta(minutes=5 * job["attempts"])).isoformat()
        else:
            job["status"] = "failed"
            job["completed_at"] = _now_iso()
    job["updated_at"] = _now_iso()
    _db_save_job(job)
    _db_add_audit(
        _current_actor(),
        "run_job",
        "job",
        job_id,
        job["payload"].get("task_id"),
        outcome=job["status"],
        detail={"job_type": job["job_type"], "attempts": job["attempts"], "error": job.get("last_error")},
    )
    return job


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
            "effect_details": _json_loads(row["effect_details"], {}),
            "created_at": row["created_at"],
        }
        retests.setdefault(row["task_id"], []).append(item)

    return {
        "tasks": [_task_from_row(row) for row in task_rows],
        "versions": [_version_from_row(row) for row in version_rows],
        "retests": retests,
        "injections": [_injection_from_row(row) for row in injection_rows],
        "publications": _db_publications(),
        "cms_targets": _db_cms_targets(),
        "knowledge_items": _db_knowledge_items(limit=200),
        "feedback_entries": _db_feedback(limit=200),
        "llm_logs": _db_llm_logs(limit=200),
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

    knowledge_items = _knowledge_context(url, title)
    knowledge_block = "\n".join(
        f"- [{item['category']}] {item['title']}: {item['content'][:220]}"
        for item in knowledge_items
    ) or "- none"
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

Brand knowledge:
{knowledge_block}

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
        _log_llm_call(
            action="geo_analyze",
            provider=request.provider,
            model=request.model,
            status="success",
            prompt=prompt,
            task_id=_build_task_id(url),
            response=output,
        )
        package = _extract_json_object(output)
        package = _validate_content_package(package)
        package["ai_status"] = "generated"
        package["analysis_source"] = "ai"
        package["knowledge_snapshot"] = knowledge_items
        return package
    except Exception as exc:
        _log_llm_call(
            action="geo_analyze",
            provider=request.provider,
            model=request.model,
            status="failed",
            prompt=prompt,
            task_id=_build_task_id(url),
            error=str(exc),
        )
        return {
            "analysis_source": "rules",
            "ai_status": f"fallback_rules_used: {exc}",
            "knowledge_snapshot": knowledge_items,
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
    detail = geo_task_detail(task_id)
    detail["audit_logs"] = _db_audit_logs(limit=100, task_id=task_id)
    return detail


@app.get("/admin/api/audit-logs")
def admin_audit_logs(
    limit: int = 50,
    task_id: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    actor: str | None = None,
):
    return {
        "items": _db_audit_logs(
            limit=limit,
            task_id=task_id,
            action=action,
            outcome=outcome,
            actor=actor,
        )
    }


@app.get("/admin/api/knowledge")
def admin_knowledge(status: str | None = None, brand: str | None = None, limit: int = 100):
    return {"items": _db_knowledge_items(status=status, brand=brand, limit=limit)}


@app.get("/admin/api/feedback")
def admin_feedback(task_id: str | None = None, limit: int = 100):
    return {"items": _db_feedback(task_id=task_id, limit=limit)}


@app.get("/admin/api/llm-logs")
def admin_llm_logs(task_id: str | None = None, limit: int = 100):
    return {"items": _db_llm_logs(task_id=task_id, limit=limit)}


@app.get("/admin/api/jobs")
def admin_jobs(status: str | None = None, limit: int = 50):
    return {"items": _db_jobs(status=status, limit=limit)}


@app.post("/admin/api/jobs/run-due")
def admin_run_due_jobs(limit: int = 20):
    jobs = _db_jobs(limit=limit, due_only=True)
    return {"items": [_run_job(job["job_id"]) for job in reversed(jobs)]}


@app.post("/admin/api/jobs/{job_id}/retry")
def admin_retry_job(job_id: str, background_tasks: BackgroundTasks):
    job = _db_get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] not in {"failed", "retry_wait"}:
        raise HTTPException(status_code=409, detail="Only failed or waiting jobs can be retried.")
    job["status"] = "queued"
    job["attempts"] = 0
    job["run_at"] = _now_iso()
    job["last_error"] = None
    job["updated_at"] = _now_iso()
    _db_save_job(job)
    background_tasks.add_task(_run_job, job_id)
    _db_add_audit(
        _current_actor(),
        "retry_job",
        "job",
        job_id,
        job["payload"].get("task_id"),
        detail={"max_attempts": job["max_attempts"], "attempts_reset": True},
    )
    return job


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
        package["knowledge_snapshot"] = (ai_package or {}).get("knowledge_snapshot") or _knowledge_context(url, title)
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
    stage_order = {
        "analyzed": 10,
        "draft_ready": 20,
        "pending_review": 30,
        "approved": 40,
        "injected": 50,
        "retested": 60,
    }
    existing_status = existing_task.get("status") or "analyzed"
    next_status = existing_status if stage_order.get(existing_status, 0) > stage_order["analyzed"] else "analyzed"
    _db_upsert_task({
        **existing_task,
        "task_id": task_id,
        "url": url,
        "title": title,
        "status": next_status,
        "latest_result": result,
        "created_at": existing_task.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
    })
    _db_add_audit(_current_actor(), "analyze", "task", task_id, task_id, detail={"url": url, "score": result["geo_score"]})
    return result


@app.post("/geo/improve")
def geo_improve(request: GEOImproveRequest):
    workflow = _build_improvement_workflow(request.result)
    task_id = request.result.get("task_id")
    feedback_notes = [item["notes"] for item in _db_feedback(task_id=task_id, limit=5)] if task_id else []
    if request.result.get("knowledge_snapshot"):
        workflow["knowledge_snapshot"] = request.result.get("knowledge_snapshot")
    if feedback_notes:
        workflow["feedback_snapshot"] = feedback_notes

    if request.use_ai and request.provider:
        knowledge_block = "\n".join(
            f"- {item.get('title')}: {item.get('content')}"
            for item in workflow.get("knowledge_snapshot") or []
        ) or "- none"
        feedback_block = "\n".join(f"- {note}" for note in feedback_notes) or "- none"
        prompt = f"""
You are a GEO content injection editor.
Improve the following content package so it can be injected into a landing page CMS.
Keep all claims consistent with the provided page facts. Do not invent price, rating, inventory, or review counts.
Return strict JSON only with one top-level field: improved_modules.
Each improved module must include module_type, title, body, target_position, priority,
change_reason, and acceptance_check.

Content package:
{request.result}

Brand knowledge:
{knowledge_block}

Recent operator feedback:
{feedback_block}
"""
        try:
            output = MultiLLMClient().generate_text(
                provider=request.provider,  # type: ignore[arg-type]
                prompt=prompt,
                model=request.model,
                temperature=0.25,
            )
            _log_llm_call(
                action="geo_improve",
                provider=request.provider,
                model=request.model,
                status="success",
                prompt=prompt,
                task_id=task_id,
                response=output,
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
            _log_llm_call(
                action="geo_improve",
                provider=request.provider,
                model=request.model,
                status="failed",
                prompt=prompt,
                task_id=task_id,
                error=str(exc),
            )
            workflow["ai_status"] = f"fallback_rules_used: {exc}"

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

    actor = _current_actor()
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
        "editor": actor,
        "modules": request.modules,
        "workflow": workflow,
        "injection_payload": payload,
        "quality_report": _quality_report(request.modules, workflow),
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
    _db_add_audit(
        actor,
        "save_version",
        "version",
        version_id,
        request.task_id,
        detail={"module_count": len(request.modules), "claimed_editor": request.editor},
    )

    return version


@app.post("/geo/version/review")
def geo_version_review(request: GEOReviewRequest):
    version = _db_get_version(request.version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found.")

    if request.action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Action must be approve or reject.")
    if request.action == "approve" and (version.get("quality_report") or {}).get("status") != "passed":
        raise HTTPException(status_code=409, detail="Version is blocked by content quality checks.")
    if version.get("status") not in {"pending_review", "rejected"}:
        raise HTTPException(
            status_code=409,
            detail=f"Version in status {version.get('status')} cannot be reviewed again.",
        )

    actor = _current_actor()
    status = "approved" if request.action == "approve" else "rejected"
    workflow = version.get("workflow") or {}
    workflow = _update_stage_status(workflow, "review", "completed" if status == "approved" else "rejected")
    workflow = _update_stage_status(workflow, "inject", "ready" if status == "approved" else "blocked")
    payload = version.get("injection_payload") or {}
    payload["version_status"] = status

    version.update(
        {
            "status": status,
            "reviewer": actor,
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
    _db_add_audit(
        actor,
        request.action,
        "version",
        request.version_id,
        task_id,
        detail={"comment": request.comment, "status": status, "claimed_reviewer": request.reviewer},
    )

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
        _db_add_audit(
            _current_actor(),
            "inject",
            "injection",
            injection_id,
            version["task_id"],
            outcome="failed",
            detail={"target": request.target, "error": str(exc)},
        )
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
    _db_add_audit(
        _current_actor(),
        "inject",
        "injection",
        injection_id,
        version["task_id"],
        outcome=injection["status"],
        detail={"target": request.target, "version_id": request.version_id},
    )
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
    task = _db_get_task(request.task_id)
    previous_breakdown = ((task or {}).get("latest_result") or {}).get("breakdown") or {}
    current_breakdown = score_result.get("breakdown") or {}
    dimension_deltas = {
        key: int(current_breakdown.get(key) or 0) - int(previous_breakdown.get(key) or 0)
        for key in set(previous_breakdown) | set(current_breakdown)
    }
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
        "effect_details": {
            "dimension_deltas": dimension_deltas,
            "improved_dimensions": [key for key, value in dimension_deltas.items() if value > 0],
            "declined_dimensions": [key for key, value in dimension_deltas.items() if value < 0],
            "verdict": "effective" if delta > 0 else "ineffective",
        },
        "status": "improved" if delta > 0 else "needs_more_work",
        "created_at": _now_iso(),
    }
    _db_add_retest(retest)

    if task:
        task["status"] = "retested"
        task["latest_retest"] = retest
        workflow = task.get("latest_workflow") or {}
        task["latest_workflow"] = _update_stage_status(workflow, "retest", "completed")
        task["updated_at"] = _now_iso()
        _db_upsert_task(task)
    _db_add_audit(
        _current_actor(),
        "retest",
        "task",
        request.task_id,
        request.task_id,
        detail={"injection_id": injection.get("injection_id"), "score_delta": delta},
    )

    return retest


@app.post("/geo/retest/schedule", status_code=202)
def geo_schedule_retest(request: GEORetestScheduleRequest, background_tasks: BackgroundTasks):
    max_attempts = max(1, min(request.max_attempts, 10))
    run_at = request.run_at or _now_iso()
    try:
        normalized_run_at = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
        if normalized_run_at.tzinfo is None:
            normalized_run_at = normalized_run_at.replace(tzinfo=timezone.utc)
        run_at = normalized_run_at.astimezone(timezone.utc).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="run_at must be a valid ISO datetime.") from exc

    job = {
        "job_id": f"job_{uuid.uuid4().hex[:16]}",
        "job_type": "retest",
        "status": "queued",
        "payload": request.dict(exclude={"run_at", "max_attempts"}),
        "result": None,
        "attempts": 0,
        "max_attempts": max_attempts,
        "run_at": run_at,
        "last_error": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "completed_at": None,
    }
    _db_save_job(job)
    _db_add_audit(
        _current_actor(),
        "schedule_retest",
        "job",
        job["job_id"],
        request.task_id,
        detail={"run_at": run_at, "max_attempts": max_attempts},
    )
    if run_at <= _now_iso():
        background_tasks.add_task(_run_job, job["job_id"])
    return job


@app.get("/geo/projects")
def geo_projects():
    history = _db_history()
    return {
        "items": [
            _project_view(
                task,
                [item for item in history["versions"] if item["task_id"] == task["task_id"]],
                [item for item in history["injections"] if item["task_id"] == task["task_id"]],
                history["retests"].get(task["task_id"], []),
            )
            for task in history["tasks"]
        ]
    }


@app.get("/geo/projects/{task_id}")
def geo_project_detail(task_id: str):
    detail = geo_task_detail(task_id)
    detail["project"] = _project_view(detail["task"], detail["versions"], detail["injections"], detail["retests"])
    detail["publications"] = _db_publications(task_id)
    return detail


@app.post("/geo/projects/{task_id}")
def geo_project_update(task_id: str, request: GEOProjectUpdateRequest):
    task = _db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Project not found.")
    if request.owner is not None:
        task["owner"] = request.owner.strip() or None
    if request.target_score is not None:
        task["target_score"] = max(1, min(request.target_score, 100))
    if request.todos is not None:
        task["todos"] = [item.strip() for item in request.todos if item.strip()][:20]
    task["updated_at"] = _now_iso()
    _db_upsert_task(task)
    _db_add_audit(_current_actor(), "update_project", "task", task_id, task_id)
    return _project_view(task)


@app.get("/geo/knowledge")
def geo_knowledge(status: str | None = None, brand: str | None = None, limit: int = 100):
    return {"items": _db_knowledge_items(status=status, brand=brand, limit=limit)}


@app.post("/geo/knowledge")
def geo_knowledge_save(request: GEOKnowledgeItemRequest):
    knowledge_id = f"kb_{hashlib.sha256(f'{request.brand}:{request.category}:{request.title}'.encode()).hexdigest()[:12]}"
    now = _now_iso()
    existing = next((item for item in _db_knowledge_items(limit=200) if item["knowledge_id"] == knowledge_id), None)
    item = {
        "knowledge_id": knowledge_id,
        "brand": request.brand.strip(),
        "category": request.category.strip() or "positioning",
        "title": request.title.strip(),
        "content": request.content.strip(),
        "source": (request.source or "").strip() or None,
        "status": request.status.strip() or "approved",
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    _db_save_knowledge_item(item)
    _db_add_audit(_current_actor(), "save_knowledge", "knowledge", knowledge_id)
    return item


@app.get("/geo/feedback")
def geo_feedback(task_id: str | None = None, limit: int = 100):
    return {"items": _db_feedback(task_id=task_id, limit=limit)}


@app.post("/geo/feedback")
def geo_feedback_save(request: GEOFeedbackRequest):
    feedback = {
        "feedback_id": f"fb_{uuid.uuid4().hex[:12]}",
        "task_id": request.task_id,
        "version_id": request.version_id,
        "publication_id": request.publication_id,
        "verdict": request.verdict.strip(),
        "notes": request.notes.strip(),
        "source": request.source.strip() or "miniapp",
        "actor": _current_actor(),
        "created_at": _now_iso(),
    }
    _db_add_feedback(feedback)
    _db_add_audit(
        _current_actor(),
        "save_feedback",
        "feedback",
        feedback["feedback_id"],
        request.task_id,
        detail={"verdict": request.verdict, "publication_id": request.publication_id},
    )
    return feedback


@app.post("/geo/versions/{version_id}/quality-check")
def geo_version_quality_check(version_id: str):
    version = _db_get_version(version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found.")
    version["quality_report"] = _quality_report(version.get("modules") or [], version.get("workflow") or {})
    version["updated_at"] = _now_iso()
    _db_save_version(version)
    _db_add_audit(
        _current_actor(), "quality_check", "version", version_id, version.get("task_id"),
        outcome=version["quality_report"]["status"],
        detail={"score": version["quality_report"]["score"], "issue_count": len(version["quality_report"]["issues"])},
    )
    return version["quality_report"]


@app.get("/cms/targets")
def cms_targets():
    return {"items": _db_cms_targets()}


@app.post("/cms/targets")
def cms_target_save(request: CMSPublishTargetRequest):
    try:
        webhook_url = _validate_public_webhook_url(request.webhook_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target_id = f"cms_{hashlib.sha256(f'{request.name}:{webhook_url}'.encode()).hexdigest()[:12]}"
    now = _now_iso()
    with _db() as conn:
        existing = conn.execute("SELECT created_at FROM cms_targets WHERE target_id = ?", (target_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO cms_targets (
                target_id, name, webhook_url, environment, auth_header, auth_env_var,
                enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                name=excluded.name, webhook_url=excluded.webhook_url,
                environment=excluded.environment, auth_header=excluded.auth_header,
                auth_env_var=excluded.auth_env_var, enabled=excluded.enabled,
                updated_at=excluded.updated_at
            """,
            (
                target_id, request.name.strip(), webhook_url, request.environment,
                request.auth_header, request.auth_env_var, int(request.enabled),
                existing["created_at"] if existing else now, now,
            ),
        )
    _db_add_audit(_current_actor(), "save_cms_target", "cms_target", target_id)
    return _db_get_cms_target(target_id)


@app.get("/cms/publications")
def cms_publications(task_id: str | None = None):
    return {"items": _db_publications(task_id)}


@app.post("/cms/publications/preview")
def cms_publication_preview(request: CMSPublishPreviewRequest):
    version = _db_get_version(request.version_id)
    target = _db_get_cms_target(request.target_id)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found.")
    if not target or not target["enabled"]:
        raise HTTPException(status_code=404, detail="Enabled CMS target not found.")
    if version.get("status") != "approved":
        raise HTTPException(status_code=409, detail="Only approved versions can create a publication preview.")
    quality = version.get("quality_report") or _quality_report(version.get("modules") or [], version.get("workflow") or {})
    if quality["status"] != "passed":
        raise HTTPException(status_code=409, detail="Publication is blocked by content quality checks.")
    publication = {
        "publication_id": f"pub_{uuid.uuid4().hex[:16]}",
        "task_id": version["task_id"],
        "version_id": version["version_id"],
        "target_id": target["target_id"],
        "status": "pending_confirmation",
        "preview": {
            "target_name": target["name"],
            "environment": target["environment"],
            "url": version["url"],
            "module_count": len(version.get("modules") or []),
            "modules": [{"module_type": item.get("module_type"), "title": item.get("title")} for item in version.get("modules") or []],
        },
        "quality_report": quality,
        "live_status": "pending",
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _db_save_publication(publication)
    _db_add_audit(_current_actor(), "preview_publish", "publication", publication["publication_id"], version["task_id"])
    return publication


@app.post("/cms/publications/confirm")
def cms_publication_confirm(request: CMSPublishConfirmRequest):
    publication = _db_get_publication(request.publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="Publication not found.")
    if publication["status"] != "pending_confirmation":
        raise HTTPException(status_code=409, detail="Publication is not waiting for confirmation.")
    if request.confirmation != "PUBLISH":
        raise HTTPException(status_code=400, detail="Type PUBLISH to confirm this CMS publication.")
    target = _db_get_cms_target(publication["target_id"])
    if not target or not target["enabled"]:
        raise HTTPException(status_code=409, detail="CMS target is unavailable.")
    headers: dict[str, str] = {}
    if target.get("auth_env_var"):
        secret = os.getenv(target["auth_env_var"])
        if not secret:
            raise HTTPException(status_code=409, detail="CMS credential environment variable is not configured.")
        headers[target["auth_header"]] = secret
    try:
        injection = geo_inject(
            GEOInjectRequest(
                version_id=publication["version_id"],
                target="webhook",
                webhook_url=target["webhook_url"],
                headers=headers,
            )
        )
        publication["status"] = "published"
        publication["injection_id"] = injection["injection_id"]
        publication["response_summary"] = injection.get("response_summary")
        publication["live_status"] = "pending"
        publication["live_summary"] = None
    except HTTPException as exc:
        publication["status"] = "failed"
        publication["response_summary"] = str(exc.detail)
    publication["confirmed_by"] = _current_actor()
    publication["confirmed_at"] = _now_iso()
    publication["updated_at"] = _now_iso()
    _db_save_publication(publication)
    _db_add_audit(
        _current_actor(), "confirm_publish", "publication", publication["publication_id"],
        publication["task_id"], outcome=publication["status"],
    )
    return publication


@app.post("/cms/publications/verify")
def cms_publication_verify(request: CMSPublicationVerifyRequest):
    publication = _db_get_publication(request.publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="Publication not found.")
    if publication["status"] not in {"published", "verification_failed", "verified_live"}:
        raise HTTPException(status_code=409, detail="Publication must be published before live verification.")

    preview = publication.get("preview") or {}
    expected_terms = [item.strip() for item in (request.expected_terms or []) if item.strip()]
    if not expected_terms:
        expected_terms = [item.get("title", "").strip() for item in preview.get("modules") or [] if item.get("title")]
    try:
        url = _normalize_url(preview.get("url") or "")
        title, content = _fetch_page_text(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (HTTPError, URLError, TimeoutError) as exc:
        raise HTTPException(status_code=502, detail=f"Failed to verify live page: {exc}") from exc

    matched = [term for term in expected_terms if term and term.lower() in content.lower()]
    missing = [term for term in expected_terms if term and term not in matched]
    live_status = "verified_live" if expected_terms and not missing else "verification_failed"
    summary = {
        "title": title,
        "matched_terms": matched,
        "missing_terms": missing,
        "notes": request.notes,
    }
    publication["status"] = live_status
    publication["live_status"] = live_status
    publication["live_summary"] = summary
    publication["live_confirmed_by"] = _current_actor()
    publication["live_confirmed_at"] = _now_iso()
    publication["updated_at"] = _now_iso()
    _db_save_publication(publication)
    _db_add_audit(
        _current_actor(),
        "verify_publish",
        "publication",
        publication["publication_id"],
        publication["task_id"],
        outcome=live_status,
        detail=summary,
    )
    return publication


@app.post("/cms/publications/{publication_id}/retry")
def cms_publication_retry(publication_id: str):
    publication = _db_get_publication(publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="Publication not found.")
    if publication["status"] != "failed":
        raise HTTPException(status_code=409, detail="Only failed publications can be retried.")
    publication["status"] = "pending_confirmation"
    publication["response_summary"] = None
    publication["confirmed_by"] = None
    publication["confirmed_at"] = None
    publication["live_status"] = None
    publication["live_summary"] = None
    publication["live_confirmed_by"] = None
    publication["live_confirmed_at"] = None
    publication["updated_at"] = _now_iso()
    _db_save_publication(publication)
    _db_add_audit(_current_actor(), "retry_publish", "publication", publication_id, publication["task_id"])
    return publication


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
        "feedback": [item for item in history["feedback_entries"] if item["task_id"] == task_id],
        "llm_logs": [item for item in history["llm_logs"] if item.get("task_id") == task_id],
        "knowledge_items": task.get("latest_result", {}).get("knowledge_snapshot", []),
        "project": _project_view(
            task,
            [item for item in history["versions"] if item["task_id"] == task_id],
            [item for item in history["injections"] if item["task_id"] == task_id],
            history["retests"].get(task_id, []),
        ),
        "publications": _db_publications(task_id),
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
        _log_llm_call(
            action="llm_generate",
            provider=request.provider,
            model=request.model,
            status="success",
            prompt=request.prompt,
            response=output,
        )
        return {
            "provider": request.provider,
            "model": request.model,
            "output": output,
        }
    except (LLMProviderError, ValueError) as exc:
        _log_llm_call(
            action="llm_generate",
            provider=request.provider,
            model=request.model,
            status="failed",
            prompt=request.prompt,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        _log_llm_call(
            action="llm_generate",
            provider=request.provider,
            model=request.model,
            status="failed",
            prompt=request.prompt,
            error=str(exc),
        )
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
        _log_llm_call(
            action="geo_rewrite",
            provider=request.provider,
            model=request.model,
            status="success",
            prompt=prompt,
            response=output,
        )
        return {
            "provider": request.provider,
            "output": output,
        }
    except Exception as exc:
        _log_llm_call(
            action="geo_rewrite",
            provider=request.provider,
            model=request.model,
            status="failed",
            prompt=prompt,
            error=str(exc),
        )
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
        _log_llm_call(
            action="geo_faq",
            provider=request.provider,
            model=request.model,
            status="success",
            prompt=prompt,
            response=output,
        )
        return {
            "provider": request.provider,
            "output": output,
        }
    except Exception as exc:
        _log_llm_call(
            action="geo_faq",
            provider=request.provider,
            model=request.model,
            status="failed",
            prompt=prompt,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail=f"FAQ generation failed: {exc}") from exc
