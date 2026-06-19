import json
import hashlib
import ipaddress
import os
import re
import secrets
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
        AuthIdentity,
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
        AuthIdentity,
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

BOOTSTRAP_ORG_ID = "org_internal"
BOOTSTRAP_WORKSPACE_ID = "ws_internal"
BOOTSTRAP_CUSTOMER_ID = "cust_internal"
CLIENT_SESSION_ROLES = {"client_viewer", "client_approver"}
SCOPED_TABLES = (
    "tasks",
    "versions",
    "retests",
    "content_experiments",
    "lead_attributions",
    "effect_reports",
    "publications",
    "knowledge_items",
    "feedback_entries",
    "llm_logs",
    "injections",
    "audit_logs",
    "jobs",
    "monitor_queries",
    "source_observations",
    "trust_anchor_tasks",
    "mention_checks",
)


@dataclass(frozen=True)
class RequestScope:
    workspace_id: str | None = None
    customer_id: str | None = None
    membership_id: str | None = None
    via_session: bool = False


_scope_context: ContextVar[RequestScope] = ContextVar(
    "geo_request_scope",
    default=RequestScope(),
)


def _database_url() -> str:
    return os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}").strip()


def _session_cookie_name() -> str:
    return os.getenv("GEO_SESSION_COOKIE_NAME", "geo_session").strip() or "geo_session"


def _session_cookie_secure() -> bool:
    return os.getenv("GEO_SESSION_COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"}


def _session_cookie_samesite() -> str:
    value = os.getenv("GEO_SESSION_COOKIE_SAMESITE", "lax").strip().lower()
    return value if value in {"lax", "strict", "none"} else "lax"


def _session_ttl_hours() -> int:
    raw = os.getenv("GEO_SESSION_TTL_HOURS", "168").strip()
    try:
        return max(1, min(int(raw), 24 * 90))
    except ValueError:
        return 168


def _browser_base_url() -> str:
    return os.getenv("GEO_BROWSER_BASE_URL", "").strip().rstrip("/")


def _workspace_id_header() -> str:
    return "x-geo-workspace-id"


def _customer_id_header() -> str:
    return "x-geo-customer-id"


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:48] or f"item-{uuid.uuid4().hex[:8]}"


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _set_current_scope(scope: RequestScope):
    return _scope_context.set(scope)


def _reset_current_scope(token) -> None:
    _scope_context.reset(token)


def _current_scope() -> RequestScope:
    return _scope_context.get()


def _client_role() -> str | None:
    role = current_identity().role
    return role if role in CLIENT_SESSION_ROLES else None


def _is_client_session() -> bool:
    scope = _current_scope()
    return scope.via_session and current_identity().role in CLIENT_SESSION_ROLES


def _session_can_access_viewer_route(path: str, method: str, role: str) -> bool:
    if role == "client_approver" and path == "/geo/version/review" and method.upper() == "POST":
        return True
    if role not in CLIENT_SESSION_ROLES or method.upper() != "GET":
        return False
    if path in {"/workspaces", "/customers", "/geo/projects", "/auth/session/me"}:
        return True
    return (
        path.startswith("/geo/projects/")
        or path.startswith("/geo/tasks/")
        or path.startswith("/geo/reports")
    )


def _require_internal_panel_access() -> None:
    if _is_client_session():
        raise HTTPException(status_code=403, detail="Client sessions cannot access internal operator panels.")


def _cors_allowed_origins() -> list[str]:
    raw = os.getenv("GEO_CORS_ALLOWED_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    value = host.strip().strip("[]")
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _request_allows_unsafe_local_dev(request: FastAPIRequest) -> bool:
    client_host = request.client.host if request.client else None
    client_is_local = (
        client_host is None
        or _is_loopback_host(client_host)
        or client_host == "testclient"
    )
    return _is_loopback_host(request.url.hostname) and client_is_local


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins(),
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def api_key_rbac(request: FastAPIRequest, call_next):
    role = required_role(request.method, request.url.path)
    api_key = extract_api_key(
        request.headers.get("authorization"),
        request.headers.get("x-geo-api-key"),
    )
    session_token = request.cookies.get(_session_cookie_name())
    identity = None
    scope = RequestScope()
    try:
        if api_key:
            identity = resolve_identity(
                api_key,
                allow_unsafe_local_dev=_request_allows_unsafe_local_dev(request),
            )
        elif session_token:
            browser_session = _db_get_browser_session(session_token)
            if browser_session:
                display_name = browser_session.get("display_name") or browser_session["email"]
                identity = AuthIdentity(name=display_name, role=browser_session["role"])
                scope = RequestScope(
                    workspace_id=browser_session["workspace_id"],
                    customer_id=browser_session.get("customer_id"),
                    membership_id=browser_session["membership_id"],
                    via_session=True,
                )
        if identity is None:
            identity = resolve_identity(
                None,
                allow_unsafe_local_dev=_request_allows_unsafe_local_dev(request),
            )
        if not scope.via_session:
            scope = _scope_from_headers(request.headers)
    except ValueError as exc:
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except (OSError, sqlite3.Error) as exc:
        return JSONResponse(status_code=500, content={"detail": f"Authentication lookup failed: {exc}"})

    if role is not None and identity is None:
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    if role is not None and identity is not None and not (
        has_role(identity, role)
        or (scope.via_session and _session_can_access_viewer_route(request.url.path, request.method, identity.role))
    ):
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

    if identity is None:
        return await call_next(request)

    context_token = set_current_identity(identity)
    scope_token = _set_current_scope(scope)
    try:
        response = await call_next(request)
        response.headers["X-GEO-Actor"] = identity.name
        response.headers["X-GEO-Role"] = identity.role
        if scope.workspace_id:
            response.headers["X-GEO-Workspace"] = scope.workspace_id
        if scope.customer_id:
            response.headers["X-GEO-Customer"] = scope.customer_id
        return response
    finally:
        _reset_current_scope(scope_token)
        reset_current_identity(context_token)

def _default_db_path() -> Path:
    raw = os.getenv("DATABASE_URL", "").strip()
    if raw.startswith("sqlite:///"):
        return Path(raw.removeprefix("sqlite:///"))
    return Path(__file__).with_name("geo_growth.db")


DB_PATH = _default_db_path()
EXPORT_DIR = Path(__file__).resolve().parent.parent / "exports"
ADMIN_INDEX = Path(__file__).resolve().parent.parent / "admin" / "index.html"


class GEOAuditRequest(BaseModel):
    content: str


class GEOUrlAuditRequest(BaseModel):
    url: str


class GEOAnalyzeRequest(BaseModel):
    url: str
    workspace_id: str | None = None
    customer_id: str | None = None
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
    client_name: str | None = None
    brand_name: str | None = None
    target_engines: list[str] = ["chatgpt", "perplexity"]
    business_goal: str = "提升 AI 推荐可见度与询盘"


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
    client_name: str | None = None
    brand_name: str | None = None
    target_engines: list[str] | None = None
    business_goal: str | None = None
    service_tier: str | None = None
    package_id: str | None = None


class WorkspaceCreateRequest(BaseModel):
    name: str
    organization_name: str = "GEO Internal"
    market: str | None = None
    language: str | None = None
    status: str = "active"


class CustomerCreateRequest(BaseModel):
    workspace_id: str
    name: str
    market: str | None = None
    language: str | None = None
    status: str = "active"


class AuthInviteRequest(BaseModel):
    workspace_id: str
    customer_id: str | None = None
    email: str
    role: str = "client_viewer"
    display_name: str | None = None
    note: str | None = None
    expires_in_days: int = 7


class AuthInviteAcceptRequest(BaseModel):
    token: str
    display_name: str | None = None


class CustomerMemberCreateRequest(BaseModel):
    email: str
    role: str = "client_viewer"
    display_name: str | None = None
    status: str = "active"


class CustomerReportRequest(BaseModel):
    task_id: str
    period_label: str = "近 7 天"
    notes: str | None = None


class CMSPublishTargetRequest(BaseModel):
    name: str
    webhook_url: str
    environment: str = "staging"
    auth_header: str = "Authorization"
    auth_env_var: str | None = None
    enabled: bool = True


class CMSPublishTargetStatusRequest(BaseModel):
    enabled: bool


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


class CMSPublicationVerifyScheduleRequest(CMSPublicationVerifyRequest):
    run_at: str | None = None
    max_attempts: int = 3


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


class GEOMonitorQueryRequest(BaseModel):
    task_id: str
    query_text: str
    category: str = "comparison"
    competitor: str | None = None
    engine: str = "perplexity"
    active: bool = True
    query_type: str | None = None
    intent_stage: str | None = None
    priority: str | None = None
    reason: str | None = None
    sample_target: int = 3
    language: str | None = None
    status: str = "active"


class GEOQueryGenerateRequest(BaseModel):
    task_id: str
    query_count: int = 12
    languages: list[str] | None = None
    include_competitors: bool = True


class GEOSourceObservationRequest(BaseModel):
    task_id: str
    query_id: str
    source_domain: str
    source_url: str | None = None
    page_type: str = "unknown"
    citation_count: int = 1
    notes: str | None = None


class GEOSourceParseRequest(BaseModel):
    task_id: str
    query_id: str
    platform: str = "perplexity"
    answer_text: str
    sources_text: str = ""
    brand_terms: list[str] | None = None
    competitors: list[str] | None = None


class GEOTrustAnchorRequest(BaseModel):
    task_id: str
    channel: str
    topic: str
    target_url: str | None = None
    owner: str | None = None
    status: str = "planned"
    guidance: str | None = None
    evidence_url: str | None = None


class GEOMentionCheckRequest(BaseModel):
    task_id: str
    query_id: str
    engine: str = "perplexity"
    brand_mentioned: bool = False
    mention_position: int | None = None
    source_type: str | None = None
    source_url: str | None = None
    answer_excerpt: str | None = None
    notes: str | None = None
    cited_our_domain: bool = False
    competitor_mentions: list[str] | None = None
    confidence_weight: float = 1.0


class GEOServicePackageRequest(BaseModel):
    name: str
    tier: str = "growth"
    price_cny: int = 0
    delivery_days: int = 14
    platforms: list[str] | None = None
    features: list[str] | None = None
    status: str = "active"


class GEOExperimentRequest(BaseModel):
    task_id: str
    name: str
    hypothesis: str
    channel: str = "onsite"
    primary_metric: str = "mention_rate"
    variant_a: str
    variant_b: str
    status: str = "draft"
    notes: str | None = None


class GEOExperimentConfirmRequest(BaseModel):
    status: str
    winner: str | None = None
    notes: str | None = None


class GEOAttributionRequest(BaseModel):
    task_id: str
    source_type: str
    source_name: str
    session_ref: str | None = None
    lead_stage: str = "new"
    attributed_revenue: float = 0
    evidence_url: str | None = None
    status: str = "pending_confirmation"
    notes: str | None = None


class GEOReportGenerateRequest(BaseModel):
    task_id: str
    period_label: str = "近 30 天"
    notes: str | None = None


class GEOReportConfirmRequest(BaseModel):
    status: str = "confirmed"
    notes: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_actor() -> str:
    return current_identity().name


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _scope_from_headers(headers) -> RequestScope:
    workspace_id = (headers.get(_workspace_id_header()) or "").strip() or None
    customer_id = (headers.get(_customer_id_header()) or "").strip() or None
    if customer_id and not workspace_id:
        raise HTTPException(status_code=400, detail="Customer scope requires a workspace scope.")
    return RequestScope(workspace_id=workspace_id, customer_id=customer_id)


def _append_scope_filters(filters: list[str], params: list, *, alias: str | None = None) -> None:
    scope = _current_scope()
    prefix = f"{alias}." if alias else ""
    if scope.workspace_id:
        filters.append(f"{prefix}workspace_id = ?")
        params.append(scope.workspace_id)
    if scope.customer_id:
        filters.append(f"{prefix}customer_id = ?")
        params.append(scope.customer_id)


def _bootstrap_scope() -> tuple[str, str]:
    return BOOTSTRAP_WORKSPACE_ID, BOOTSTRAP_CUSTOMER_ID


def _validate_scope_or_404(
    workspace_id: str | None,
    customer_id: str | None = None,
) -> tuple[dict | None, dict | None]:
    if not workspace_id:
        return None, None
    workspace = _db_get_workspace(workspace_id, ignore_scope=True)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    customer = None
    if customer_id:
        customer = _db_get_customer(customer_id, ignore_scope=True)
        if not customer or customer["workspace_id"] != workspace_id:
            raise HTTPException(status_code=404, detail="Customer not found in workspace.")
    return workspace, customer


def _resolve_scope_ids(
    workspace_id: str | None = None,
    customer_id: str | None = None,
    *,
    task_id: str | None = None,
    require_customer: bool = False,
    allow_bootstrap: bool = True,
) -> tuple[str | None, str | None]:
    if task_id:
        task = _db_get_task(task_id, ignore_scope=True)
        if task:
            workspace_id = workspace_id or task.get("workspace_id")
            customer_id = customer_id or task.get("customer_id")

    scope = _current_scope()
    if scope.workspace_id:
        if workspace_id and workspace_id != scope.workspace_id:
            raise HTTPException(status_code=403, detail="Cross-workspace access is not allowed.")
        workspace_id = workspace_id or scope.workspace_id
    if scope.customer_id:
        if customer_id and customer_id != scope.customer_id:
            raise HTTPException(status_code=403, detail="Cross-customer access is not allowed.")
        customer_id = customer_id or scope.customer_id

    if not workspace_id and allow_bootstrap:
        workspace_id, fallback_customer_id = _bootstrap_scope()
        customer_id = customer_id or fallback_customer_id

    if customer_id and not workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required when customer_id is provided.")
    if require_customer and not customer_id:
        raise HTTPException(status_code=400, detail="customer_id is required for this operation.")
    if workspace_id:
        _validate_scope_or_404(workspace_id, customer_id)
    return workspace_id, customer_id


def _record_scope_from_task(task_id: str | None) -> tuple[str | None, str | None]:
    if not task_id:
        return _resolve_scope_ids()
    return _resolve_scope_ids(task_id=task_id)


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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
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
                client_name TEXT,
                brand_name TEXT,
                target_engines TEXT,
                business_goal TEXT,
                service_tier TEXT,
                package_id TEXT,
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
            CREATE TABLE IF NOT EXISTS service_packages (
                package_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                tier TEXT NOT NULL,
                price_cny INTEGER NOT NULL,
                delivery_days INTEGER NOT NULL,
                platforms TEXT NOT NULL,
                features TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content_experiments (
                experiment_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                name TEXT NOT NULL,
                hypothesis TEXT NOT NULL,
                channel TEXT NOT NULL,
                primary_metric TEXT NOT NULL,
                variant_a TEXT NOT NULL,
                variant_b TEXT NOT NULL,
                status TEXT NOT NULL,
                winner TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                confirmed_by TEXT,
                confirmed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_attributions (
                attribution_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_name TEXT NOT NULL,
                session_ref TEXT,
                lead_stage TEXT NOT NULL,
                attributed_revenue REAL NOT NULL,
                evidence_url TEXT,
                status TEXT NOT NULL,
                notes TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS effect_reports (
                report_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                period_label TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                metrics TEXT NOT NULL,
                findings TEXT NOT NULL,
                next_actions TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                confirmed_by TEXT,
                confirmed_at TEXT,
                notes TEXT
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitor_queries (
                query_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                query_text TEXT NOT NULL,
                category TEXT NOT NULL,
                competitor TEXT,
                engine TEXT NOT NULL,
                active INTEGER NOT NULL,
                query_type TEXT,
                intent_stage TEXT,
                priority TEXT,
                reason TEXT,
                sample_target INTEGER,
                language TEXT,
                status TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_observations (
                observation_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                query_id TEXT NOT NULL,
                source_domain TEXT NOT NULL,
                source_url TEXT,
                page_type TEXT NOT NULL,
                citation_count INTEGER NOT NULL,
                notes TEXT,
                observed_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trust_anchor_tasks (
                anchor_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                topic TEXT NOT NULL,
                target_url TEXT,
                owner TEXT,
                status TEXT NOT NULL,
                guidance TEXT,
                evidence_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mention_checks (
                check_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                query_id TEXT NOT NULL,
                engine TEXT NOT NULL,
                brand_mentioned INTEGER NOT NULL,
                mention_position INTEGER,
                source_type TEXT,
                source_url TEXT,
                answer_excerpt TEXT,
                notes TEXT,
                cited_our_domain INTEGER,
                competitor_mentions TEXT,
                confidence_weight REAL,
                checked_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS organizations (
                organization_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspaces (
                workspace_id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                name TEXT NOT NULL,
                slug TEXT NOT NULL,
                market TEXT,
                language TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                customer_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                name TEXT NOT NULL,
                slug TEXT NOT NULL,
                market TEXT,
                language TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_memberships (
                membership_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                customer_id TEXT,
                email TEXT NOT NULL,
                display_name TEXT,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                invited_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS viewer_invites (
                invite_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL,
                customer_id TEXT,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                display_name TEXT,
                token_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT,
                invited_by TEXT,
                expires_at TEXT NOT NULL,
                accepted_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS browser_sessions (
                session_id TEXT PRIMARY KEY,
                membership_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                customer_id TEXT,
                session_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                user_agent TEXT,
                ip_address TEXT,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
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
            "client_name": "TEXT",
            "brand_name": "TEXT",
            "target_engines": "TEXT",
            "business_goal": "TEXT",
            "service_tier": "TEXT",
            "package_id": "TEXT",
        }.items():
            if column not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        monitor_columns = {row["name"] for row in conn.execute("PRAGMA table_info(monitor_queries)").fetchall()}
        for column, definition in {
            "query_type": "TEXT",
            "intent_stage": "TEXT",
            "priority": "TEXT",
            "reason": "TEXT",
            "sample_target": "INTEGER",
            "language": "TEXT",
            "status": "TEXT",
        }.items():
            if column not in monitor_columns:
                conn.execute(f"ALTER TABLE monitor_queries ADD COLUMN {column} {definition}")
        mention_columns = {row["name"] for row in conn.execute("PRAGMA table_info(mention_checks)").fetchall()}
        for column, definition in {
            "cited_our_domain": "INTEGER",
            "competitor_mentions": "TEXT",
            "confidence_weight": "REAL",
        }.items():
            if column not in mention_columns:
                conn.execute(f"ALTER TABLE mention_checks ADD COLUMN {column} {definition}")
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
        for table_name in SCOPED_TABLES:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
            if "workspace_id" not in columns:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN workspace_id TEXT")
            if "customer_id" not in columns:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN customer_id TEXT")

        now = _now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO organizations (
                organization_id, name, slug, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP_ORG_ID,
                os.getenv("GEO_BOOTSTRAP_ORG_NAME", "GEO Internal"),
                _slugify(os.getenv("GEO_BOOTSTRAP_ORG_NAME", "GEO Internal")),
                "active",
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO workspaces (
                workspace_id, organization_id, name, slug, market, language, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP_WORKSPACE_ID,
                BOOTSTRAP_ORG_ID,
                os.getenv("GEO_BOOTSTRAP_WORKSPACE_NAME", "Internal Ops"),
                _slugify(os.getenv("GEO_BOOTSTRAP_WORKSPACE_NAME", "Internal Ops")),
                os.getenv("GEO_BOOTSTRAP_MARKET", "Hong Kong/Japan"),
                os.getenv("GEO_BOOTSTRAP_LANGUAGE", "zh-HK"),
                "active",
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO customers (
                customer_id, workspace_id, name, slug, market, language, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                BOOTSTRAP_CUSTOMER_ID,
                BOOTSTRAP_WORKSPACE_ID,
                os.getenv("GEO_BOOTSTRAP_CUSTOMER_NAME", "Internal Pilot"),
                _slugify(os.getenv("GEO_BOOTSTRAP_CUSTOMER_NAME", "Internal Pilot")),
                os.getenv("GEO_BOOTSTRAP_MARKET", "Hong Kong/Japan"),
                os.getenv("GEO_BOOTSTRAP_LANGUAGE", "zh-HK"),
                "active",
                now,
                now,
            ),
        )
        for table_name in SCOPED_TABLES:
            conn.execute(
                f"UPDATE {table_name} SET workspace_id = ? WHERE workspace_id IS NULL OR workspace_id = ''",
                (BOOTSTRAP_WORKSPACE_ID,),
            )
            conn.execute(
                f"UPDATE {table_name} SET customer_id = ? WHERE customer_id IS NULL OR customer_id = ''",
                (BOOTSTRAP_CUSTOMER_ID,),
            )


def _organization_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _workspace_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _customer_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _membership_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _invite_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _db_workspaces(ignore_scope: bool = False) -> list[dict]:
    query = "SELECT * FROM workspaces"
    filters: list[str] = []
    params: list[str] = []
    if not ignore_scope:
        scope = _current_scope()
        if scope.workspace_id:
            filters.append("workspace_id = ?")
            params.append(scope.workspace_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC, name ASC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_workspace_from_row(row) for row in rows]


def _db_get_workspace(workspace_id: str, *, ignore_scope: bool = False) -> dict | None:
    query = "SELECT * FROM workspaces WHERE workspace_id = ?"
    params: list[str] = [workspace_id]
    if not ignore_scope:
        scope = _current_scope()
        if scope.workspace_id:
            query += " AND workspace_id = ?"
            params.append(scope.workspace_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _workspace_from_row(row) if row else None


def _db_save_workspace(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO workspaces (
                workspace_id, organization_id, name, slug, market, language, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                name=excluded.name,
                slug=excluded.slug,
                market=excluded.market,
                language=excluded.language,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                item["workspace_id"],
                item["organization_id"],
                item["name"],
                item["slug"],
                item.get("market"),
                item.get("language"),
                item["status"],
                item["created_at"],
                item["updated_at"],
            ),
        )


def _db_customers(
    workspace_id: str | None = None,
    *,
    ignore_scope: bool = False,
) -> list[dict]:
    query = "SELECT * FROM customers"
    filters: list[str] = []
    params: list[str] = []
    effective_workspace_id = workspace_id
    effective_customer_id = None
    if not ignore_scope:
        scope = _current_scope()
        effective_workspace_id = effective_workspace_id or scope.workspace_id
        effective_customer_id = scope.customer_id
    if effective_workspace_id:
        filters.append("workspace_id = ?")
        params.append(effective_workspace_id)
    if effective_customer_id:
        filters.append("customer_id = ?")
        params.append(effective_customer_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC, name ASC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_customer_from_row(row) for row in rows]


def _db_get_customer(customer_id: str, *, ignore_scope: bool = False) -> dict | None:
    query = "SELECT * FROM customers WHERE customer_id = ?"
    params: list[str] = [customer_id]
    if not ignore_scope:
        scope = _current_scope()
        if scope.workspace_id:
            query += " AND workspace_id = ?"
            params.append(scope.workspace_id)
        if scope.customer_id:
            query += " AND customer_id = ?"
            params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _customer_from_row(row) if row else None


def _db_save_customer(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO customers (
                customer_id, workspace_id, name, slug, market, language, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_id) DO UPDATE SET
                name=excluded.name,
                slug=excluded.slug,
                market=excluded.market,
                language=excluded.language,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                item["customer_id"],
                item["workspace_id"],
                item["name"],
                item["slug"],
                item.get("market"),
                item.get("language"),
                item["status"],
                item["created_at"],
                item["updated_at"],
            ),
        )


def _db_memberships(
    workspace_id: str | None = None,
    customer_id: str | None = None,
    *,
    ignore_scope: bool = False,
) -> list[dict]:
    query = "SELECT * FROM workspace_memberships"
    filters: list[str] = []
    params: list[str] = []
    effective_workspace_id = workspace_id
    effective_customer_id = customer_id
    if not ignore_scope:
        scope = _current_scope()
        effective_workspace_id = effective_workspace_id or scope.workspace_id
        effective_customer_id = effective_customer_id or scope.customer_id
    if effective_workspace_id:
        filters.append("workspace_id = ?")
        params.append(effective_workspace_id)
    if effective_customer_id:
        filters.append("customer_id = ?")
        params.append(effective_customer_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_membership_from_row(row) for row in rows]


def _db_get_membership_by_email(
    workspace_id: str,
    customer_id: str | None,
    email: str,
    *,
    ignore_scope: bool = False,
) -> dict | None:
    query = """
        SELECT * FROM workspace_memberships
        WHERE workspace_id = ? AND email = ?
    """
    params: list[str | None] = [workspace_id, email.strip().lower()]
    if customer_id:
        query += " AND customer_id = ?"
        params.append(customer_id)
    else:
        query += " AND customer_id IS NULL"
    if not ignore_scope:
        scope = _current_scope()
        if scope.workspace_id:
            query += " AND workspace_id = ?"
            params.append(scope.workspace_id)
        if scope.customer_id:
            query += " AND customer_id = ?"
            params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _membership_from_row(row) if row else None


def _db_save_membership(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO workspace_memberships (
                membership_id, workspace_id, customer_id, email, display_name, role, status,
                invited_by, created_at, updated_at, last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(membership_id) DO UPDATE SET
                display_name=excluded.display_name,
                role=excluded.role,
                status=excluded.status,
                invited_by=excluded.invited_by,
                updated_at=excluded.updated_at,
                last_login_at=excluded.last_login_at
            """,
            (
                item["membership_id"],
                item["workspace_id"],
                item.get("customer_id"),
                item["email"].strip().lower(),
                item.get("display_name"),
                item["role"],
                item["status"],
                item.get("invited_by"),
                item["created_at"],
                item["updated_at"],
                item.get("last_login_at"),
            ),
        )


def _db_save_invite(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO viewer_invites (
                invite_id, workspace_id, customer_id, email, role, display_name, token_hash,
                status, note, invited_by, expires_at, accepted_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(invite_id) DO UPDATE SET
                role=excluded.role,
                display_name=excluded.display_name,
                token_hash=excluded.token_hash,
                status=excluded.status,
                note=excluded.note,
                invited_by=excluded.invited_by,
                expires_at=excluded.expires_at,
                accepted_at=excluded.accepted_at
            """,
            (
                item["invite_id"],
                item["workspace_id"],
                item.get("customer_id"),
                item["email"].strip().lower(),
                item["role"],
                item.get("display_name"),
                item["token_hash"],
                item["status"],
                item.get("note"),
                item.get("invited_by"),
                item["expires_at"],
                item.get("accepted_at"),
                item["created_at"],
            ),
        )


def _db_get_invite_by_token(token: str) -> dict | None:
    token_hash = _hash_secret(token)
    with _db() as conn:
        row = conn.execute(
            """
            SELECT * FROM viewer_invites
            WHERE token_hash = ? AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (token_hash,),
        ).fetchone()
    return _invite_from_row(row) if row else None


def _db_mark_invite_accepted(invite_id: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE viewer_invites SET status = 'accepted', accepted_at = ? WHERE invite_id = ?",
            (_now_iso(), invite_id),
        )


def _db_save_browser_session(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO browser_sessions (
                session_id, membership_id, workspace_id, customer_id, session_hash, status,
                expires_at, user_agent, ip_address, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                status=excluded.status,
                expires_at=excluded.expires_at,
                user_agent=excluded.user_agent,
                ip_address=excluded.ip_address,
                last_seen_at=excluded.last_seen_at
            """,
            (
                item["session_id"],
                item["membership_id"],
                item["workspace_id"],
                item.get("customer_id"),
                item["session_hash"],
                item["status"],
                item["expires_at"],
                item.get("user_agent"),
                item.get("ip_address"),
                item["created_at"],
                item["last_seen_at"],
            ),
        )


def _db_get_browser_session(token: str) -> dict | None:
    session_hash = _hash_secret(token)
    with _db() as conn:
        row = conn.execute(
            """
            SELECT
                sessions.*,
                memberships.email,
                memberships.display_name,
                memberships.role,
                memberships.status AS membership_status
            FROM browser_sessions AS sessions
            JOIN workspace_memberships AS memberships
              ON memberships.membership_id = sessions.membership_id
            WHERE sessions.session_hash = ?
              AND sessions.status = 'active'
              AND memberships.status = 'active'
            LIMIT 1
            """,
            (session_hash,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] <= _now_iso():
            conn.execute(
                "UPDATE browser_sessions SET status = 'expired', last_seen_at = ? WHERE session_id = ?",
                (_now_iso(), row["session_id"]),
            )
            return None
        conn.execute(
            "UPDATE browser_sessions SET last_seen_at = ? WHERE session_id = ?",
            (_now_iso(), row["session_id"]),
        )
    return dict(row)


def _db_revoke_browser_session(token: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE browser_sessions SET status = 'revoked', last_seen_at = ? WHERE session_hash = ?",
            (_now_iso(), _hash_secret(token)),
        )


def _invite_accept_url(token: str) -> str:
    suffix = f"/admin?invite_token={token}"
    base = _browser_base_url()
    return f"{base}{suffix}" if base else suffix


def _validate_membership_role(role: str, *, customer_id: str | None = None) -> str:
    normalized = role.strip().lower()
    allowed = {
        "workspace_admin",
        "operator",
        "reviewer",
        "client_viewer",
        "client_approver",
    }
    if normalized not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported membership role.")
    if normalized in CLIENT_SESSION_ROLES and not customer_id:
        raise HTTPException(status_code=400, detail="Client viewer roles require a customer scope.")
    return normalized


def _upsert_membership(
    *,
    workspace_id: str,
    customer_id: str | None,
    email: str,
    role: str,
    display_name: str | None,
    invited_by: str | None = None,
    status: str = "active",
    last_login_at: str | None = None,
) -> dict:
    existing = _db_get_membership_by_email(
        workspace_id,
        customer_id,
        email,
        ignore_scope=True,
    )
    now = _now_iso()
    membership = {
        "membership_id": existing["membership_id"] if existing else f"member_{uuid.uuid4().hex[:12]}",
        "workspace_id": workspace_id,
        "customer_id": customer_id,
        "email": email.strip().lower(),
        "display_name": (display_name or "").strip() or (existing or {}).get("display_name"),
        "role": _validate_membership_role(role, customer_id=customer_id),
        "status": status,
        "invited_by": invited_by or (existing or {}).get("invited_by"),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
        "last_login_at": last_login_at or (existing or {}).get("last_login_at"),
    }
    _db_save_membership(membership)
    return _db_get_membership_by_email(
        workspace_id,
        customer_id,
        email,
        ignore_scope=True,
    ) or membership


def _create_browser_session(
    membership: dict,
    request: FastAPIRequest,
) -> tuple[str, dict]:
    raw_token = secrets.token_urlsafe(32)
    now = _now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=_session_ttl_hours())).isoformat()
    session = {
        "session_id": f"session_{uuid.uuid4().hex[:12]}",
        "membership_id": membership["membership_id"],
        "workspace_id": membership["workspace_id"],
        "customer_id": membership.get("customer_id"),
        "session_hash": _hash_secret(raw_token),
        "status": "active",
        "expires_at": expires_at,
        "user_agent": request.headers.get("user-agent"),
        "ip_address": request.client.host if request.client else None,
        "created_at": now,
        "last_seen_at": now,
    }
    _db_save_browser_session(session)
    return raw_token, session


def _session_response(payload: dict, token: str | None = None) -> JSONResponse:
    response = JSONResponse(payload)
    if token:
        response.set_cookie(
            _session_cookie_name(),
            token,
            httponly=True,
            secure=_session_cookie_secure(),
            samesite=_session_cookie_samesite(),
            max_age=_session_ttl_hours() * 3600,
            path="/",
        )
    return response


def _clear_session_cookie(response: JSONResponse) -> JSONResponse:
    response.delete_cookie(_session_cookie_name(), path="/")
    return response


def _db_upsert_task(task: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        task.get("workspace_id"),
        task.get("customer_id"),
        allow_bootstrap=True,
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, url, title, status, latest_result, latest_workflow,
                latest_version_id, latest_retest, owner, target_score, todos,
                client_name, brand_name, target_engines, business_goal, service_tier,
                package_id, workspace_id, customer_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                client_name=excluded.client_name,
                brand_name=excluded.brand_name,
                target_engines=excluded.target_engines,
                business_goal=excluded.business_goal,
                service_tier=excluded.service_tier,
                package_id=excluded.package_id,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id,
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
                task.get("client_name"),
                task.get("brand_name"),
                _json_dumps(task.get("target_engines", [])),
                task.get("business_goal"),
                task.get("service_tier"),
                task.get("package_id"),
                workspace_id,
                customer_id,
                task.get("created_at") or _now_iso(),
                task.get("updated_at") or _now_iso(),
            ),
        )


def _db_get_task(task_id: str, *, ignore_scope: bool = False) -> dict | None:
    query = "SELECT * FROM tasks WHERE task_id = ?"
    params: list[str] = [task_id]
    if not ignore_scope:
        scope = _current_scope()
        if scope.workspace_id:
            query += " AND workspace_id = ?"
            params.append(scope.workspace_id)
        if scope.customer_id:
            query += " AND customer_id = ?"
            params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _task_from_row(row) if row else None


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
        "client_name": row["client_name"],
        "brand_name": row["brand_name"],
        "target_engines": _json_loads(row["target_engines"], []),
        "business_goal": row["business_goal"],
        "service_tier": row["service_tier"],
        "package_id": row["package_id"],
        "workspace_id": row["workspace_id"],
        "customer_id": row["customer_id"],
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
        "workspace_id": row["workspace_id"],
        "customer_id": row["customer_id"],
    }


def _db_save_version(version: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        version.get("workspace_id"),
        version.get("customer_id"),
        task_id=version.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO versions (
                version_id, task_id, url, status, editor, reviewer, review_comment,
                modules, workflow, injection_payload, created_at, updated_at, approved_at, quality_report,
                workspace_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version_id) DO UPDATE SET
                status=excluded.status,
                reviewer=excluded.reviewer,
                review_comment=excluded.review_comment,
                modules=excluded.modules,
                workflow=excluded.workflow,
                injection_payload=excluded.injection_payload,
                updated_at=excluded.updated_at,
                approved_at=excluded.approved_at,
                quality_report=excluded.quality_report,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id
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
                workspace_id,
                customer_id,
            ),
        )


def _db_get_version(version_id: str, *, ignore_scope: bool = False) -> dict | None:
    query = "SELECT * FROM versions WHERE version_id = ?"
    params: list[str] = [version_id]
    if not ignore_scope:
        scope = _current_scope()
        if scope.workspace_id:
            query += " AND workspace_id = ?"
            params.append(scope.workspace_id)
        if scope.customer_id:
            query += " AND customer_id = ?"
            params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _version_from_row(row) if row else None


def _db_count_versions(task_id: str) -> int:
    query = "SELECT COUNT(*) AS count FROM versions WHERE task_id = ?"
    params: list[str] = [task_id]
    scope = _current_scope()
    if scope.workspace_id:
        query += " AND workspace_id = ?"
        params.append(scope.workspace_id)
    if scope.customer_id:
        query += " AND customer_id = ?"
        params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return int(row["count"]) if row else 0


def _db_add_retest(retest: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        retest.get("workspace_id"),
        retest.get("customer_id"),
        task_id=retest.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO retests (
                task_id, version_id, injection_id, url, title, previous_score, current_score, score_delta,
                status, breakdown, recommendations, effect_details, created_at, workspace_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                workspace_id,
                customer_id,
            ),
        )


def _db_save_injection(injection: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        injection.get("workspace_id"),
        injection.get("customer_id"),
        task_id=injection.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO injections (
                injection_id, task_id, version_id, url, target, status,
                response_summary, artifact_path, created_at, completed_at, workspace_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(injection_id) DO UPDATE SET
                status=excluded.status,
                response_summary=excluded.response_summary,
                artifact_path=excluded.artifact_path,
                completed_at=excluded.completed_at,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id
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
                workspace_id,
                customer_id,
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
        "workspace_id": row["workspace_id"],
        "customer_id": row["customer_id"],
    }


def _db_get_injection(injection_id: str) -> dict | None:
    query = "SELECT * FROM injections WHERE injection_id = ?"
    params: list[str] = [injection_id]
    scope = _current_scope()
    if scope.workspace_id:
        query += " AND workspace_id = ?"
        params.append(scope.workspace_id)
    if scope.customer_id:
        query += " AND customer_id = ?"
        params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _injection_from_row(row) if row else None


def _db_latest_successful_injection(task_id: str, version_id: str | None = None) -> dict | None:
    query = "SELECT * FROM injections WHERE task_id = ? AND status = 'completed'"
    params: list[str] = [task_id]
    if version_id:
        query += " AND version_id = ?"
        params.append(version_id)
    scope = _current_scope()
    if scope.workspace_id:
        query += " AND workspace_id = ?"
        params.append(scope.workspace_id)
    if scope.customer_id:
        query += " AND customer_id = ?"
        params.append(scope.customer_id)
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
    workspace_id: str | None = None,
    customer_id: str | None = None,
) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        workspace_id,
        customer_id,
        task_id=task_id,
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs (
                actor, action, entity_type, entity_id, task_id, outcome, detail, created_at,
                workspace_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                workspace_id,
                customer_id,
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
    _append_scope_filters(filters, params)
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
            "workspace_id": row["workspace_id"],
            "customer_id": row["customer_id"],
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
        "workspace_id": row["workspace_id"],
        "customer_id": row["customer_id"],
    }


def _db_save_job(job: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        job.get("workspace_id"),
        job.get("customer_id"),
        task_id=(job.get("payload") or {}).get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, job_type, status, payload, result, attempts, max_attempts,
                run_at, last_error, created_at, updated_at, completed_at, workspace_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                result=excluded.result,
                attempts=excluded.attempts,
                max_attempts=excluded.max_attempts,
                run_at=excluded.run_at,
                last_error=excluded.last_error,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id
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
                workspace_id,
                customer_id,
            ),
        )


def _db_get_job(job_id: str) -> dict | None:
    query = "SELECT * FROM jobs WHERE job_id = ?"
    params: list[str] = [job_id]
    scope = _current_scope()
    if scope.workspace_id:
        query += " AND workspace_id = ?"
        params.append(scope.workspace_id)
    if scope.customer_id:
        query += " AND customer_id = ?"
        params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
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
    _append_scope_filters(filters, params)
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
    knowledge_ids = {
        item.get("knowledge_id")
        for item in (workflow or {}).get("knowledge_snapshot") or []
        if item.get("knowledge_id")
    }
    cited_knowledge_ids: set[str] = set()
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
        module_citations = [
            item for item in module.get("knowledge_citations") or [] if item in knowledge_ids
        ]
        cited_knowledge_ids.update(module_citations)
        if knowledge_ids and not module_citations:
            issues.append({"severity": "warning", "code": "missing_knowledge_citation", "module": index})
        fact_checks.append(
            {
                "module": index,
                "title_present": bool(str(module.get("title") or "").strip()),
                "body_length": len(body.strip()),
                "status": "review" if warning_claim.search(body) else "pass",
                "knowledge_citations": module_citations,
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
        "citation_coverage": {
            "available": len(knowledge_ids),
            "cited": len(cited_knowledge_ids),
            "percent": round(len(cited_knowledge_ids) / len(knowledge_ids) * 100) if knowledge_ids else 100,
            "uncited_knowledge_ids": sorted(knowledge_ids - cited_knowledge_ids),
        },
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


def _attach_knowledge_citations(modules: list[dict], knowledge_items: list[dict]) -> list[dict]:
    citations = [item["knowledge_id"] for item in knowledge_items if item.get("knowledge_id")]
    if not citations:
        return modules
    return [{**module, "knowledge_citations": citations} for module in modules]


def _project_view(
    task: dict,
    versions: list[dict] | None = None,
    injections: list[dict] | None = None,
    retests: list[dict] | None = None,
    publications: list[dict] | None = None,
    jobs: list[dict] | None = None,
) -> dict:
    versions = versions or []
    injections = injections or []
    retests = retests or []
    publications = publications or []
    jobs = jobs or []
    latest_version = versions[0] if versions else None
    latest_injection = injections[0] if injections else None
    latest_retest = retests[0] if retests else task.get("latest_retest")
    latest_publication = publications[0] if publications else None
    pending_publication = next((item for item in publications if item.get("status") == "pending_confirmation"), None)
    failed_publication = next((item for item in publications if item.get("status") == "failed"), None)
    verifiable_publication = next(
        (item for item in publications if item.get("status") in {"published", "verification_failed"}),
        None,
    )
    pending_retest_job = next(
        (
            item
            for item in jobs
            if item.get("job_type") == "retest"
            and item.get("payload", {}).get("task_id") == task["task_id"]
            and item.get("status") in {"queued", "retry_wait", "running"}
        ),
        None,
    )
    pending_verify_job = next(
        (
            item
            for item in jobs
            if item.get("job_type") == "publication_verify"
            and item.get("payload", {}).get("publication_id") in {entry.get("publication_id") for entry in publications}
            and item.get("status") in {"queued", "retry_wait", "running"}
        ),
        None,
    )
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
    elif failed_publication:
        next_action, next_action_key = "重试失败发布", "retry_publish"
    elif pending_publication:
        next_action, next_action_key = "人工确认正式发布", "confirm_publish"
    elif pending_verify_job:
        next_action, next_action_key = "等待线上发布自动校验", "wait_publish_verify"
    elif verifiable_publication:
        next_action, next_action_key = "校验线上结果", "verify_publish"
    elif pending_retest_job:
        next_action, next_action_key = "等待定时复测执行", "wait_retest_job"
    elif latest_injection and latest_injection.get("status") == "completed" and not latest_retest:
        next_action, next_action_key = "安排发布后复测", "schedule_retest"
    effect = "尚未复测"
    if latest_retest:
        delta = int(latest_retest.get("score_delta") or 0)
        effect = "有效优化" if delta > 0 else "未见提升"
    todos = task.get("todos") or []
    if not todos:
        todos = [next_action]
        if score < target_score:
            todos.append(f"将 GEO 分数从 {score} 提升到 {target_score}")
    monitoring = _monitoring_summary(task["task_id"])
    assigned_package = _db_get_service_package(task.get("package_id")) if task.get("package_id") else None
    experiments = _db_experiments(task["task_id"])
    attributions = _db_attributions(task["task_id"])
    reports = _db_reports(task["task_id"])
    readiness_checks = [
        bool(task.get("brand_name")),
        bool(task.get("client_name")),
        bool(task.get("target_engines")),
        monitoring["active_query_count"] > 0,
        bool(task.get("owner")),
    ]
    commercial_ready = all(readiness_checks)
    return {
        **task,
        "project_id": task["task_id"],
        "geo_score": score,
        "owner": task.get("owner") or "待分配",
        "target_score": target_score,
        "current_stage": status,
        "next_action": next_action,
        "next_action_key": next_action_key,
        "todos": todos,
        "assigned_package": assigned_package,
        "package_id": task.get("package_id"),
        "package_name": assigned_package.get("name") if assigned_package else None,
        "experiment_count": len(experiments),
        "active_experiment_count": len([item for item in experiments if item.get("status") in {"draft", "running"}]),
        "attribution_count": len(attributions),
        "confirmed_lead_count": len([item for item in attributions if item.get("status") == "confirmed"]),
        "report_count": len(reports),
        "effectiveness": effect,
        "latest_version": latest_version,
        "latest_injection": latest_injection,
        "latest_retest": latest_retest,
        "latest_publication": latest_publication,
        "pending_job": pending_verify_job or pending_retest_job,
        "monitoring": monitoring,
        "commercial_readiness": {
            "ready": commercial_ready,
            "completed": sum(readiness_checks),
            "total": 5,
            "missing": [
                label
                for value, label in [
                    (task.get("brand_name"), "品牌名称"),
                    (task.get("client_name"), "客户名称"),
                    (task.get("target_engines"), "目标 AI 平台"),
                    (monitoring["active_query_count"], "平台监测问题"),
                    (task.get("owner"), "项目负责人"),
                ]
                if not value
            ],
        },
    }


def _client_safe_task_detail(detail: dict) -> dict:
    versions = []
    for version in detail.get("versions", []):
        versions.append(
            {
                "version_id": version["version_id"],
                "task_id": version["task_id"],
                "status": version["status"],
                "editor": version.get("editor"),
                "reviewer": version.get("reviewer"),
                "review_comment": version.get("review_comment"),
                "modules": version.get("modules", []),
                "quality_report": version.get("quality_report"),
                "created_at": version.get("created_at"),
                "updated_at": version.get("updated_at"),
                "approved_at": version.get("approved_at"),
            }
        )
    return {
        "task": detail["task"],
        "versions": versions,
        "injections": detail.get("injections", []),
        "retests": detail.get("retests", []),
        "project": detail.get("project"),
        "publications": detail.get("publications", []),
        "monitoring": detail.get("monitoring"),
        "reports": detail.get("reports", []),
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


def _db_set_cms_target_enabled(target_id: str, enabled: bool) -> dict | None:
    now = _now_iso()
    with _db() as conn:
        updated = conn.execute(
            "UPDATE cms_targets SET enabled = ?, updated_at = ? WHERE target_id = ?",
            (int(enabled), now, target_id),
        ).rowcount
    if not updated:
        return None
    return _db_get_cms_target(target_id)


def _db_save_knowledge_item(item: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        item.get("workspace_id"),
        item.get("customer_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_items (
                knowledge_id, brand, category, title, content, source, status, created_at, updated_at,
                workspace_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(knowledge_id) DO UPDATE SET
                brand=excluded.brand,
                category=excluded.category,
                title=excluded.title,
                content=excluded.content,
                source=excluded.source,
                status=excluded.status,
                updated_at=excluded.updated_at,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id
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
                workspace_id,
                customer_id,
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
    _append_scope_filters(filters, params)
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
            "workspace_id": row["workspace_id"],
            "customer_id": row["customer_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _db_add_feedback(entry: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        entry.get("workspace_id"),
        entry.get("customer_id"),
        task_id=entry.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO feedback_entries (
                feedback_id, task_id, version_id, publication_id, verdict, notes, source, actor, created_at,
                workspace_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                workspace_id,
                customer_id,
            ),
        )


def _db_feedback(task_id: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM feedback_entries"
    params: list[str | int] = []
    filters: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
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
            "workspace_id": row["workspace_id"],
            "customer_id": row["customer_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _db_add_llm_log(entry: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        entry.get("workspace_id"),
        entry.get("customer_id"),
        task_id=entry.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO llm_logs (
                log_id, task_id, action, provider, model, status, prompt_excerpt, response_excerpt,
                error_message, created_at, workspace_id, customer_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                workspace_id,
                customer_id,
            ),
        )


def _db_llm_logs(task_id: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM llm_logs"
    params: list[str | int] = []
    filters: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
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
            "workspace_id": row["workspace_id"],
            "customer_id": row["customer_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _db_monitor_queries(task_id: str | None = None, active_only: bool = False) -> list[dict]:
    query = "SELECT * FROM monitor_queries"
    filters: list[str] = []
    params: list = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if active_only:
        filters.append("active = 1")
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "query_id": row["query_id"], "task_id": row["task_id"],
            "query_text": row["query_text"], "category": row["category"],
            "competitor": row["competitor"], "engine": row["engine"],
            "active": bool(row["active"]), "created_at": row["created_at"],
            "query_type": row["query_type"],
            "intent_stage": row["intent_stage"],
            "priority": row["priority"],
            "reason": row["reason"],
            "sample_target": row["sample_target"] or 3,
            "language": row["language"],
            "status": row["status"] or ("active" if row["active"] else "paused"),
            "updated_at": row["updated_at"],
            "workspace_id": row["workspace_id"],
            "customer_id": row["customer_id"],
        }
        for row in rows
    ]


def _db_source_observations(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM source_observations"
    params: list[str] = []
    filters: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY observed_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "observation_id": row["observation_id"], "task_id": row["task_id"],
            "query_id": row["query_id"], "source_domain": row["source_domain"],
            "source_url": row["source_url"], "page_type": row["page_type"],
            "citation_count": row["citation_count"], "notes": row["notes"],
            "observed_at": row["observed_at"],
            "workspace_id": row["workspace_id"],
            "customer_id": row["customer_id"],
        }
        for row in rows
    ]


def _db_trust_anchors(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM trust_anchor_tasks"
    params: list[str] = []
    filters: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _db_mention_checks(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM mention_checks"
    params: list[str] = []
    filters: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY checked_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            **dict(row),
            "brand_mentioned": bool(row["brand_mentioned"]),
            "cited_our_domain": bool(row["cited_our_domain"]),
            "competitor_mentions": _json_loads(row["competitor_mentions"], []),
            "confidence_weight": row["confidence_weight"] if row["confidence_weight"] is not None else 1.0,
        }
        for row in rows
    ]


def _package_from_row(row: sqlite3.Row) -> dict:
    return {
        "package_id": row["package_id"],
        "name": row["name"],
        "tier": row["tier"],
        "price_cny": row["price_cny"],
        "delivery_days": row["delivery_days"],
        "platforms": _json_loads(row["platforms"], []),
        "features": _json_loads(row["features"], []),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _db_get_service_package(package_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM service_packages WHERE package_id = ?", (package_id,)).fetchone()
    return _package_from_row(row) if row else None


def _db_service_packages(status: str | None = None) -> list[dict]:
    query = "SELECT * FROM service_packages"
    params: list[str] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY updated_at DESC, name ASC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_package_from_row(row) for row in rows]


def _db_save_service_package(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO service_packages (
                package_id, name, tier, price_cny, delivery_days, platforms,
                features, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(package_id) DO UPDATE SET
                name=excluded.name,
                tier=excluded.tier,
                price_cny=excluded.price_cny,
                delivery_days=excluded.delivery_days,
                platforms=excluded.platforms,
                features=excluded.features,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                item["package_id"],
                item["name"],
                item["tier"],
                item["price_cny"],
                item["delivery_days"],
                _json_dumps(item.get("platforms", [])),
                _json_dumps(item.get("features", [])),
                item["status"],
                item["created_at"],
                item["updated_at"],
            ),
        )


def _experiment_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _db_experiments(task_id: str | None = None, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM content_experiments"
    filters: list[str] = []
    params: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if status:
        filters.append("status = ?")
        params.append(status)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_experiment_from_row(row) for row in rows]


def _db_get_experiment(experiment_id: str) -> dict | None:
    query = "SELECT * FROM content_experiments WHERE experiment_id = ?"
    params: list[str] = [experiment_id]
    scope = _current_scope()
    if scope.workspace_id:
        query += " AND workspace_id = ?"
        params.append(scope.workspace_id)
    if scope.customer_id:
        query += " AND customer_id = ?"
        params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _experiment_from_row(row) if row else None


def _db_save_experiment(item: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        item.get("workspace_id"),
        item.get("customer_id"),
        task_id=item.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO content_experiments (
                experiment_id, task_id, name, hypothesis, channel, primary_metric,
                variant_a, variant_b, status, winner, notes, created_at, updated_at,
                confirmed_by, confirmed_at, workspace_id, customer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(experiment_id) DO UPDATE SET
                name=excluded.name,
                hypothesis=excluded.hypothesis,
                channel=excluded.channel,
                primary_metric=excluded.primary_metric,
                variant_a=excluded.variant_a,
                variant_b=excluded.variant_b,
                status=excluded.status,
                winner=excluded.winner,
                notes=excluded.notes,
                updated_at=excluded.updated_at,
                confirmed_by=excluded.confirmed_by,
                confirmed_at=excluded.confirmed_at,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id
            """,
            (
                item["experiment_id"],
                item["task_id"],
                item["name"],
                item["hypothesis"],
                item["channel"],
                item["primary_metric"],
                item["variant_a"],
                item["variant_b"],
                item["status"],
                item["winner"],
                item["notes"],
                item["created_at"],
                item["updated_at"],
                item["confirmed_by"],
                item["confirmed_at"],
                workspace_id,
                customer_id,
            ),
        )


def _attribution_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _db_attributions(task_id: str | None = None, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM lead_attributions"
    filters: list[str] = []
    params: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if status:
        filters.append("status = ?")
        params.append(status)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_attribution_from_row(row) for row in rows]


def _db_save_attribution(item: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        item.get("workspace_id"),
        item.get("customer_id"),
        task_id=item.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO lead_attributions (
                attribution_id, task_id, source_type, source_name, session_ref,
                lead_stage, attributed_revenue, evidence_url, status, notes, actor,
                created_at, updated_at, workspace_id, customer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attribution_id) DO UPDATE SET
                source_type=excluded.source_type,
                source_name=excluded.source_name,
                session_ref=excluded.session_ref,
                lead_stage=excluded.lead_stage,
                attributed_revenue=excluded.attributed_revenue,
                evidence_url=excluded.evidence_url,
                status=excluded.status,
                notes=excluded.notes,
                updated_at=excluded.updated_at,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id
            """,
            (
                item["attribution_id"],
                item["task_id"],
                item["source_type"],
                item["source_name"],
                item["session_ref"],
                item["lead_stage"],
                item["attributed_revenue"],
                item["evidence_url"],
                item["status"],
                item["notes"],
                item["actor"],
                item["created_at"],
                item["updated_at"],
                workspace_id,
                customer_id,
            ),
        )


def _report_from_row(row: sqlite3.Row) -> dict:
    return {
        "report_id": row["report_id"],
        "task_id": row["task_id"],
        "period_label": row["period_label"],
        "status": row["status"],
        "summary": row["summary"],
        "metrics": _json_loads(row["metrics"], {}),
        "findings": _json_loads(row["findings"], []),
        "next_actions": _json_loads(row["next_actions"], []),
        "generated_at": row["generated_at"],
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
        "notes": row["notes"],
        "workspace_id": row["workspace_id"],
        "customer_id": row["customer_id"],
    }


def _db_reports(task_id: str | None = None, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM effect_reports"
    filters: list[str] = []
    params: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if status:
        filters.append("status = ?")
        params.append(status)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY generated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_report_from_row(row) for row in rows]


def _db_get_report(report_id: str) -> dict | None:
    query = "SELECT * FROM effect_reports WHERE report_id = ?"
    params: list[str] = [report_id]
    scope = _current_scope()
    if scope.workspace_id:
        query += " AND workspace_id = ?"
        params.append(scope.workspace_id)
    if scope.customer_id:
        query += " AND customer_id = ?"
        params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
    return _report_from_row(row) if row else None


def _db_save_report(item: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        item.get("workspace_id"),
        item.get("customer_id"),
        task_id=item.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO effect_reports (
                report_id, task_id, period_label, status, summary, metrics, findings,
                next_actions, generated_at, confirmed_by, confirmed_at, notes, workspace_id, customer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_id) DO UPDATE SET
                period_label=excluded.period_label,
                status=excluded.status,
                summary=excluded.summary,
                metrics=excluded.metrics,
                findings=excluded.findings,
                next_actions=excluded.next_actions,
                confirmed_by=excluded.confirmed_by,
                confirmed_at=excluded.confirmed_at,
                notes=excluded.notes,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id
            """,
            (
                item["report_id"],
                item["task_id"],
                item["period_label"],
                item["status"],
                item["summary"],
                _json_dumps(item.get("metrics", {})),
                _json_dumps(item.get("findings", [])),
                _json_dumps(item.get("next_actions", [])),
                item["generated_at"],
                item.get("confirmed_by"),
                item.get("confirmed_at"),
                item.get("notes"),
                workspace_id,
                customer_id,
            ),
        )


def _source_map(task_id: str) -> dict:
    observations = _db_source_observations(task_id)
    domain_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for item in observations:
        count = max(1, int(item.get("citation_count") or 1))
        domain_counts[item["source_domain"]] = domain_counts.get(item["source_domain"], 0) + count
        type_counts[item["page_type"]] = type_counts.get(item["page_type"], 0) + count
    domains = [
        {"domain": key, "citations": value}
        for key, value in sorted(domain_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    page_types = [
        {"page_type": key, "citations": value}
        for key, value in sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    recommendations: list[dict] = []
    recommendation_map = {
        "comparison": ("创建对比决策页", "加入对比表、适用边界、排除声明和采购检查清单。"),
        "product": ("强化产品页结构", "补充 5-8 组 FAQ Schema，并链向至少 2 个问答或对比页。"),
        "forum": ("建立真实行业回答任务", "选择高质量问题，由专家提供可验证回答；禁止伪装用户或虚构体验。"),
        "media": ("准备行业媒体证据包", "整理数据、案例、专家观点与可核验来源，再开展媒体触达。"),
        "blog": ("建设主题问答内容簇", "围绕高意图问题生产可直接引用的短答案、清单和内链。"),
    }
    for item in page_types[:3]:
        title, action = recommendation_map.get(
            item["page_type"],
            ("复刻高频信源的信息结构", "分析高频来源的标题、证据和回答结构，生成对应页面任务。"),
        )
        recommendations.append({**item, "title": title, "action": action})
    return {
        "task_id": task_id,
        "observation_count": len(observations),
        "citation_total": sum(domain_counts.values()),
        "domains": domains,
        "page_types": page_types,
        "recommendations": recommendations,
    }


def _query_templates(brand_name: str, page_type: str, market: str, competitors: list[str]) -> list[dict]:
    competitor = competitors[0] if competitors else "alternatives"
    templates = [
        ("best", "discover", "P0", f"Best {brand_name} options for {market} travelers", "判断 AI 是否把品牌纳入推荐答案。"),
        ("compare", "compare", "P0", f"{brand_name} vs {competitor}", "覆盖 PRD 要求的竞品差距与对比决策。"),
        ("worth", "decide", "P0", f"Is {brand_name} worth it for {market} travelers?", "覆盖价值判断与购买前疑问。"),
        ("how", "use", "P1", f"How to use {brand_name} after booking?", "覆盖使用流程与政策说明。"),
        ("buy", "buy", "P0", f"Where to buy {brand_name} online?", "覆盖高意图购买问题。"),
        ("scenario", "compare", "P1", f"Best {page_type.replace('_', ' ')} for a first-time {market} trip", "覆盖具体场景与行程选择。"),
        ("risk", "decide", "P1", f"What are the restrictions of {brand_name}?", "覆盖限制、退改和风险问题。"),
        ("local", "discover", "P2", f"{brand_name} for {market} visitors", "覆盖本地市场表达。"),
    ]
    return [
        {
            "query_type": query_type,
            "intent_stage": intent_stage,
            "priority": priority,
            "query_text": query_text,
            "reason": reason,
        }
        for query_type, intent_stage, priority, query_text, reason in templates
    ]


def _infer_page_type_from_url(url: str | None) -> str:
    path = (urlparse(url or "").path or "").lower()
    if any(token in path for token in ["transport", "pass", "rail", "jr"]):
        return "transport_ticket"
    if any(token in path for token in ["things-to-do", "activity", "experience"]):
        return "local_activity"
    if any(token in path for token in ["ticket", "attraction"]):
        return "attraction_ticket"
    if any(token in path for token in ["guide", "travel", "itinerary"]):
        return "destination_guide"
    return "landing_page"


def _generate_monitor_queries(task: dict, query_count: int, languages: list[str] | None = None) -> list[dict]:
    brand = task.get("brand_name") or task.get("title") or "this offer"
    engines = task.get("target_engines") or ["chatgpt", "perplexity"]
    market = ((task.get("latest_result") or {}).get("page_summary") or {}).get("market") or "target market"
    page_type = ((task.get("latest_result") or {}).get("page_summary") or {}).get("product_type") or _infer_page_type_from_url(task.get("url"))
    competitors = []
    for query in _db_monitor_queries(task["task_id"]):
        if query.get("competitor"):
            competitors.append(query["competitor"])
    templates = _query_templates(brand, page_type, market, competitors)
    selected_languages = languages or [((task.get("latest_result") or {}).get("page_summary") or {}).get("language") or "en"]
    generated: list[dict] = []
    now = _now_iso()
    task_id = task["task_id"]
    workspace_id, customer_id = _resolve_scope_ids(task_id=task_id)
    with _db() as conn:
        for engine in engines:
            for language in selected_languages:
                for template in templates:
                    if len(generated) >= max(1, min(query_count, 50)):
                        break
                    query_text = template["query_text"]
                    query_id = f"query_{hashlib.sha256(f'{task_id}:{engine}:{language}:{query_text}'.encode()).hexdigest()[:12]}"
                    conn.execute(
                        """
                        INSERT INTO monitor_queries (
                            query_id, task_id, query_text, category, competitor, engine,
                            active, query_type, intent_stage, priority, reason,
                            sample_target, language, status, created_at, updated_at, workspace_id, customer_id
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(query_id) DO UPDATE SET
                            query_type=excluded.query_type,
                            intent_stage=excluded.intent_stage,
                            priority=excluded.priority,
                            reason=excluded.reason,
                            sample_target=excluded.sample_target,
                            language=excluded.language,
                            status=excluded.status,
                            active=excluded.active,
                            updated_at=excluded.updated_at
                        """,
                        (
                            query_id,
                            task_id,
                            query_text,
                            template["query_type"],
                            None,
                            engine,
                            template["query_type"],
                            template["intent_stage"],
                            template["priority"],
                            template["reason"],
                            3,
                            language,
                            "active",
                            now,
                            now,
                            workspace_id,
                            customer_id,
                        ),
                    )
                    generated.append(query_id)
    all_queries = _db_monitor_queries(task["task_id"])
    return [item for item in all_queries if item["query_id"] in set(generated)]


def _domain_from_url(value: str) -> str | None:
    parsed = urlparse(value.strip())
    if parsed.hostname:
        return parsed.hostname.lower()
    return None


def _infer_source_type(domain: str, url: str | None = None) -> str:
    text = f"{domain} {url or ''}".lower()
    if any(token in text for token in ["reddit", "quora", "forum", "community"]):
        return "forum"
    if any(token in text for token in ["news", "media", "magazine", "blog"]):
        return "media" if "blog" not in text else "blog"
    if any(token in text for token in ["support", "help", "official"]):
        return "official"
    if any(token in text for token in ["product", "ticket", "pass", "tour"]):
        return "product"
    if any(token in text for token in ["guide", "itinerary", "things-to-do"]):
        return "guide"
    return "unknown"


def _parse_ai_sources(request: GEOSourceParseRequest, query: dict, task: dict) -> dict:
    answer = request.answer_text or ""
    sources_text = request.sources_text or ""
    brand_terms = [term.lower() for term in (request.brand_terms or []) if term.strip()]
    if task.get("brand_name"):
        brand_terms.append(task["brand_name"].lower())
    if task.get("url"):
        hostname = urlparse(task["url"]).hostname
        if hostname:
            brand_terms.append(hostname.lower())
    brand_terms = list(dict.fromkeys(brand_terms))
    competitors = [item for item in (request.competitors or []) if item.strip()]
    answer_lower = answer.lower()
    brand_mentioned = any(term and term in answer_lower for term in brand_terms)
    competitor_mentions = [item for item in competitors if item.lower() in answer_lower]

    urls = re.findall(r"""https?://[^\s\]\)>"']+""", sources_text + "\n" + answer)
    domains_seen: dict[str, dict] = {}
    for url in urls:
        cleaned = url.rstrip(".,;，。)")
        domain = _domain_from_url(cleaned)
        if not domain:
            continue
        item = domains_seen.setdefault(
            domain,
            {"domain": domain, "url": cleaned, "count": 0, "page_type": _infer_source_type(domain, cleaned)},
        )
        item["count"] += 1
    for raw_line in sources_text.splitlines():
        line = raw_line.strip()
        if not line or "http" in line:
            continue
        domain_match = re.search(r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)", line.lower())
        if domain_match:
            domain = domain_match.group(1)
            item = domains_seen.setdefault(
                domain,
                {"domain": domain, "url": None, "count": 0, "page_type": _infer_source_type(domain)},
            )
            item["count"] += 1

    now = _now_iso()
    observations = []
    workspace_id, customer_id = _resolve_scope_ids(task_id=request.task_id)
    with _db() as conn:
        for source in domains_seen.values():
            observation = {
                "observation_id": f"source_{uuid.uuid4().hex[:12]}",
                "task_id": request.task_id,
                "query_id": request.query_id,
                "source_domain": source["domain"],
                "source_url": source["url"],
                "page_type": source["page_type"],
                "citation_count": max(1, source["count"]),
                "notes": f"Parsed from {request.platform} answer.",
                "observed_at": now,
                "workspace_id": workspace_id,
                "customer_id": customer_id,
            }
            conn.execute(
                """INSERT INTO source_observations (
                    observation_id, task_id, query_id, source_domain, source_url, page_type,
                    citation_count, notes, observed_at, workspace_id, customer_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                tuple(observation.values()),
            )
            observations.append(observation)

    own_host = urlparse(task.get("url") or "").hostname or ""
    cited_our_domain = any(own_host and own_host.lower() in item["source_domain"] for item in observations)
    mention_position = None
    if brand_mentioned:
        first_index = min(
            [answer_lower.find(term) for term in brand_terms if term and answer_lower.find(term) >= 0],
            default=0,
        )
        mention_position = max(1, answer[:first_index].count("\n") + 1)
    check = GEOMentionCheckRequest(
        task_id=request.task_id,
        query_id=request.query_id,
        engine=request.platform,
        brand_mentioned=brand_mentioned,
        mention_position=mention_position,
        source_type="official" if cited_our_domain else (observations[0]["page_type"] if observations else None),
        source_url=observations[0]["source_url"] if observations else None,
        answer_excerpt=answer[:1000],
        notes="Parsed from pasted AI answer and sources.",
        cited_our_domain=cited_our_domain,
        competitor_mentions=competitor_mentions,
        confidence_weight=1.0 if observations else 0.6,
    )
    return {
        "check": geo_mention_check_save(check),
        "source_observations": observations,
        "parsed": {
            "brand_terms": brand_terms,
            "competitor_mentions": competitor_mentions,
            "source_count": len(observations),
        },
    }


def _monitoring_summary(task_id: str) -> dict:
    queries = _db_monitor_queries(task_id)
    checks = _db_mention_checks(task_id)
    mentioned = [item for item in checks if item["brand_mentioned"]]
    positions = [int(item["mention_position"]) for item in mentioned if item.get("mention_position")]
    source_counts: dict[str, int] = {}
    platform_counts: dict[str, dict] = {}
    cited_own = [item for item in checks if item.get("cited_our_domain")]
    competitor_counter: dict[str, int] = {}
    weekly: dict[str, dict] = {}
    for item in checks:
        for competitor in item.get("competitor_mentions") or []:
            competitor_counter[competitor] = competitor_counter.get(competitor, 0) + 1
        platform = platform_counts.setdefault(
            item["engine"],
            {"engine": item["engine"], "checks": 0, "mentions": 0, "positions": []},
        )
        platform["checks"] += 1
        platform["mentions"] += int(item["brand_mentioned"])
        if item.get("mention_position"):
            platform["positions"].append(int(item["mention_position"]))
        source_type = item.get("source_type") or "unknown"
        source_counts[source_type] = source_counts.get(source_type, 0) + 1
        day = datetime.fromisoformat(item["checked_at"].replace("Z", "+00:00")).date()
        week = (day - timedelta(days=day.weekday())).isoformat()
        bucket = weekly.setdefault(week, {"week": week, "checks": 0, "mentions": 0})
        bucket["checks"] += 1
        bucket["mentions"] += int(item["brand_mentioned"])
    weekly_series = sorted(weekly.values(), key=lambda item: item["week"])
    for item in weekly_series:
        item["mention_rate"] = round(item["mentions"] * 100 / item["checks"]) if item["checks"] else 0
    platform_breakdown = []
    for item in sorted(platform_counts.values(), key=lambda entry: entry["engine"]):
        platform_breakdown.append(
            {
                "engine": item["engine"],
                "checks": item["checks"],
                "mentions": item["mentions"],
                "mention_rate": round(item["mentions"] * 100 / item["checks"]) if item["checks"] else 0,
                "average_position": round(sum(item["positions"]) / len(item["positions"]), 1) if item["positions"] else None,
            }
        )
    sample_target = sum(max(1, int(item.get("sample_target") or 3)) for item in queries if item["active"])
    coverage = round(len(checks) * 100 / sample_target) if sample_target else 0
    if len(checks) >= sample_target and len(checks) >= 9:
        confidence_level = "high"
    elif len(checks) >= max(3, sample_target // 2):
        confidence_level = "medium"
    elif checks:
        confidence_level = "low"
    else:
        confidence_level = "none"
    return {
        "task_id": task_id,
        "query_count": len(queries),
        "active_query_count": len([item for item in queries if item["active"]]),
        "queries": queries,
        "check_count": len(checks),
        "mention_count": len(mentioned),
        "mention_rate": round(len(mentioned) * 100 / len(checks)) if checks else 0,
        "citation_rate": round(len(cited_own) * 100 / len(checks)) if checks else 0,
        "average_position": round(sum(positions) / len(positions), 1) if positions else None,
        "sampling": {
            "sample_target": sample_target,
            "sample_count": len(checks),
            "coverage_percent": coverage,
            "confidence_level": confidence_level,
            "warning": "样本不足，周报只能作为方向参考。" if confidence_level in {"none", "low"} else None,
        },
        "competitor_gap": [
            {"competitor": key, "mentions": value}
            for key, value in sorted(competitor_counter.items(), key=lambda item: (-item[1], item[0]))
        ],
        "source_distribution": [
            {"source_type": key, "checks": value}
            for key, value in sorted(source_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "platform_breakdown": platform_breakdown,
        "weekly": weekly_series,
        "latest_check": checks[0] if checks else None,
        "source_map": _source_map(task_id),
        "trust_anchors": _db_trust_anchors(task_id),
    }


def _build_effect_report(task: dict, period_label: str, notes: str | None = None) -> dict:
    task_id = task["task_id"]
    monitoring = _monitoring_summary(task_id)
    retests = _db_history()["retests"].get(task_id, [])
    attributions = _db_attributions(task_id)
    experiments = _db_experiments(task_id)
    reports = _db_reports(task_id)
    confirmed_attributions = [item for item in attributions if item.get("status") == "confirmed"]
    total_revenue = round(sum(float(item.get("attributed_revenue") or 0) for item in confirmed_attributions), 2)
    won_experiments = [item for item in experiments if item.get("status") == "won"]
    latest_retest = retests[0] if retests else task.get("latest_retest") or {}
    delta = int(latest_retest.get("score_delta") or 0)
    findings = [
            f"AI 平台品牌提及率 {monitoring['mention_rate']}%，累计监测 {monitoring['check_count']} 次。",
            f"官网引用率 {monitoring['citation_rate']}%，采样可信度 {monitoring['sampling']['confidence_level']}。",
            f"已确认线索 {len(confirmed_attributions)} 条，归因收入 {total_revenue:.2f}。",
        f"内容实验完结 {len(won_experiments)} 个，当前 GEO 分数变化 {delta:+d}。",
    ]
    next_actions = [
        "补录待确认线索的证据链接并完成人工确认。",
        "将赢面实验的内容结构同步到主站和 CMS 模板。",
        "继续补充高频信源页型，提升目标 AI 平台提及率。",
    ]
    return {
        "report_id": f"report_{uuid.uuid4().hex[:12]}",
        "task_id": task_id,
        "period_label": period_label,
        "status": "generated",
        "summary": f"{task.get('brand_name') or task.get('title') or task_id} 在 {period_label} 形成 {len(reports) + 1} 期 GEO 效果报告。",
        "metrics": {
            "mention_rate": monitoring["mention_rate"],
            "mention_count": monitoring["mention_count"],
            "check_count": monitoring["check_count"],
            "citation_rate": monitoring["citation_rate"],
            "confidence_level": monitoring["sampling"]["confidence_level"],
            "sample_target": monitoring["sampling"]["sample_target"],
            "confirmed_leads": len(confirmed_attributions),
            "attributed_revenue": total_revenue,
            "won_experiments": len(won_experiments),
            "retest_delta": delta,
        },
        "findings": findings,
        "next_actions": next_actions,
        "generated_at": _now_iso(),
        "confirmed_by": None,
        "confirmed_at": None,
        "notes": notes,
    }


def _seed_monitor_queries(task_id: str, brand_name: str, engines: list[str]) -> None:
    workspace_id, customer_id = _resolve_scope_ids(task_id=task_id)
    now = _now_iso()
    templates = [
        ("category", f"What is {brand_name} and who is it for?"),
        ("comparison", f"{brand_name} vs alternatives"),
        ("recommendation", f"Best solutions like {brand_name}"),
    ]
    with _db() as conn:
        for engine in engines:
            normalized_engine = engine.strip().lower()
            if not normalized_engine:
                continue
            for category, query_text in templates:
                query_id = f"query_{hashlib.sha256(f'{task_id}:{normalized_engine}:{query_text}'.encode()).hexdigest()[:12]}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO monitor_queries (
                        query_id, task_id, query_text, category, competitor, engine,
                        active, created_at, updated_at, workspace_id, customer_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        query_id,
                        task_id,
                        query_text,
                        category,
                        None,
                        normalized_engine,
                        now,
                        now,
                        workspace_id,
                        customer_id,
                    ),
                )


def _db_save_publication(publication: dict) -> None:
    workspace_id, customer_id = _resolve_scope_ids(
        publication.get("workspace_id"),
        publication.get("customer_id"),
        task_id=publication.get("task_id"),
    )
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO publications (
                publication_id, task_id, version_id, target_id, status, preview,
                quality_report, injection_id, confirmed_by, confirmed_at,
                response_summary, live_status, live_summary, live_confirmed_by,
                live_confirmed_at, created_at, updated_at, workspace_id, customer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                updated_at=excluded.updated_at,
                workspace_id=excluded.workspace_id,
                customer_id=excluded.customer_id
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
                workspace_id, customer_id,
            ),
        )


def _db_publications(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM publications"
    params: list[str] = []
    filters: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    _append_scope_filters(filters, params)
    if filters:
        query += " WHERE " + " AND ".join(filters)
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
            "workspace_id": row["workspace_id"], "customer_id": row["customer_id"],
        }
        for row in rows
    ]


def _db_get_publication(publication_id: str) -> dict | None:
    query = "SELECT * FROM publications WHERE publication_id = ?"
    params: list[str] = [publication_id]
    scope = _current_scope()
    if scope.workspace_id:
        query += " AND workspace_id = ?"
        params.append(scope.workspace_id)
    if scope.customer_id:
        query += " AND customer_id = ?"
        params.append(scope.customer_id)
    with _db() as conn:
        row = conn.execute(query, params).fetchone()
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
        "workspace_id": row["workspace_id"],
        "customer_id": row["customer_id"],
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
        if job["job_type"] == "retest":
            job["result"] = geo_retest(GEORetestRequest(**job["payload"]))
        elif job["job_type"] == "publication_verify":
            job["result"] = cms_publication_verify(CMSPublicationVerifyRequest(**job["payload"]))
        else:
            raise ValueError(f"Unsupported job type: {job['job_type']}")
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
    task_filters: list[str] = []
    task_params: list[str] = []
    _append_scope_filters(task_filters, task_params)
    task_query = "SELECT * FROM tasks"
    version_query = "SELECT * FROM versions"
    retest_query = "SELECT * FROM retests"
    injection_query = "SELECT * FROM injections"
    version_filters = list(task_filters)
    version_params = list(task_params)
    retest_filters = list(task_filters)
    retest_params = list(task_params)
    injection_filters = list(task_filters)
    injection_params = list(task_params)
    if task_filters:
        where_clause = " WHERE " + " AND ".join(task_filters)
        task_query += where_clause
        version_query += " WHERE " + " AND ".join(version_filters)
        retest_query += " WHERE " + " AND ".join(retest_filters)
        injection_query += " WHERE " + " AND ".join(injection_filters)
    task_query += " ORDER BY updated_at DESC"
    version_query += " ORDER BY updated_at DESC"
    retest_query += " ORDER BY created_at DESC"
    injection_query += " ORDER BY created_at DESC"
    with _db() as conn:
        task_rows = conn.execute(task_query, task_params).fetchall()
        version_rows = conn.execute(version_query, version_params).fetchall()
        retest_rows = conn.execute(retest_query, retest_params).fetchall()
        injection_rows = conn.execute(injection_query, injection_params).fetchall()

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
            "workspace_id": row["workspace_id"],
            "customer_id": row["customer_id"],
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
        "monitor_queries": _db_monitor_queries(),
        "source_observations": _db_source_observations(),
        "trust_anchors": _db_trust_anchors(),
        "mention_checks": _db_mention_checks(),
        "service_packages": _db_service_packages(),
        "experiments": _db_experiments(),
        "attributions": _db_attributions(),
        "reports": _db_reports(),
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


def _build_task_id(
    url: str,
    workspace_id: str | None = None,
    customer_id: str | None = None,
) -> str:
    scope = _current_scope()
    workspace_id = workspace_id or scope.workspace_id
    customer_id = customer_id or scope.customer_id
    material = f"{workspace_id}:{customer_id}:{url}" if workspace_id or customer_id else url
    return f"geo_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def _build_injection_id(version_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"inject_{version_id}_{stamp}"


class _BlockRedirectHandler(HTTPRedirectHandler):
    def _raise_redirect(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = _raise_redirect
    http_error_302 = _raise_redirect
    http_error_303 = _raise_redirect
    http_error_307 = _raise_redirect
    http_error_308 = _raise_redirect


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


def _open_outbound_request(request: Request, timeout: int):
    opener = build_opener(_BlockRedirectHandler())
    return opener.open(request, timeout=timeout)


def _perform_outbound_request(
    raw_url: str,
    *,
    label: str,
    timeout: int,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
):
    url = _validate_public_url(raw_url, label)
    request = Request(url, data=data, headers=headers or {}, method=method)
    try:
        return _open_outbound_request(request, timeout)
    except HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            location = exc.headers.get("Location") if exc.headers else None
            if not location:
                raise ValueError(f"{label} redirect response is missing a Location header.") from exc
            _validate_public_url(urljoin(url, location), label)
            raise ValueError(f"{label} URL redirects are not allowed.") from exc
        raise


@app.on_event("startup")
def startup_event():
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
    with _perform_outbound_request(
        url,
        label="Page",
        timeout=12,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; GEOGrowthOS/1.0; "
                "+https://example.com/geo-audit)"
            )
        },
    ) as response:
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

    scoped_task_id = _build_task_id(url, request.workspace_id, request.customer_id)
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
            task_id=scoped_task_id,
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
            task_id=scoped_task_id,
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
                    "knowledge_citations": module.get("knowledge_citations") or [],
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
                "knowledge_citations": module.get("knowledge_citations") or [],
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
    return {
        "status": "ok",
        "database": str(DB_PATH),
        "database_url": _database_url(),
        "export_dir": str(EXPORT_DIR),
    }


@app.post("/auth/invites")
def auth_create_invite(request: AuthInviteRequest):
    role = _validate_membership_role(request.role, customer_id=request.customer_id)
    workspace_id, customer_id = _resolve_scope_ids(
        request.workspace_id,
        request.customer_id,
        require_customer=role in CLIENT_SESSION_ROLES,
        allow_bootstrap=False,
    )
    raw_token = secrets.token_urlsafe(24)
    now = _now_iso()
    invite = {
        "invite_id": f"invite_{uuid.uuid4().hex[:12]}",
        "workspace_id": workspace_id,
        "customer_id": customer_id,
        "email": request.email.strip().lower(),
        "role": role,
        "display_name": (request.display_name or "").strip() or None,
        "token_hash": _hash_secret(raw_token),
        "status": "pending",
        "note": (request.note or "").strip() or None,
        "invited_by": _current_actor(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=max(1, min(request.expires_in_days, 30)))).isoformat(),
        "accepted_at": None,
        "created_at": now,
    }
    _db_save_invite(invite)
    _db_add_audit(
        _current_actor(),
        "create_invite",
        "viewer_invite",
        invite["invite_id"],
        outcome="pending",
        detail={"workspace_id": workspace_id, "customer_id": customer_id, "email": invite["email"], "role": role},
        workspace_id=workspace_id,
        customer_id=customer_id,
    )
    return {
        **{key: value for key, value in invite.items() if key != "token_hash"},
        "token": raw_token,
        "accept_url": _invite_accept_url(raw_token),
    }


@app.post("/auth/invites/accept")
def auth_accept_invite(payload: AuthInviteAcceptRequest, request: FastAPIRequest):
    invite = _db_get_invite_by_token(payload.token)
    if not invite:
        raise HTTPException(status_code=404, detail="Invite not found or already used.")
    if invite["expires_at"] <= _now_iso():
        raise HTTPException(status_code=410, detail="Invite has expired.")
    membership = _upsert_membership(
        workspace_id=invite["workspace_id"],
        customer_id=invite.get("customer_id"),
        email=invite["email"],
        role=invite["role"],
        display_name=(payload.display_name or invite.get("display_name") or invite["email"]),
        invited_by=invite.get("invited_by"),
        status="active",
        last_login_at=_now_iso(),
    )
    _db_mark_invite_accepted(invite["invite_id"])
    session_token, _ = _create_browser_session(membership, request)
    workspace = _db_get_workspace(membership["workspace_id"], ignore_scope=True)
    customer = _db_get_customer(membership["customer_id"], ignore_scope=True) if membership.get("customer_id") else None
    _db_add_audit(
        membership["email"],
        "accept_invite",
        "viewer_invite",
        invite["invite_id"],
        outcome="accepted",
        detail={"membership_id": membership["membership_id"]},
        workspace_id=membership["workspace_id"],
        customer_id=membership.get("customer_id"),
    )
    return _session_response(
        {
            "authenticated": True,
            "membership_id": membership["membership_id"],
            "email": membership["email"],
            "display_name": membership.get("display_name"),
            "role": membership["role"],
            "workspace": workspace,
            "customer": customer,
        },
        token=session_token,
    )


@app.get("/auth/session/me")
def auth_session_me(request: FastAPIRequest):
    api_key = extract_api_key(
        request.headers.get("authorization"),
        request.headers.get("x-geo-api-key"),
    )
    if api_key:
        identity = resolve_identity(api_key, allow_unsafe_local_dev=False)
        if not identity:
            raise HTTPException(status_code=401, detail="Invalid API key.")
        scope = _scope_from_headers(request.headers)
        workspace = _db_get_workspace(scope.workspace_id, ignore_scope=True) if scope.workspace_id else None
        customer = _db_get_customer(scope.customer_id, ignore_scope=True) if scope.customer_id else None
        return {
            "authenticated": True,
            "mode": "api_key",
            "actor": identity.name,
            "role": identity.role,
            "workspace": workspace,
            "customer": customer,
        }

    session_token = request.cookies.get(_session_cookie_name())
    if not session_token:
        raise HTTPException(status_code=401, detail="No active browser session.")
    session = _db_get_browser_session(session_token)
    if not session:
        raise HTTPException(status_code=401, detail="Browser session expired or invalid.")
    workspace = _db_get_workspace(session["workspace_id"], ignore_scope=True)
    customer = _db_get_customer(session["customer_id"], ignore_scope=True) if session.get("customer_id") else None
    return {
        "authenticated": True,
        "mode": "session",
        "membership_id": session["membership_id"],
        "email": session["email"],
        "display_name": session.get("display_name"),
        "actor": session.get("display_name") or session["email"],
        "role": session["role"],
        "workspace": workspace,
        "customer": customer,
        "expires_at": session["expires_at"],
    }


@app.post("/auth/session/logout")
def auth_session_logout(request: FastAPIRequest):
    session_token = request.cookies.get(_session_cookie_name())
    if session_token:
        _db_revoke_browser_session(session_token)
    return _clear_session_cookie(JSONResponse({"ok": True}))


@app.get("/workspaces")
def workspaces_list():
    return {"items": _db_workspaces()}


@app.post("/workspaces")
def workspaces_create(request: WorkspaceCreateRequest):
    now = _now_iso()
    workspace = {
        "workspace_id": f"ws_{uuid.uuid4().hex[:12]}",
        "organization_id": BOOTSTRAP_ORG_ID,
        "name": request.name.strip(),
        "slug": _slugify(request.name),
        "market": (request.market or "").strip() or None,
        "language": (request.language or "").strip() or None,
        "status": request.status.strip() or "active",
        "created_at": now,
        "updated_at": now,
    }
    _db_save_workspace(workspace)
    _db_add_audit(_current_actor(), "create_workspace", "workspace", workspace["workspace_id"], workspace_id=workspace["workspace_id"])
    return workspace


@app.get("/customers")
def customers_list(workspace_id: str | None = None):
    scoped_workspace_id, _ = _resolve_scope_ids(workspace_id, _current_scope().customer_id, allow_bootstrap=False)
    return {"items": _db_customers(scoped_workspace_id)}


@app.post("/customers")
def customers_create(request: CustomerCreateRequest):
    workspace_id, _ = _resolve_scope_ids(request.workspace_id, allow_bootstrap=False)
    now = _now_iso()
    customer = {
        "customer_id": f"cust_{uuid.uuid4().hex[:12]}",
        "workspace_id": workspace_id,
        "name": request.name.strip(),
        "slug": _slugify(request.name),
        "market": (request.market or "").strip() or None,
        "language": (request.language or "").strip() or None,
        "status": request.status.strip() or "active",
        "created_at": now,
        "updated_at": now,
    }
    _db_save_customer(customer)
    _db_add_audit(
        _current_actor(),
        "create_customer",
        "customer",
        customer["customer_id"],
        detail={"workspace_id": workspace_id},
        workspace_id=workspace_id,
        customer_id=customer["customer_id"],
    )
    return customer


@app.get("/customers/{customer_id}/members")
def customer_members(customer_id: str):
    _require_internal_panel_access()
    customer = _db_get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return {"items": _db_memberships(workspace_id=customer["workspace_id"], customer_id=customer_id)}


@app.post("/customers/{customer_id}/members")
def customer_member_create(customer_id: str, request: CustomerMemberCreateRequest):
    _require_internal_panel_access()
    customer = _db_get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    membership = _upsert_membership(
        workspace_id=customer["workspace_id"],
        customer_id=customer_id,
        email=request.email,
        role=request.role,
        display_name=request.display_name,
        invited_by=_current_actor(),
        status=request.status.strip() or "active",
    )
    _db_add_audit(
        _current_actor(),
        "create_membership",
        "workspace_membership",
        membership["membership_id"],
        detail={"customer_id": customer_id, "email": membership["email"], "role": membership["role"]},
        workspace_id=customer["workspace_id"],
        customer_id=customer_id,
    )
    return membership


@app.get("/customers/{customer_id}/reports")
def customer_reports(customer_id: str, status: str | None = None):
    customer = _db_get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    scope_token = _set_current_scope(
        RequestScope(workspace_id=customer["workspace_id"], customer_id=customer_id, via_session=_current_scope().via_session)
    )
    try:
        return {"items": _db_reports(status=status)}
    finally:
        _reset_current_scope(scope_token)


@app.post("/customers/{customer_id}/reports")
def customer_report_create(customer_id: str, request: CustomerReportRequest):
    customer = _db_get_customer(customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    task = _db_get_task(request.task_id)
    if not task or task.get("customer_id") != customer_id:
        raise HTTPException(status_code=404, detail="Task not found for customer.")
    report = _build_effect_report(task, request.period_label, request.notes)
    _db_save_report(report)
    _db_add_audit(
        _current_actor(),
        "generate_customer_report",
        "report",
        report["report_id"],
        request.task_id,
        workspace_id=customer["workspace_id"],
        customer_id=customer_id,
    )
    return report


@app.get("/admin", include_in_schema=False)
def admin_console():
    return FileResponse(ADMIN_INDEX)


@app.get("/admin/api/overview")
def admin_overview():
    _require_internal_panel_access()
    return build_admin_overview(_db_history())


@app.get("/admin/api/tasks")
def admin_tasks(status: str | None = None, q: str | None = None, limit: int = 50):
    _require_internal_panel_access()
    return {"items": filter_admin_tasks(_db_history(), status, q, limit)}


@app.get("/admin/api/tasks/{task_id}")
def admin_task_detail(task_id: str):
    _require_internal_panel_access()
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
    _require_internal_panel_access()
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
    _require_internal_panel_access()
    return {"items": _db_knowledge_items(status=status, brand=brand, limit=limit)}


@app.get("/admin/api/feedback")
def admin_feedback(task_id: str | None = None, limit: int = 100):
    _require_internal_panel_access()
    return {"items": _db_feedback(task_id=task_id, limit=limit)}


@app.get("/admin/api/llm-logs")
def admin_llm_logs(task_id: str | None = None, limit: int = 100):
    _require_internal_panel_access()
    return {"items": _db_llm_logs(task_id=task_id, limit=limit)}


@app.get("/admin/api/jobs")
def admin_jobs(status: str | None = None, limit: int = 50):
    _require_internal_panel_access()
    return {"items": _db_jobs(status=status, limit=limit)}


@app.post("/admin/api/jobs/run-due")
def admin_run_due_jobs(limit: int = 20):
    _require_internal_panel_access()
    jobs = _db_jobs(limit=limit, due_only=True)
    return {"items": [_run_job(job["job_id"]) for job in reversed(jobs)]}


@app.post("/admin/api/jobs/{job_id}/retry")
def admin_retry_job(job_id: str, background_tasks: BackgroundTasks):
    _require_internal_panel_access()
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
    allow_bootstrap = current_identity().name == "local-dev" and not _current_scope().workspace_id
    workspace_id, customer_id = _resolve_scope_ids(
        request.workspace_id,
        request.customer_id,
        require_customer=not allow_bootstrap,
        allow_bootstrap=allow_bootstrap,
    )
    request = request.model_copy(update={"workspace_id": workspace_id, "customer_id": customer_id})
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
    task_id = _build_task_id(url, workspace_id, customer_id)
    result = {
        "task_id": task_id,
        "workspace_id": workspace_id,
        "customer_id": customer_id,
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
        "client_name": request.client_name or existing_task.get("client_name"),
        "brand_name": request.brand_name or existing_task.get("brand_name") or title,
        "target_engines": request.target_engines or existing_task.get("target_engines") or ["chatgpt", "perplexity"],
        "business_goal": request.business_goal or existing_task.get("business_goal"),
        "service_tier": existing_task.get("service_tier") or "growth",
        "workspace_id": workspace_id,
        "customer_id": customer_id,
        "created_at": existing_task.get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
    })
    _seed_monitor_queries(
        task_id,
        request.brand_name or existing_task.get("brand_name") or title,
        request.target_engines or existing_task.get("target_engines") or ["chatgpt", "perplexity"],
    )
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
            normalized_modules = _attach_knowledge_citations(
                normalized_modules,
                workflow.get("knowledge_snapshot") or [],
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
    task = _db_get_task(request.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

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
            with _perform_outbound_request(
                webhook_url,
                label="Webhook",
                timeout=15,
                method="POST",
                headers=headers,
                data=_json_dumps(payload).encode("utf-8"),
            ) as response:
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
        "payload": request.model_dump(exclude={"run_at", "max_attempts"}),
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
    jobs = _db_jobs(limit=200)
    return {
        "items": [
            _project_view(
                task,
                [item for item in history["versions"] if item["task_id"] == task["task_id"]],
                [item for item in history["injections"] if item["task_id"] == task["task_id"]],
                history["retests"].get(task["task_id"], []),
                [item for item in history["publications"] if item["task_id"] == task["task_id"]],
                [item for item in jobs if item.get("payload", {}).get("task_id") == task["task_id"]],
            )
            for task in history["tasks"]
        ]
    }


@app.get("/geo/projects/{task_id}")
def geo_project_detail(task_id: str):
    detail = geo_task_detail(task_id)
    detail["project"] = _project_view(
        detail["task"],
        detail["versions"],
        detail["injections"],
        detail["retests"],
        detail["publications"],
        detail.get("jobs"),
    )
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
    if request.client_name is not None:
        task["client_name"] = request.client_name.strip() or None
    if request.brand_name is not None:
        task["brand_name"] = request.brand_name.strip() or None
    if request.target_engines is not None:
        task["target_engines"] = list(dict.fromkeys(item.strip().lower() for item in request.target_engines if item.strip()))[:8]
    if request.business_goal is not None:
        task["business_goal"] = request.business_goal.strip() or None
    if request.service_tier is not None:
        task["service_tier"] = request.service_tier.strip() or None
    if request.package_id is not None:
        package_id = request.package_id.strip() or None
        if package_id and not _db_get_service_package(package_id):
            raise HTTPException(status_code=404, detail="Service package not found.")
        task["package_id"] = package_id
    task["updated_at"] = _now_iso()
    _db_upsert_task(task)
    if task.get("brand_name") and task.get("target_engines"):
        _seed_monitor_queries(task_id, task["brand_name"], task["target_engines"])
    _db_add_audit(_current_actor(), "update_project", "task", task_id, task_id)
    history = _db_history()
    return _project_view(
        task,
        [item for item in history["versions"] if item["task_id"] == task_id],
        [item for item in history["injections"] if item["task_id"] == task_id],
        history["retests"].get(task_id, []),
        _db_publications(task_id),
        [item for item in _db_jobs(limit=100) if item.get("payload", {}).get("task_id") == task_id],
    )


@app.get("/geo/service-packages")
def geo_service_packages(status: str | None = None):
    return {"items": _db_service_packages(status=status)}


@app.post("/geo/service-packages")
def geo_service_package_save(request: GEOServicePackageRequest):
    now = _now_iso()
    package_id = f"pkg_{hashlib.sha256(f'{request.name}:{request.tier}'.encode()).hexdigest()[:12]}"
    existing = _db_get_service_package(package_id)
    item = {
        "package_id": package_id,
        "name": request.name.strip(),
        "tier": request.tier.strip() or "growth",
        "price_cny": max(0, int(request.price_cny)),
        "delivery_days": max(1, min(int(request.delivery_days), 365)),
        "platforms": [item.strip().lower() for item in (request.platforms or []) if item.strip()][:10],
        "features": [item.strip() for item in (request.features or []) if item.strip()][:20],
        "status": request.status.strip() or "active",
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    _db_save_service_package(item)
    _db_add_audit(_current_actor(), "save_service_package", "service_package", package_id)
    return item


@app.get("/geo/experiments")
def geo_experiments(task_id: str | None = None, status: str | None = None):
    return {"items": _db_experiments(task_id=task_id, status=status)}


@app.post("/geo/experiments")
def geo_experiment_save(request: GEOExperimentRequest):
    if not _db_get_task(request.task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    now = _now_iso()
    item = {
        "experiment_id": f"exp_{uuid.uuid4().hex[:12]}",
        "task_id": request.task_id,
        "name": request.name.strip(),
        "hypothesis": request.hypothesis.strip(),
        "channel": request.channel.strip() or "onsite",
        "primary_metric": request.primary_metric.strip() or "mention_rate",
        "variant_a": request.variant_a.strip(),
        "variant_b": request.variant_b.strip(),
        "status": request.status.strip() or "draft",
        "winner": None,
        "notes": (request.notes or "").strip() or None,
        "created_at": now,
        "updated_at": now,
        "confirmed_by": None,
        "confirmed_at": None,
    }
    _db_save_experiment(item)
    _db_add_audit(_current_actor(), "save_experiment", "experiment", item["experiment_id"], request.task_id)
    return item


@app.post("/geo/experiments/{experiment_id}/confirm")
def geo_experiment_confirm(experiment_id: str, request: GEOExperimentConfirmRequest):
    item = _db_get_experiment(experiment_id)
    if not item:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    item["status"] = request.status.strip() or item["status"]
    item["winner"] = (request.winner or "").strip() or None
    item["notes"] = (request.notes or "").strip() or item.get("notes")
    item["updated_at"] = _now_iso()
    item["confirmed_by"] = _current_actor()
    item["confirmed_at"] = _now_iso()
    _db_save_experiment(item)
    _db_add_audit(_current_actor(), "confirm_experiment", "experiment", experiment_id, item["task_id"], outcome=item["status"])
    return item


@app.get("/geo/attributions")
def geo_attributions(task_id: str | None = None, status: str | None = None):
    return {"items": _db_attributions(task_id=task_id, status=status)}


@app.post("/geo/attributions")
def geo_attribution_save(request: GEOAttributionRequest):
    if not _db_get_task(request.task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    now = _now_iso()
    item = {
        "attribution_id": f"attr_{uuid.uuid4().hex[:12]}",
        "task_id": request.task_id,
        "source_type": request.source_type.strip(),
        "source_name": request.source_name.strip(),
        "session_ref": (request.session_ref or "").strip() or None,
        "lead_stage": request.lead_stage.strip() or "new",
        "attributed_revenue": round(float(request.attributed_revenue or 0), 2),
        "evidence_url": (request.evidence_url or "").strip() or None,
        "status": request.status.strip() or "pending_confirmation",
        "notes": (request.notes or "").strip() or None,
        "actor": _current_actor(),
        "created_at": now,
        "updated_at": now,
    }
    _db_save_attribution(item)
    _db_add_audit(_current_actor(), "save_attribution", "attribution", item["attribution_id"], request.task_id, outcome=item["status"])
    return item


@app.get("/geo/reports")
def geo_reports(task_id: str | None = None, status: str | None = None):
    return {"items": _db_reports(task_id=task_id, status=status)}


@app.post("/geo/reports/generate")
def geo_report_generate(request: GEOReportGenerateRequest):
    task = _db_get_task(request.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Project not found.")
    report = _build_effect_report(task, request.period_label.strip() or "近 30 天", request.notes)
    _db_save_report(report)
    _db_add_audit(_current_actor(), "generate_report", "report", report["report_id"], request.task_id)
    return report


@app.post("/geo/reports/{report_id}/confirm")
def geo_report_confirm(report_id: str, request: GEOReportConfirmRequest):
    report = _db_get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    report["status"] = request.status.strip() or report["status"]
    report["notes"] = (request.notes or "").strip() or report.get("notes")
    report["confirmed_by"] = _current_actor()
    report["confirmed_at"] = _now_iso()
    _db_save_report(report)
    _db_add_audit(_current_actor(), "confirm_report", "report", report_id, report["task_id"], outcome=report["status"])
    return report


@app.get("/geo/monitoring/queries")
def geo_monitor_queries(task_id: str | None = None):
    return {"items": _db_monitor_queries(task_id)}


@app.post("/geo/monitoring/queries/generate")
def geo_monitor_queries_generate(request: GEOQueryGenerateRequest):
    task = _db_get_task(request.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Project not found.")
    queries = _generate_monitor_queries(
        task,
        request.query_count,
        request.languages,
    )
    _db_add_audit(
        _current_actor(),
        "generate_queries",
        "task",
        request.task_id,
        request.task_id,
        detail={"count": len(queries), "languages": request.languages or []},
    )
    return {"items": queries, "count": len(queries)}


@app.post("/geo/monitoring/queries")
def geo_monitor_query_save(request: GEOMonitorQueryRequest):
    if not _db_get_task(request.task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    query_text = request.query_text.strip()
    if not query_text:
        raise HTTPException(status_code=400, detail="Query text is required.")
    query_id = f"query_{hashlib.sha256(f'{request.task_id}:{request.engine}:{query_text}'.encode()).hexdigest()[:12]}"
    now = _now_iso()
    workspace_id, customer_id = _resolve_scope_ids(task_id=request.task_id)
    with _db() as conn:
        existing = conn.execute("SELECT created_at FROM monitor_queries WHERE query_id = ?", (query_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO monitor_queries (
                query_id, task_id, query_text, category, competitor, engine,
                active, query_type, intent_stage, priority, reason, sample_target,
                language, status, created_at, updated_at, workspace_id, customer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_id) DO UPDATE SET
                category=excluded.category, competitor=excluded.competitor,
                engine=excluded.engine, active=excluded.active,
                query_type=excluded.query_type,
                intent_stage=excluded.intent_stage,
                priority=excluded.priority,
                reason=excluded.reason,
                sample_target=excluded.sample_target,
                language=excluded.language,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                query_id, request.task_id, query_text, request.category.strip() or "comparison",
                (request.competitor or "").strip() or None, request.engine.strip() or "perplexity",
                int(request.active), request.query_type or request.category.strip() or "comparison",
                request.intent_stage or "compare", request.priority or "P1",
                (request.reason or "").strip() or None,
                max(1, min(request.sample_target, 30)), request.language,
                request.status.strip() or ("active" if request.active else "paused"),
                existing["created_at"] if existing else now, now, workspace_id, customer_id,
            ),
        )
    _db_add_audit(_current_actor(), "save_monitor_query", "monitor_query", query_id, request.task_id)
    return next(item for item in _db_monitor_queries(request.task_id) if item["query_id"] == query_id)


@app.post("/geo/monitoring/sources")
def geo_source_observation_save(request: GEOSourceObservationRequest):
    query = next((item for item in _db_monitor_queries(request.task_id) if item["query_id"] == request.query_id), None)
    if not query:
        raise HTTPException(status_code=404, detail="Monitoring query not found.")
    domain = request.source_domain.strip().lower()
    if not domain:
        raise HTTPException(status_code=400, detail="Source domain is required.")
    observation = {
        "observation_id": f"source_{uuid.uuid4().hex[:12]}",
        "task_id": request.task_id,
        "query_id": request.query_id,
        "source_domain": domain,
        "source_url": (request.source_url or "").strip() or None,
        "page_type": request.page_type.strip().lower() or "unknown",
        "citation_count": max(1, min(request.citation_count, 1000)),
        "notes": (request.notes or "").strip() or None,
        "observed_at": _now_iso(),
    }
    workspace_id, customer_id = _resolve_scope_ids(task_id=request.task_id)
    observation["workspace_id"] = workspace_id
    observation["customer_id"] = customer_id
    with _db() as conn:
        conn.execute(
            """INSERT INTO source_observations (
                observation_id, task_id, query_id, source_domain, source_url, page_type,
                citation_count, notes, observed_at, workspace_id, customer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(observation.values()),
        )
    _db_add_audit(_current_actor(), "save_source_observation", "source_observation", observation["observation_id"], request.task_id)
    return observation


@app.post("/geo/monitoring/sources/parse")
def geo_source_answer_parse(request: GEOSourceParseRequest):
    task = _db_get_task(request.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Project not found.")
    query = next((item for item in _db_monitor_queries(request.task_id) if item["query_id"] == request.query_id), None)
    if not query:
        raise HTTPException(status_code=404, detail="Monitoring query not found.")
    if not request.answer_text.strip() and not request.sources_text.strip():
        raise HTTPException(status_code=400, detail="Answer text or sources text is required.")
    parsed = _parse_ai_sources(request, query, task)
    _db_add_audit(
        _current_actor(),
        "parse_ai_sources",
        "monitor_query",
        request.query_id,
        request.task_id,
        detail={"platform": request.platform, "source_count": len(parsed["source_observations"])},
    )
    return parsed


@app.get("/geo/monitoring/source-map")
def geo_source_map(task_id: str):
    if not _db_get_task(task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    return _source_map(task_id)


@app.get("/geo/monitoring/trust-anchors")
def geo_trust_anchors(task_id: str | None = None):
    return {"items": _db_trust_anchors(task_id)}


@app.post("/geo/monitoring/trust-anchors")
def geo_trust_anchor_save(request: GEOTrustAnchorRequest):
    if not _db_get_task(request.task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    now = _now_iso()
    anchor = {
        "anchor_id": f"anchor_{uuid.uuid4().hex[:12]}",
        "task_id": request.task_id,
        "channel": request.channel.strip().lower(),
        "topic": request.topic.strip(),
        "target_url": (request.target_url or "").strip() or None,
        "owner": (request.owner or "").strip() or None,
        "status": request.status.strip() or "planned",
        "guidance": (request.guidance or "").strip() or "提供真实、可验证、有帮助的行业回答，不伪装用户或虚构体验。",
        "evidence_url": (request.evidence_url or "").strip() or None,
        "created_at": now,
        "updated_at": now,
    }
    workspace_id, customer_id = _resolve_scope_ids(task_id=request.task_id)
    anchor["workspace_id"] = workspace_id
    anchor["customer_id"] = customer_id
    with _db() as conn:
        conn.execute(
            """INSERT INTO trust_anchor_tasks (
                anchor_id, task_id, channel, topic, target_url, owner, status,
                guidance, evidence_url, created_at, updated_at, workspace_id, customer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(anchor.values()),
        )
    _db_add_audit(_current_actor(), "save_trust_anchor", "trust_anchor", anchor["anchor_id"], request.task_id)
    return anchor


@app.get("/geo/monitoring/checks")
def geo_mention_checks(task_id: str | None = None):
    return {"items": _db_mention_checks(task_id)}


@app.post("/geo/monitoring/checks")
def geo_mention_check_save(request: GEOMentionCheckRequest):
    query = next((item for item in _db_monitor_queries(request.task_id) if item["query_id"] == request.query_id), None)
    if not query:
        raise HTTPException(status_code=404, detail="Monitoring query not found.")
    position = request.mention_position if request.brand_mentioned else None
    if position is not None:
        position = max(1, min(position, 100))
    check = {
        "check_id": f"check_{uuid.uuid4().hex[:12]}",
        "task_id": request.task_id,
        "query_id": request.query_id,
        "engine": request.engine.strip() or query["engine"],
        "brand_mentioned": bool(request.brand_mentioned),
        "mention_position": position,
        "source_type": (request.source_type or "").strip().lower() or None,
        "source_url": (request.source_url or "").strip() or None,
        "answer_excerpt": (request.answer_excerpt or "").strip()[:1000] or None,
        "notes": (request.notes or "").strip() or None,
        "cited_our_domain": bool(request.cited_our_domain),
        "competitor_mentions": [item.strip() for item in (request.competitor_mentions or []) if item.strip()],
        "confidence_weight": max(0.1, min(float(request.confidence_weight or 1), 1.0)),
        "checked_at": _now_iso(),
    }
    workspace_id, customer_id = _resolve_scope_ids(task_id=request.task_id)
    with _db() as conn:
        conn.execute(
            """INSERT INTO mention_checks (
                check_id, task_id, query_id, engine, brand_mentioned, mention_position,
                source_type, source_url, answer_excerpt, notes, cited_our_domain,
                competitor_mentions, confidence_weight, checked_at, workspace_id, customer_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                check["check_id"], check["task_id"], check["query_id"], check["engine"],
                int(check["brand_mentioned"]), check["mention_position"], check["source_type"],
                check["source_url"], check["answer_excerpt"], check["notes"],
                int(check["cited_our_domain"]), _json_dumps(check["competitor_mentions"]),
                check["confidence_weight"], check["checked_at"], workspace_id, customer_id,
            ),
        )
    _db_add_audit(
        _current_actor(), "save_mention_check", "mention_check", check["check_id"], request.task_id,
        detail={
            "mentioned": check["brand_mentioned"],
            "position": check["mention_position"],
            "cited_our_domain": check["cited_our_domain"],
            "competitors": check["competitor_mentions"],
        },
    )
    return check


@app.get("/geo/monitoring/summary")
def geo_monitoring_summary(task_id: str):
    if not _db_get_task(task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    return _monitoring_summary(task_id)


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
    if not _db_get_task(request.task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
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


@app.patch("/cms/targets/{target_id}")
def cms_target_update_status(target_id: str, request: CMSPublishTargetStatusRequest):
    target = _db_set_cms_target_enabled(target_id, request.enabled)
    if not target:
        raise HTTPException(status_code=404, detail="CMS target not found.")
    _db_add_audit(
        _current_actor(),
        "toggle_cms_target",
        "cms_target",
        target_id,
        outcome="enabled" if request.enabled else "disabled",
    )
    return target


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


@app.post("/cms/publications/verify/schedule", status_code=202)
def cms_publication_verify_schedule(
    request: CMSPublicationVerifyScheduleRequest,
    background_tasks: BackgroundTasks,
):
    publication = _db_get_publication(request.publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="Publication not found.")
    if publication["status"] not in {"published", "verification_failed", "verified_live"}:
        raise HTTPException(status_code=409, detail="Publication must be published before verification can be scheduled.")
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
        "job_type": "publication_verify",
        "status": "queued",
        "payload": request.model_dump(exclude={"run_at", "max_attempts"}),
        "result": None,
        "attempts": 0,
        "max_attempts": max_attempts,
        "run_at": run_at,
        "last_error": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "completed_at": None,
        "workspace_id": publication.get("workspace_id"),
        "customer_id": publication.get("customer_id"),
    }
    _db_save_job(job)
    _db_add_audit(
        _current_actor(),
        "schedule_publish_verify",
        "job",
        job["job_id"],
        publication["task_id"],
        detail={"publication_id": request.publication_id, "run_at": run_at},
    )
    if run_at <= _now_iso():
        background_tasks.add_task(_run_job, job["job_id"])
    return job


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
    if _is_client_session():
        raise HTTPException(status_code=403, detail="Client sessions cannot read global history exports.")
    return _db_history()


@app.get("/geo/tasks/{task_id}")
def geo_task_detail(task_id: str):
    task = _db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    history = _db_history()
    task_versions = [item for item in history["versions"] if item["task_id"] == task_id]
    task_injections = [item for item in history["injections"] if item["task_id"] == task_id]
    task_retests = history["retests"].get(task_id, [])
    task_publications = _db_publications(task_id)
    task_feedback = [item for item in history["feedback_entries"] if item["task_id"] == task_id]
    task_jobs = [
        item
        for item in _db_jobs(limit=100)
        if item.get("payload", {}).get("task_id") == task_id
    ]
    task_experiments = _db_experiments(task_id)
    task_attributions = _db_attributions(task_id)
    task_reports = _db_reports(task_id)
    detail = {
        "task": task,
        "versions": task_versions,
        "injections": task_injections,
        "retests": task_retests,
        "feedback": task_feedback,
        "llm_logs": [item for item in history["llm_logs"] if item.get("task_id") == task_id],
        "knowledge_items": task.get("latest_result", {}).get("knowledge_snapshot", []),
        "project": _project_view(
            task,
            task_versions,
            task_injections,
            task_retests,
            task_publications,
            task_jobs,
        ),
        "publications": task_publications,
        "jobs": task_jobs,
        "monitoring": _monitoring_summary(task_id),
        "service_packages": _db_service_packages(status="active"),
        "experiments": task_experiments,
        "attributions": task_attributions,
        "reports": task_reports,
    }
    if _is_client_session():
        return _client_safe_task_detail(detail)
    return detail


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
