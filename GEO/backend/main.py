import json
import hashlib
import ipaddress
import os
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional
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


class CMSPublicationRollbackRequest(BaseModel):
    publication_id: str
    status: str = "rollback_completed"
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


class GEOTrustAnchorUpdateRequest(BaseModel):
    channel: str | None = None
    topic: str | None = None
    status: str | None = None
    owner: str | None = None
    target_url: str | None = None
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


class GEOExperimentEventRequest(BaseModel):
    status: str
    notes: str | None = None
    sample_size: int | None = None
    metric_value: float | None = None


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


class GEOAttributionUpdateRequest(BaseModel):
    source_type: str | None = None
    source_name: str | None = None
    session_ref: str | None = None
    lead_stage: str | None = None
    attributed_revenue: float | None = None
    evidence_url: str | None = None
    status: str | None = None
    notes: str | None = None


class GEOReportGenerateRequest(BaseModel):
    task_id: str
    period_label: str = "近 30 天"
    notes: str | None = None


class GEOReportConfirmRequest(BaseModel):
    status: str = "confirmed"
    notes: str | None = None


class GEOReportShareRequest(BaseModel):
    share_status: str = "shared"
    share_channel: str
    notes: str | None = None


class GEOArticleCreateRequest(BaseModel):
    task_id: str
    title: str | None = None
    folder_token: str | None = None
    use_ai: bool = False
    provider: str = "openai"
    model: str | None = None
    publish_to_feishu: bool = True
    feishu_identity: str = "bot"


class GEOArticleFeishuSyncRequest(BaseModel):
    folder_token: str | None = None
    feishu_identity: str = "bot"


class GEOArticleIndexingRequest(BaseModel):
    public_url: str | None = None
    index_status: str = "published"
    notes: str | None = None
    indexed_at: str | None = None


class GEOMonitorConnectorRequest(BaseModel):
    task_id: str
    platform: str
    connector_type: str = "manual_audit"
    provider_name: str
    status: str = "planned"
    credential_env_var: str | None = None
    evidence_url: str | None = None
    verification_method: str = "human_recorded"
    notes: str | None = None
    owner: str | None = None
    next_check_at: str | None = None
    recovery_hint: str | None = None


class GEOMonitorConnectorStatusRequest(BaseModel):
    status: str | None = None
    platform: str | None = None
    connector_type: str | None = None
    provider_name: str | None = None
    credential_env_var: str | None = None
    evidence_url: str | None = None
    last_error: str | None = None
    notes: str | None = None
    verification_method: str | None = None
    owner: str | None = None
    next_check_at: str | None = None
    recovery_hint: str | None = None


class GEOMonitorConnectorRunRequest(BaseModel):
    status: str
    evidence_url: str | None = None
    notes: str | None = None
    last_error: str | None = None
    next_check_at: str | None = None


class GEOGapActionRequest(BaseModel):
    task_id: str
    title: str
    action_type: str = "content_gap"
    source: str = "manual"
    priority: str = "P1"
    status: str = "accepted"
    owner: str | None = None
    related_object_id: str | None = None
    notes: str | None = None
    evidence_url: str | None = None


class GEOPackageDeliveryUpdateRequest(BaseModel):
    feature_key: str
    status: str
    notes: str | None = None


class GEOGapActionUpdateRequest(BaseModel):
    title: str | None = None
    priority: str | None = None
    status: str | None = None
    owner: str | None = None
    notes: str | None = None
    evidence_url: str | None = None


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


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso_after_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


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
                client_name TEXT,
                brand_name TEXT,
                target_engines TEXT,
                business_goal TEXT,
                service_tier TEXT,
                package_id TEXT,
                package_assigned_at TEXT,
                package_due_at TEXT,
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
            CREATE TABLE IF NOT EXISTS experiment_events (
                event_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT,
                sample_size INTEGER,
                metric_value REAL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
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
                notes TEXT,
                share_status TEXT,
                share_channel TEXT,
                shared_at TEXT,
                share_notes TEXT
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
                rollback_status TEXT,
                rollback_note TEXT,
                rollback_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publication_events (
                event_id TEXT PRIMARY KEY,
                publication_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
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
            CREATE TABLE IF NOT EXISTS monitor_connectors (
                connector_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                status TEXT NOT NULL,
                credential_env_var TEXT,
                evidence_url TEXT,
                verification_method TEXT NOT NULL,
                notes TEXT,
                owner TEXT,
                next_check_at TEXT,
                recovery_hint TEXT,
                last_error TEXT,
                last_checked_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS page_gap_actions (
                action_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                title TEXT NOT NULL,
                action_type TEXT NOT NULL,
                source TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                owner TEXT,
                related_object_id TEXT,
                notes TEXT,
                evidence_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS connector_runs (
                run_id TEXT PRIMARY KEY,
                connector_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT,
                evidence_url TEXT,
                last_error TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS report_exports (
                export_id TEXT PRIMARY KEY,
                report_id TEXT,
                task_id TEXT,
                project_name TEXT,
                title TEXT NOT NULL,
                format TEXT NOT NULL,
                filepath TEXT,
                external_url TEXT,
                external_id TEXT,
                status TEXT NOT NULL,
                note TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS package_delivery_updates (
                update_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                feature_key TEXT NOT NULL,
                feature_name TEXT NOT NULL,
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
            CREATE TABLE IF NOT EXISTS geo_articles (
                article_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                markdown_path TEXT,
                feishu_url TEXT,
                feishu_token TEXT,
                feishu_response TEXT,
                feishu_status TEXT,
                last_feishu_sync_at TEXT,
                public_url TEXT,
                index_status TEXT,
                indexing_notes TEXT,
                indexing_plan TEXT,
                indexed_at TEXT,
                last_index_checked_at TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS article_index_events (
                event_id TEXT PRIMARY KEY,
                article_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                index_status TEXT NOT NULL,
                public_url TEXT,
                notes TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        article_columns = {row["name"] for row in conn.execute("PRAGMA table_info(geo_articles)").fetchall()}
        for column, definition in {
            "public_url": "TEXT",
            "index_status": "TEXT",
            "indexing_notes": "TEXT",
            "indexing_plan": "TEXT",
            "indexed_at": "TEXT",
            "last_index_checked_at": "TEXT",
            "feishu_status": "TEXT",
            "last_feishu_sync_at": "TEXT",
        }.items():
            if column not in article_columns:
                conn.execute(f"ALTER TABLE geo_articles ADD COLUMN {column} {definition}")
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
            "package_assigned_at": "TEXT",
            "package_due_at": "TEXT",
        }.items():
            if column not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        connector_columns = {row["name"] for row in conn.execute("PRAGMA table_info(monitor_connectors)").fetchall()}
        for column, definition in {
            "owner": "TEXT",
            "next_check_at": "TEXT",
            "recovery_hint": "TEXT",
        }.items():
            if column not in connector_columns:
                conn.execute(f"ALTER TABLE monitor_connectors ADD COLUMN {column} {definition}")
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
        report_columns = {row["name"] for row in conn.execute("PRAGMA table_info(effect_reports)").fetchall()}
        for column, definition in {
            "share_status": "TEXT",
            "share_channel": "TEXT",
            "shared_at": "TEXT",
            "share_notes": "TEXT",
        }.items():
            if column not in report_columns:
                conn.execute(f"ALTER TABLE effect_reports ADD COLUMN {column} {definition}")
        report_export_columns = {row["name"] for row in conn.execute("PRAGMA table_info(report_exports)").fetchall()}
        for column, definition in {
            "external_url": "TEXT",
            "external_id": "TEXT",
        }.items():
            if column not in report_export_columns:
                conn.execute(f"ALTER TABLE report_exports ADD COLUMN {column} {definition}")
        publication_columns = {row["name"] for row in conn.execute("PRAGMA table_info(publications)").fetchall()}
        for column, definition in {
            "live_status": "TEXT",
            "live_summary": "TEXT",
            "live_confirmed_by": "TEXT",
            "live_confirmed_at": "TEXT",
            "rollback_status": "TEXT",
            "rollback_note": "TEXT",
            "rollback_at": "TEXT",
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
                latest_version_id, latest_retest, owner, target_score, todos,
                client_name, brand_name, target_engines, business_goal, service_tier,
                package_id, package_assigned_at, package_due_at, created_at, updated_at
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
                package_assigned_at=excluded.package_assigned_at,
                package_due_at=excluded.package_due_at,
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
                task.get("package_assigned_at"),
                task.get("package_due_at"),
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
        "client_name": row["client_name"],
        "brand_name": row["brand_name"],
        "target_engines": _json_loads(row["target_engines"], []),
        "business_goal": row["business_goal"],
        "service_tier": row["service_tier"],
        "package_id": row["package_id"],
        "package_assigned_at": row["package_assigned_at"],
        "package_due_at": row["package_due_at"],
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


def _ensure_default_service_packages() -> None:
    now = _now_iso()
    defaults = [
        {
            "package_id": "pkg_geo_starter",
            "name": "GEO Starter",
            "tier": "starter",
            "price_cny": 6800,
            "delivery_days": 7,
            "platforms": ["chatgpt", "perplexity"],
            "features": ["诊断", "FAQ", "Schema", "CMS", "发布"],
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
        {
            "package_id": "pkg_geo_growth",
            "name": "GEO Growth Loop",
            "tier": "growth",
            "price_cny": 16800,
            "delivery_days": 21,
            "platforms": ["chatgpt", "perplexity", "gemini", "google_ai_overviews"],
            "features": ["监测", "实验", "Trust Anchor", "CMS", "发布", "复测", "报告"],
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
        {
            "package_id": "pkg_geo_pro",
            "name": "GEO Revenue OS",
            "tier": "pro",
            "price_cny": 32800,
            "delivery_days": 30,
            "platforms": ["chatgpt", "perplexity", "gemini", "google_ai_overviews", "claude", "doubao", "deepseek"],
            "features": ["监测", "实验", "归因", "报告", "Trust Anchor", "CMS", "发布", "复测", "周报"],
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    ]
    for item in defaults:
        _db_save_service_package(item)


def _attribution_summary(attributions: list[dict]) -> dict:
    confirmed = [item for item in attributions if item.get("status") == "confirmed"]
    pending = [item for item in attributions if item.get("status") == "pending_confirmation"]
    revenue = round(sum(float(item.get("attributed_revenue") or 0) for item in confirmed), 2)
    latest = attributions[0] if attributions else None
    return {
        "count": len(attributions),
        "confirmed": len(confirmed),
        "pending": len(pending),
        "revenue": revenue,
        "latest_source": latest.get("source_name") if latest else None,
        "latest_stage": latest.get("lead_stage") if latest else None,
        "needs_confirmation": len(pending) > 0,
    }


def _reporting_summary(reports: list[dict], report_exports: list[dict]) -> dict:
    latest_report = reports[0] if reports else None
    latest_export = report_exports[0] if report_exports else None
    confirmed = [item for item in reports if item.get("status") == "confirmed"]
    shared = [item for item in reports if item.get("share_status") == "shared"]
    return {
        "count": len(reports),
        "confirmed": len(confirmed),
        "shared": len(shared),
        "latest_period": latest_report.get("period_label") if latest_report else None,
        "latest_status": latest_report.get("status") if latest_report else None,
        "latest_export_format": latest_export.get("format") if latest_export else None,
        "latest_export_status": latest_export.get("status") if latest_export else None,
        "latest_share_channel": latest_report.get("share_channel") if latest_report else None,
        "ready_for_share": bool(latest_report and latest_report.get("status") == "confirmed"),
    }


def _connector_recovery_hint(connector: dict) -> str:
    if connector.get("last_error"):
        return f"优先处理最近失败：{connector['last_error']}"
    if connector.get("credential_env_var") and not connector.get("credential_configured"):
        return f"补齐环境变量 {connector['credential_env_var']} 后复核。"
    if connector.get("status") == "failed":
        return "检查凭证、人工审计步骤和证据链接后再重试。"
    if connector.get("freshness") == "stale":
        return "已超过 7 天未校验，建议重新执行采样并更新证据。"
    return "保持固定问法、固定时间窗口，并补人工确认记录。"


def _package_sla_view(task: dict, assigned_package: dict | None, package_delivery: list[dict]) -> dict | None:
    if not assigned_package:
        return None
    assigned_at = _parse_iso_datetime(task.get("package_assigned_at"))
    due_at = _parse_iso_datetime(task.get("package_due_at"))
    done = len([item for item in package_delivery if item.get("status") == "done"])
    blocked = len([item for item in package_delivery if item.get("status") == "blocked"])
    total = len(package_delivery)
    percent = round(done * 100 / total) if total else 0
    now = datetime.now(timezone.utc)
    status = "tracking"
    days_remaining = None
    overdue_days = 0
    if due_at:
        days_remaining = (due_at.date() - now.date()).days
        if total and done >= total:
            status = "completed"
        elif days_remaining < 0:
            status = "overdue"
            overdue_days = abs(days_remaining)
        elif days_remaining <= 3:
            status = "at_risk"
        else:
            status = "on_track"
    if blocked and status not in {"completed", "overdue"}:
        status = "blocked"
    return {
        "assigned_at": assigned_at.isoformat() if assigned_at else None,
        "due_at": due_at.isoformat() if due_at else None,
        "days_remaining": days_remaining,
        "overdue_days": overdue_days,
        "status": status,
        "completion_percent": percent,
        "done": done,
        "blocked": blocked,
        "total": total,
    }


def _experiment_summary(experiments: list[dict], events: list[dict]) -> dict:
    running = [item for item in experiments if item.get("status") == "running"]
    blocked = [item for item in experiments if item.get("status") in {"blocked", "rollback"}]
    observed = [item for item in events if item.get("status") == "observed"]
    latest = events[0] if events else None
    return {
        "count": len(experiments),
        "running": len(running),
        "blocked": len(blocked),
        "observed_events": len(observed),
        "latest_event_status": latest.get("status") if latest else None,
        "latest_event_at": latest.get("created_at") if latest else None,
    }


def _monitor_connector_blueprints(task: dict, connectors: list[dict]) -> list[dict]:
    target_engines = list(dict.fromkeys(task.get("target_engines") or ["chatgpt", "perplexity"]))
    connector_map = {item["platform"]: item for item in connectors}
    defaults = {
        "chatgpt": {
            "provider_name": "OpenAI Responses API",
            "connector_type": "official_api",
            "credential_env_var": "OPENAI_API_KEY",
            "verification_method": "api_response",
            "audit_requirement": "保留请求日志、提示词版本和人工抽样记录。",
        },
        "perplexity": {
            "provider_name": "Perplexity Export Review",
            "connector_type": "manual_export",
            "credential_env_var": "",
            "verification_method": "export_screenshot",
            "audit_requirement": "保留导出回答、Sources 截图和人工核对记录。",
        },
        "gemini": {
            "provider_name": "Gemini Answer Audit",
            "connector_type": "manual_audit",
            "credential_env_var": "",
            "verification_method": "ops_checklist",
            "audit_requirement": "按固定 Query 周期人工复核，保存审计人和时间。",
        },
        "google_ai_overviews": {
            "provider_name": "Google AI Overviews Audit",
            "connector_type": "manual_audit",
            "credential_env_var": "",
            "verification_method": "ops_checklist",
            "audit_requirement": "保留 SERP 截图、地区与时间戳，避免不可复核抓取。",
        },
        "claude": {
            "provider_name": "Claude Ops Review",
            "connector_type": "manual_audit",
            "credential_env_var": "",
            "verification_method": "ops_checklist",
            "audit_requirement": "按提示词模板记录回答与来源，人工复核后入库。",
        },
        "doubao": {
            "provider_name": "Doubao Export Review",
            "connector_type": "manual_export",
            "credential_env_var": "",
            "verification_method": "export_screenshot",
            "audit_requirement": "保存对话导出与截图，注明操作者和版本。",
        },
        "deepseek": {
            "provider_name": "DeepSeek Audit Log",
            "connector_type": "manual_audit",
            "credential_env_var": "",
            "verification_method": "ops_checklist",
            "audit_requirement": "固定问法、固定时间窗口，保存人工核验凭证。",
        },
    }
    blueprints = []
    for platform in target_engines:
        base = defaults.get(
            platform,
            {
                "provider_name": f"{platform} Audit",
                "connector_type": "manual_audit",
                "credential_env_var": "",
                "verification_method": "ops_checklist",
                "audit_requirement": "保留人工可审计记录。",
            },
        )
        current = connector_map.get(platform)
        blueprints.append(
            {
                "platform": platform,
                "provider_name": current.get("provider_name") if current else base["provider_name"],
                "connector_type": current.get("connector_type") if current else base["connector_type"],
                "credential_env_var": current.get("credential_env_var") if current else base["credential_env_var"],
                "verification_method": current.get("verification_method") if current else base["verification_method"],
                "audit_requirement": base["audit_requirement"],
                "status": current.get("status") if current else "missing",
                "connected": bool(current and current.get("status") == "connected"),
                "existing_connector_id": current.get("connector_id") if current else None,
            }
        )
    return blueprints


def _source_ops_summary(source_map: dict, trust_anchors: list[dict], gap_actions: list[dict]) -> dict:
    done_anchors = len([item for item in trust_anchors if item.get("status") == "done"])
    active_anchors = len([item for item in trust_anchors if item.get("status") == "in_progress"])
    blocked_actions = len([item for item in gap_actions if item.get("status") in {"blocked", "rollback"}])
    top_domain = source_map["domains"][0]["domain"] if source_map.get("domains") else None
    top_page_type = source_map["page_types"][0]["page_type"] if source_map.get("page_types") else None
    next_focus = source_map["recommendations"][0]["title"] if source_map.get("recommendations") else None
    return {
        "top_domain": top_domain,
        "top_page_type": top_page_type,
        "trust_anchor_done": done_anchors,
        "trust_anchor_active": active_anchors,
        "blocked_actions": blocked_actions,
        "next_focus": next_focus,
    }


def _recommend_service_package(
    task: dict,
    packages: list[dict],
    monitoring: dict,
    experiments: list[dict],
    attributions: list[dict],
    reports: list[dict],
) -> dict | None:
    if not packages:
        return None
    target_engines = task.get("target_engines") or []
    need_level = "starter"
    reasons = []
    if len(target_engines) >= 2:
        need_level = "growth"
        reasons.append("目标 AI 平台超过 1 个，需要持续监测和复测。")
    if experiments or monitoring.get("active_query_count", 0) >= 6:
        need_level = "growth"
        reasons.append("已进入实验/采样阶段，需要固定运营动作。")
    if attributions or reports or len(target_engines) >= 4:
        need_level = "pro"
        reasons.append("已涉及归因或多平台运营，需要报告和营收视角。")
    preferred_order = {"starter": 0, "growth": 1, "pro": 2}
    target_rank = preferred_order[need_level]
    ranked = sorted(
        packages,
        key=lambda item: (abs(preferred_order.get(item.get("tier"), 1) - target_rank), item.get("price_cny", 0)),
    )
    selected = ranked[0]
    return {
        "package_id": selected["package_id"],
        "name": selected["name"],
        "tier": selected["tier"],
        "price_cny": selected["price_cny"],
        "delivery_days": selected["delivery_days"],
        "reason": reasons or ["当前仍处于基础交付阶段，先用入门套餐启动闭环。"],
        "match_score": 100 - abs(preferred_order.get(selected.get("tier"), 1) - target_rank) * 20,
    }


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
    monitoring = _monitoring_summary(task["task_id"])
    active_packages = _db_service_packages(status="active")
    assigned_package = _db_get_service_package(task.get("package_id")) if task.get("package_id") else None
    experiments = _db_experiments(task["task_id"])
    experiment_events = _db_experiment_events(task_id=task["task_id"], limit=50)
    attributions = _db_attributions(task["task_id"])
    reports = _db_reports(task["task_id"])
    report_exports = _db_report_exports(task_id=task["task_id"], limit=20)
    gap_actions = _db_gap_actions(task["task_id"])
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
    elif reports and reports[0].get("status") == "confirmed" and reports[0].get("share_status") != "shared":
        next_action, next_action_key = "确认报告分发与客户回执", "share_report"
    effect = "尚未复测"
    if latest_retest:
        delta = int(latest_retest.get("score_delta") or 0)
        effect = "有效优化" if delta > 0 else "未见提升"
    todos = task.get("todos") or []
    if not todos:
        todos = [next_action]
        if score < target_score:
            todos.append(f"将 GEO 分数从 {score} 提升到 {target_score}")
    package_delivery = _package_delivery_view(task, assigned_package, monitoring, gap_actions, experiments, attributions, reports)
    package_sla = _package_sla_view(task, assigned_package, package_delivery)
    action_progress = _gap_action_progress(gap_actions)
    experiment_summary = _experiment_summary(experiments, experiment_events)
    attribution_summary = _attribution_summary(attributions)
    reporting_summary = _reporting_summary(reports, report_exports)
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
        "owner": task.get("owner") or "待分配",
        "target_score": target_score,
        "current_stage": status,
        "next_action": next_action,
        "next_action_key": next_action_key,
        "todos": todos,
        "assigned_package": assigned_package,
        "package_id": task.get("package_id"),
        "package_name": assigned_package.get("name") if assigned_package else None,
        "package_assigned_at": task.get("package_assigned_at"),
        "package_due_at": task.get("package_due_at"),
        "package_sla": package_sla,
        "recommended_package": _recommend_service_package(
            task, active_packages, monitoring, experiments, attributions, reports
        ),
        "experiment_count": len(experiments),
        "active_experiment_count": len([item for item in experiments if item.get("status") in {"draft", "running"}]),
        "experiment_summary": experiment_summary,
        "attribution_count": len(attributions),
        "confirmed_lead_count": len([item for item in attributions if item.get("status") == "confirmed"]),
        "report_count": len(reports),
        "attribution_summary": attribution_summary,
        "reporting_summary": reporting_summary,
        "effectiveness": effect,
        "gap_actions": gap_actions,
        "action_progress": action_progress,
        "package_delivery": package_delivery,
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
            "completion_percent": round(sum(readiness_checks) * 100 / 5),
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
            "blockers": [
                "先补齐基础项，再对外承诺套餐 SLA。"
                if not commercial_ready
                else "项目资料齐备，可进入标准交付。"
            ],
        },
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


def _db_monitor_queries(task_id: str | None = None, active_only: bool = False) -> list[dict]:
    query = "SELECT * FROM monitor_queries"
    filters: list[str] = []
    params: list = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if active_only:
        filters.append("active = 1")
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
        }
        for row in rows
    ]


def _db_source_observations(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM source_observations"
    params: list[str] = []
    if task_id:
        query += " WHERE task_id = ?"
        params.append(task_id)
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
        }
        for row in rows
    ]


def _db_trust_anchors(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM trust_anchor_tasks"
    params: list[str] = []
    if task_id:
        query += " WHERE task_id = ?"
        params.append(task_id)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _db_get_trust_anchor(anchor_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM trust_anchor_tasks WHERE anchor_id = ?", (anchor_id,)).fetchone()
    return dict(row) if row else None


def _db_save_trust_anchor(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO trust_anchor_tasks (
                anchor_id, task_id, channel, topic, target_url, owner, status,
                guidance, evidence_url, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(anchor_id) DO UPDATE SET
                channel=excluded.channel,
                topic=excluded.topic,
                target_url=excluded.target_url,
                owner=excluded.owner,
                status=excluded.status,
                guidance=excluded.guidance,
                evidence_url=excluded.evidence_url,
                updated_at=excluded.updated_at
            """,
            tuple(
                item[key]
                for key in [
                    "anchor_id",
                    "task_id",
                    "channel",
                    "topic",
                    "target_url",
                    "owner",
                    "status",
                    "guidance",
                    "evidence_url",
                    "created_at",
                    "updated_at",
                ]
            ),
        )


def _db_mention_checks(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM mention_checks"
    params: list[str] = []
    if task_id:
        query += " WHERE task_id = ?"
        params.append(task_id)
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


def _monitor_connector_from_row(row: sqlite3.Row) -> dict:
    return {
        "connector_id": row["connector_id"],
        "task_id": row["task_id"],
        "platform": row["platform"],
        "connector_type": row["connector_type"],
        "provider_name": row["provider_name"],
        "status": row["status"],
        "credential_env_var": row["credential_env_var"],
        "credential_configured": bool(row["credential_env_var"] and os.getenv(row["credential_env_var"])),
        "evidence_url": row["evidence_url"],
        "verification_method": row["verification_method"],
        "notes": row["notes"],
        "owner": row["owner"],
        "next_check_at": row["next_check_at"],
        "recovery_hint": row["recovery_hint"],
        "last_error": row["last_error"],
        "last_checked_at": row["last_checked_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _db_monitor_connectors(task_id: str | None = None, platform: str | None = None) -> list[dict]:
    query = "SELECT * FROM monitor_connectors"
    filters: list[str] = []
    params: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if platform:
        filters.append("platform = ?")
        params.append(platform.strip().lower())
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_monitor_connector_from_row(row) for row in rows]


def _db_get_monitor_connector(connector_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM monitor_connectors WHERE connector_id = ?", (connector_id,)).fetchone()
    return _monitor_connector_from_row(row) if row else None


def _db_save_monitor_connector(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO monitor_connectors (
                connector_id, task_id, platform, connector_type, provider_name, status,
                credential_env_var, evidence_url, verification_method, notes, owner,
                next_check_at, recovery_hint, last_error, last_checked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connector_id) DO UPDATE SET
                platform=excluded.platform,
                connector_type=excluded.connector_type,
                provider_name=excluded.provider_name,
                status=excluded.status,
                credential_env_var=excluded.credential_env_var,
                evidence_url=excluded.evidence_url,
                verification_method=excluded.verification_method,
                notes=excluded.notes,
                owner=excluded.owner,
                next_check_at=excluded.next_check_at,
                recovery_hint=excluded.recovery_hint,
                last_error=excluded.last_error,
                last_checked_at=excluded.last_checked_at,
                updated_at=excluded.updated_at
            """,
            (
                item["connector_id"],
                item["task_id"],
                item["platform"],
                item["connector_type"],
                item["provider_name"],
                item["status"],
                item.get("credential_env_var"),
                item.get("evidence_url"),
                item["verification_method"],
                item.get("notes"),
                item.get("owner"),
                item.get("next_check_at"),
                item.get("recovery_hint"),
                item.get("last_error"),
                item.get("last_checked_at"),
                item["created_at"],
                item["updated_at"],
            ),
        )


def _connector_run_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _db_connector_runs(connector_id: str | None = None, task_id: str | None = None, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM connector_runs"
    filters: list[str] = []
    params: list[str] = []
    if connector_id:
        filters.append("connector_id = ?")
        params.append(connector_id)
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_connector_run_from_row(row) for row in rows]


def _db_save_connector_run(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO connector_runs (
                run_id, connector_id, task_id, status, notes, evidence_url,
                last_error, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["run_id"],
                item["connector_id"],
                item["task_id"],
                item["status"],
                item.get("notes"),
                item.get("evidence_url"),
                item.get("last_error"),
                item["actor"],
                item["created_at"],
            ),
        )


def _gap_action_from_row(row: sqlite3.Row) -> dict:
    return {
        "action_id": row["action_id"],
        "task_id": row["task_id"],
        "title": row["title"],
        "action_type": row["action_type"],
        "source": row["source"],
        "priority": row["priority"],
        "status": row["status"],
        "owner": row["owner"],
        "related_object_id": row["related_object_id"],
        "notes": row["notes"],
        "evidence_url": row["evidence_url"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def _db_gap_actions(task_id: str | None = None, status: str | None = None) -> list[dict]:
    query = "SELECT * FROM page_gap_actions"
    filters: list[str] = []
    params: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if status:
        filters.append("status = ?")
        params.append(status)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END, updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_gap_action_from_row(row) for row in rows]


def _db_get_gap_action(action_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM page_gap_actions WHERE action_id = ?", (action_id,)).fetchone()
    return _gap_action_from_row(row) if row else None


def _db_save_gap_action(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO page_gap_actions (
                action_id, task_id, title, action_type, source, priority, status,
                owner, related_object_id, notes, evidence_url, created_at, updated_at,
                completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(action_id) DO UPDATE SET
                title=excluded.title,
                action_type=excluded.action_type,
                source=excluded.source,
                priority=excluded.priority,
                status=excluded.status,
                owner=excluded.owner,
                related_object_id=excluded.related_object_id,
                notes=excluded.notes,
                evidence_url=excluded.evidence_url,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at
            """,
            (
                item["action_id"],
                item["task_id"],
                item["title"],
                item["action_type"],
                item["source"],
                item["priority"],
                item["status"],
                item.get("owner"),
                item.get("related_object_id"),
                item.get("notes"),
                item.get("evidence_url"),
                item["created_at"],
                item["updated_at"],
                item.get("completed_at"),
            ),
        )


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
    if not row:
        _ensure_default_service_packages()
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
    if not rows:
        _ensure_default_service_packages()
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


def _experiment_event_from_row(row: sqlite3.Row) -> dict:
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
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_experiment_from_row(row) for row in rows]


def _db_get_experiment(experiment_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM content_experiments WHERE experiment_id = ?", (experiment_id,)).fetchone()
    return _experiment_from_row(row) if row else None


def _db_save_experiment(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO content_experiments (
                experiment_id, task_id, name, hypothesis, channel, primary_metric,
                variant_a, variant_b, status, winner, notes, created_at, updated_at,
                confirmed_by, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                confirmed_at=excluded.confirmed_at
            """,
            tuple(item[key] for key in [
                "experiment_id", "task_id", "name", "hypothesis", "channel", "primary_metric",
                "variant_a", "variant_b", "status", "winner", "notes", "created_at", "updated_at",
                "confirmed_by", "confirmed_at",
            ]),
        )


def _db_experiment_events(experiment_id: str | None = None, task_id: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM experiment_events"
    filters: list[str] = []
    params: list[str | int] = []
    if experiment_id:
        filters.append("experiment_id = ?")
        params.append(experiment_id)
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_experiment_event_from_row(row) for row in rows]


def _db_save_experiment_event(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO experiment_events (
                event_id, experiment_id, task_id, status, notes, sample_size,
                metric_value, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["event_id"],
                item["experiment_id"],
                item["task_id"],
                item["status"],
                item.get("notes"),
                item.get("sample_size"),
                item.get("metric_value"),
                item["actor"],
                item["created_at"],
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
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_attribution_from_row(row) for row in rows]


def _db_get_attribution(attribution_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM lead_attributions WHERE attribution_id = ?", (attribution_id,)).fetchone()
    return _attribution_from_row(row) if row else None


def _db_save_attribution(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO lead_attributions (
                attribution_id, task_id, source_type, source_name, session_ref,
                lead_stage, attributed_revenue, evidence_url, status, notes, actor,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attribution_id) DO UPDATE SET
                source_type=excluded.source_type,
                source_name=excluded.source_name,
                session_ref=excluded.session_ref,
                lead_stage=excluded.lead_stage,
                attributed_revenue=excluded.attributed_revenue,
                evidence_url=excluded.evidence_url,
                status=excluded.status,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            tuple(item[key] for key in [
                "attribution_id", "task_id", "source_type", "source_name", "session_ref",
                "lead_stage", "attributed_revenue", "evidence_url", "status", "notes", "actor",
                "created_at", "updated_at",
            ]),
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
        "share_status": row["share_status"],
        "share_channel": row["share_channel"],
        "shared_at": row["shared_at"],
        "share_notes": row["share_notes"],
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
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY generated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_report_from_row(row) for row in rows]


def _db_get_report(report_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM effect_reports WHERE report_id = ?", (report_id,)).fetchone()
    return _report_from_row(row) if row else None


def _db_save_report(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO effect_reports (
                report_id, task_id, period_label, status, summary, metrics, findings,
                next_actions, generated_at, confirmed_by, confirmed_at, notes,
                share_status, share_channel, shared_at, share_notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                share_status=excluded.share_status,
                share_channel=excluded.share_channel,
                shared_at=excluded.shared_at,
                share_notes=excluded.share_notes
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
                item.get("share_status"),
                item.get("share_channel"),
                item.get("shared_at"),
                item.get("share_notes"),
            ),
        )


def _report_export_from_row(row: sqlite3.Row) -> dict:
    return {
        **dict(row),
        "is_external": bool(row["external_url"]),
    }


def _package_delivery_update_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _feature_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "_", (value or "").strip().lower())
    return normalized.strip("_")[:60] or "feature"


def _db_package_delivery_updates(task_id: str) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM package_delivery_updates WHERE task_id = ? ORDER BY updated_at DESC",
            (task_id,),
        ).fetchall()
    return [_package_delivery_update_from_row(row) for row in rows]


def _db_get_package_delivery_update(task_id: str, feature_key: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM package_delivery_updates WHERE task_id = ? AND feature_key = ?",
            (task_id, feature_key),
        ).fetchone()
    return _package_delivery_update_from_row(row) if row else None


def _db_save_package_delivery_update(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO package_delivery_updates (
                update_id, task_id, feature_key, feature_name, status, notes, actor,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(update_id) DO UPDATE SET
                status=excluded.status,
                notes=excluded.notes,
                actor=excluded.actor,
                updated_at=excluded.updated_at
            """,
            (
                item["update_id"],
                item["task_id"],
                item["feature_key"],
                item["feature_name"],
                item["status"],
                item.get("notes"),
                item["actor"],
                item["created_at"],
                item["updated_at"],
            ),
        )


def _db_report_exports(task_id: str | None = None, report_id: str | None = None, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM report_exports"
    filters: list[str] = []
    params: list[str] = []
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if report_id:
        filters.append("report_id = ?")
        params.append(report_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_report_export_from_row(row) for row in rows]


def _db_save_report_export(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO report_exports (
                export_id, report_id, task_id, project_name, title, format,
                filepath, external_url, external_id, status, note, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["export_id"],
                item.get("report_id"),
                item.get("task_id"),
                item.get("project_name"),
                item["title"],
                item["format"],
                item.get("filepath"),
                item.get("external_url"),
                item.get("external_id"),
                item["status"],
                item.get("note"),
                item["actor"],
                item["created_at"],
            ),
        )


def _gap_action_progress(actions: list[dict]) -> dict:
    total = len(actions)
    done = len([item for item in actions if item.get("status") == "done"])
    active = len([item for item in actions if item.get("status") == "in_progress"])
    blocked = len([item for item in actions if item.get("status") in {"rollback", "blocked"}])
    return {
        "total": total,
        "done": done,
        "active": active,
        "blocked": blocked,
        "completion_percent": round(done * 100 / total) if total else 0,
    }


def _package_delivery_view(
    task: dict,
    assigned_package: dict | None,
    monitoring: dict,
    gap_actions: list[dict],
    experiments: list[dict],
    attributions: list[dict],
    reports: list[dict],
) -> list[dict]:
    if not assigned_package:
        return []
    manual_updates = {
        item["feature_key"]: item
        for item in _db_package_delivery_updates(task["task_id"])
    }
    feature_state = {
        "监测": monitoring["check_count"] > 0,
        "实验": bool(experiments),
        "归因": bool(attributions),
        "报告": bool(reports),
        "CMS": bool(task.get("latest_result")) and bool(task.get("package_id")),
        "发布": task.get("status") in {"approved", "injected", "retested"},
        "复测": task.get("status") == "retested",
    }
    active_action_titles = {item["title"] for item in gap_actions if item.get("status") != "done"}
    checklist = []
    for feature in assigned_package.get("features") or []:
        feature_key = _feature_key(feature)
        manual = manual_updates.get(feature_key)
        matched = next((value for key, value in feature_state.items() if key in feature or feature in key), False)
        status = manual["status"] if manual else ("done" if matched else "pending")
        checklist.append(
            {
                "feature": feature,
                "feature_key": feature_key,
                "status": status,
                "source": "manual" if manual else "derived",
                "notes": manual.get("notes") if manual else None,
                "actor": manual.get("actor") if manual else None,
                "updated_at": manual.get("updated_at") if manual else None,
                "next_hint": None if status == "done" else ("已有待办跟进" if active_action_titles else "建议创建执行动作"),
            }
        )
    return checklist


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
        "dominant_domain": domains[0]["domain"] if domains else None,
        "dominant_page_type": page_types[0]["page_type"] if page_types else None,
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
        return "transport_pass"
    if any(token in path for token in ["things-to-do", "activity", "experience"]):
        return "local_activity"
    if any(token in path for token in ["ticket", "attraction"]):
        return "attraction_ticket"
    if any(token in path for token in ["guide", "travel", "itinerary"]):
        return "destination_guide"
    if any(token in path for token in ["campaign", "promo", "sale", "deal"]):
        return "campaign_landing"
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
                            sample_target, language, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            }
            conn.execute(
                """INSERT INTO source_observations (
                    observation_id, task_id, query_id, source_domain, source_url, page_type,
                    citation_count, notes, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    connectors = _db_monitor_connectors(task_id)
    connector_runs = _db_connector_runs(task_id=task_id, limit=80)
    observations = _db_source_observations(task_id)
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
    connector_status_counts: dict[str, int] = {}
    for item in connectors:
        connector_status_counts[item["status"]] = connector_status_counts.get(item["status"], 0) + 1
    runs_by_connector: dict[str, list[dict]] = {}
    for item in connector_runs:
        runs_by_connector.setdefault(item["connector_id"], []).append(item)
    fresh_connectors = 0
    stale_connectors = 0
    missing_credentials = 0
    for item in connectors:
        item["recent_runs"] = runs_by_connector.get(item["connector_id"], [])[:5]
        item["latest_run"] = item["recent_runs"][0] if item["recent_runs"] else None
        freshness = "never_checked"
        checked_at = item.get("last_checked_at") or (item["latest_run"] or {}).get("created_at")
        if checked_at:
            last_checked = _parse_iso_datetime(checked_at)
            if last_checked:
                age_days = max(0, (datetime.now(timezone.utc) - last_checked).days)
                freshness = "fresh" if age_days <= 7 else "stale"
                item["age_days"] = age_days
            else:
                item["age_days"] = None
        else:
            item["age_days"] = None
        item["freshness"] = freshness
        if freshness == "fresh":
            fresh_connectors += 1
        elif freshness == "stale":
            stale_connectors += 1
        if item.get("credential_env_var") and not item.get("credential_configured"):
            missing_credentials += 1
        item["recovery_hint"] = item.get("recovery_hint") or _connector_recovery_hint(item)
        next_check = _parse_iso_datetime(item.get("next_check_at"))
        item["next_check_due"] = bool(next_check and next_check <= datetime.now(timezone.utc))
    task = _db_get_task(task_id) or {"task_id": task_id, "target_engines": []}
    source_map = _source_map(task_id)
    trust_anchors = _db_trust_anchors(task_id)
    gap_actions = _db_gap_actions(task_id)
    connector_blueprints = _monitor_connector_blueprints(task, connectors)
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
        "connectors": connectors,
        "connector_runs": connector_runs,
        "connector_status": {
            "count": len(connectors),
            "connected": connector_status_counts.get("connected", 0),
            "failed": connector_status_counts.get("failed", 0),
            "planned": connector_status_counts.get("planned", 0),
            "auditable": len([item for item in connectors if item["connector_type"] in {"official_api", "manual_export", "manual_audit"}]),
            "fresh": fresh_connectors,
            "stale": stale_connectors,
            "missing_credentials": missing_credentials,
        },
        "connector_blueprints": connector_blueprints,
        "connector_plan": {
            "target_platforms": len(connector_blueprints),
            "connected": len([item for item in connector_blueprints if item["connected"]]),
            "missing": len([item for item in connector_blueprints if item["status"] == "missing"]),
            "next_platform": next((item["platform"] for item in connector_blueprints if item["status"] != "connected"), None),
            "stale_platforms": [item["platform"] for item in connectors if item.get("freshness") == "stale"],
            "action_required": len([item for item in connectors if item.get("freshness") == "stale" or (item.get("credential_env_var") and not item.get("credential_configured"))]),
        },
        "source_observations": observations,
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
        "source_map": source_map,
        "trust_anchors": trust_anchors,
        "source_ops_summary": _source_ops_summary(source_map, trust_anchors, gap_actions),
    }


def _build_effect_report(task: dict, period_label: str, notes: str | None = None) -> dict:
    task_id = task["task_id"]
    monitoring = _monitoring_summary(task_id)
    retests = _db_history()["retests"].get(task_id, [])
    attributions = _db_attributions(task_id)
    experiments = _db_experiments(task_id)
    reports = _db_reports(task_id)
    gap_actions = _db_gap_actions(task_id)
    confirmed_attributions = [item for item in attributions if item.get("status") == "confirmed"]
    total_revenue = round(sum(float(item.get("attributed_revenue") or 0) for item in confirmed_attributions), 2)
    won_experiments = [item for item in experiments if item.get("status") == "won"]
    latest_retest = retests[0] if retests else task.get("latest_retest") or {}
    delta = int(latest_retest.get("score_delta") or 0)
    findings = [
        f"AI 平台品牌提及率 {monitoring['mention_rate']}%，累计监测 {monitoring['check_count']} 次。",
        f"官网引用率 {monitoring['citation_rate']}%，采样可信度 {monitoring['sampling']['confidence_level']}。",
        f"已接入 {monitoring['connector_status']['count']} 个监测连接，已连通 {monitoring['connector_status']['connected']} 个。",
        f"已确认线索 {len(confirmed_attributions)} 条，归因收入 {total_revenue:.2f}。",
        f"内容实验完结 {len(won_experiments)} 个，当前 GEO 分数变化 {delta:+d}。",
    ]
    source_actions = monitoring["source_map"]["recommendations"][:2]
    action_progress = _gap_action_progress(gap_actions)
    next_actions = [
        "补录待确认线索的证据链接并完成人工确认。",
        "将赢面实验的内容结构同步到主站和 CMS 模板。",
        *(item["title"] for item in source_actions),
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
            "connected_connectors": monitoring["connector_status"]["connected"],
            "gap_action_completion": action_progress["completion_percent"],
        },
        "findings": findings,
        "next_actions": next_actions,
        "generated_at": _now_iso(),
        "confirmed_by": None,
        "confirmed_at": None,
        "notes": notes,
        "share_status": "pending_share",
        "share_channel": None,
        "shared_at": None,
        "share_notes": None,
    }


def _upsert_gap_action_seed(
    task_id: str,
    title: str,
    action_type: str,
    source: str,
    priority: str,
    related_object_id: str | None = None,
    notes: str | None = None,
) -> dict:
    action_id = f"act_{hashlib.sha256(f'{task_id}:{action_type}:{title}'.encode()).hexdigest()[:12]}"
    now = _now_iso()
    existing = _db_get_gap_action(action_id)
    item = {
        "action_id": action_id,
        "task_id": task_id,
        "title": title.strip(),
        "action_type": action_type,
        "source": source,
        "priority": priority,
        "status": existing["status"] if existing else "accepted",
        "owner": existing["owner"] if existing else None,
        "related_object_id": related_object_id,
        "notes": notes if notes is not None else (existing["notes"] if existing else None),
        "evidence_url": existing["evidence_url"] if existing else None,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        "completed_at": existing["completed_at"] if existing else None,
    }
    _db_save_gap_action(item)
    return item


def _bootstrap_gap_actions(task_id: str) -> list[dict]:
    task = _db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Project not found.")
    monitoring = _monitoring_summary(task_id)
    for item in monitoring["source_map"]["recommendations"][:3]:
        _upsert_gap_action_seed(
            task_id,
            item["title"],
            "source_map",
            "source_map",
            "P1",
            related_object_id=item["page_type"],
            notes=item["action"],
        )
    platforms = set(task.get("target_engines") or [])
    connector_platforms = {item["platform"] for item in monitoring["connectors"]}
    for platform in sorted(platforms - connector_platforms):
        _upsert_gap_action_seed(
            task_id,
            f"补齐 {platform} 可审计监测接入",
            "connector_setup",
            "monitoring_connector",
            "P0",
            related_object_id=platform,
            notes="优先使用官方 API、官方导出或人工可复核记录，避免不可审计采集。",
        )
    if not _db_reports(task_id):
        _upsert_gap_action_seed(
            task_id,
            "生成首期 GEO 效果报告",
            "effect_report",
            "reporting",
            "P1",
            notes="补齐提及率、引用率、归因收入与后续动作。",
        )
    latest_publication = _db_publications(task_id)[:1]
    if latest_publication and latest_publication[0].get("live_status") == "verification_failed":
        _upsert_gap_action_seed(
            task_id,
            "修复 CMS 发布校验失败",
            "cms_recovery",
            "cms",
            "P0",
            related_object_id=latest_publication[0]["publication_id"],
            notes="检查线上内容缺词、字段映射或凭证异常，再重新校验。",
        )
    return _db_gap_actions(task_id)


def _seed_monitor_queries(task_id: str, brand_name: str, engines: list[str]) -> None:
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
                        active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (query_id, task_id, query_text, category, None, normalized_engine, now, now),
                )


def _db_save_publication(publication: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO publications (
                publication_id, task_id, version_id, target_id, status, preview,
                quality_report, injection_id, confirmed_by, confirmed_at,
                response_summary, live_status, live_summary, live_confirmed_by,
                live_confirmed_at, rollback_status, rollback_note, rollback_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                rollback_status=excluded.rollback_status,
                rollback_note=excluded.rollback_note,
                rollback_at=excluded.rollback_at,
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
                publication.get("live_confirmed_at"),
                publication.get("rollback_status"),
                publication.get("rollback_note"),
                publication.get("rollback_at"),
                publication["created_at"],
                publication["updated_at"],
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
            "rollback_status": row["rollback_status"], "rollback_note": row["rollback_note"],
            "rollback_at": row["rollback_at"],
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
        "rollback_status": row["rollback_status"],
        "rollback_note": row["rollback_note"],
        "rollback_at": row["rollback_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _db_publication_events(publication_id: str | None = None, task_id: str | None = None, limit: int = 100) -> list[dict]:
    query = "SELECT * FROM publication_events"
    filters: list[str] = []
    params: list[str | int] = []
    if publication_id:
        filters.append("publication_id = ?")
        params.append(publication_id)
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _db_save_publication_event(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO publication_events (
                event_id, publication_id, task_id, status, notes, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["event_id"],
                item["publication_id"],
                item["task_id"],
                item["status"],
                item.get("notes"),
                item["actor"],
                item["created_at"],
            ),
        )


def _record_publication_event(publication: dict, status: str, notes: str | None = None) -> None:
    _db_save_publication_event(
        {
            "event_id": f"pe_{uuid.uuid4().hex[:12]}",
            "publication_id": publication["publication_id"],
            "task_id": publication["task_id"],
            "status": status,
            "notes": notes,
            "actor": _current_actor(),
            "created_at": _now_iso(),
        }
    )
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


def _article_from_row(row: sqlite3.Row) -> dict:
    return {
        "article_id": row["article_id"],
        "task_id": row["task_id"],
        "title": row["title"],
        "status": row["status"],
        "markdown_path": row["markdown_path"],
        "feishu_url": row["feishu_url"],
        "feishu_token": row["feishu_token"],
        "feishu_response": _json_loads(row["feishu_response"], None),
        "feishu_status": row["feishu_status"] or ("synced" if row["feishu_url"] else "not_synced"),
        "last_feishu_sync_at": row["last_feishu_sync_at"],
        "public_url": row["public_url"],
        "index_status": row["index_status"] or "draft",
        "indexing_notes": row["indexing_notes"],
        "indexing_plan": _json_loads(row["indexing_plan"], []),
        "indexed_at": row["indexed_at"],
        "last_index_checked_at": row["last_index_checked_at"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _article_index_event_from_row(row: sqlite3.Row) -> dict:
    return dict(row)


def _db_article_index_events(article_id: str | None = None, task_id: str | None = None, limit: int = 50) -> list[dict]:
    query = "SELECT * FROM article_index_events"
    filters: list[str] = []
    params: list[str] = []
    if article_id:
        filters.append("article_id = ?")
        params.append(article_id)
    if task_id:
        filters.append("task_id = ?")
        params.append(task_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(limit, 200)))
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_article_index_event_from_row(row) for row in rows]


def _db_save_article_index_event(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO article_index_events (
                event_id, article_id, task_id, index_status, public_url, notes,
                actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["event_id"],
                item["article_id"],
                item["task_id"],
                item["index_status"],
                item.get("public_url"),
                item.get("notes"),
                item["actor"],
                item["created_at"],
            ),
        )


def _db_get_article(article_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM geo_articles WHERE article_id = ?", (article_id,)).fetchone()
    return _article_from_row(row) if row else None


def _db_articles(task_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM geo_articles"
    params: list[str] = []
    if task_id:
        query += " WHERE task_id = ?"
        params.append(task_id)
    query += " ORDER BY updated_at DESC"
    with _db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_article_from_row(row) for row in rows]


def _db_save_article(item: dict) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO geo_articles (
                article_id, task_id, title, status, markdown_path, feishu_url,
                feishu_token, feishu_response, feishu_status, last_feishu_sync_at, public_url, index_status,
                indexing_notes, indexing_plan, indexed_at, last_index_checked_at,
                error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                title=excluded.title,
                status=excluded.status,
                markdown_path=excluded.markdown_path,
                feishu_url=excluded.feishu_url,
                feishu_token=excluded.feishu_token,
                feishu_response=excluded.feishu_response,
                feishu_status=excluded.feishu_status,
                last_feishu_sync_at=excluded.last_feishu_sync_at,
                public_url=excluded.public_url,
                index_status=excluded.index_status,
                indexing_notes=excluded.indexing_notes,
                indexing_plan=excluded.indexing_plan,
                indexed_at=excluded.indexed_at,
                last_index_checked_at=excluded.last_index_checked_at,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                item["article_id"],
                item["task_id"],
                item["title"],
                item["status"],
                item.get("markdown_path"),
                item.get("feishu_url"),
                item.get("feishu_token"),
                _json_dumps(item.get("feishu_response")) if item.get("feishu_response") else None,
                item.get("feishu_status"),
                item.get("last_feishu_sync_at"),
                item.get("public_url"),
                item.get("index_status") or "draft",
                item.get("indexing_notes"),
                _json_dumps(item.get("indexing_plan")) if item.get("indexing_plan") is not None else None,
                item.get("indexed_at"),
                item.get("last_index_checked_at"),
                item.get("error"),
                item["created_at"],
                item["updated_at"],
            ),
        )


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
        "monitor_queries": _db_monitor_queries(),
        "source_observations": _db_source_observations(),
        "trust_anchors": _db_trust_anchors(),
        "mention_checks": _db_mention_checks(),
        "service_packages": _db_service_packages(),
        "experiments": _db_experiments(),
        "attributions": _db_attributions(),
        "reports": _db_reports(),
        "articles": _db_articles(),
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
_ensure_default_service_packages()


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


def _extract_html_title_text(url: str, html: str) -> tuple[str, str]:
    parser = _ReadableTextParser()
    parser.feed(html)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else url
    return title, parser.text[:12000]


def _fetch_page_text_with_urlopen(url: str) -> tuple[str, str]:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh-HK;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        },
    )
    with urlopen(request, timeout=14) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(charset, errors="ignore")
    return _extract_html_title_text(url, html)


def _fetch_page_text_with_curl(url: str) -> tuple[str, str]:
    curl = shutil.which("curl")
    if not curl:
        raise URLError("curl fallback is unavailable.")
    completed = subprocess.run(
        [
            curl,
            "--location",
            "--silent",
            "--show-error",
            "--compressed",
            "--http1.1",
            "--retry",
            "2",
            "--retry-delay",
            "1",
            "--max-time",
            "18",
            "--user-agent",
            (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "--header",
            "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "--header",
            "Accept-Language: zh-CN,zh-HK;q=0.9,en;q=0.8",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=22,
    )
    if completed.returncode != 0:
        raise URLError((completed.stderr or completed.stdout or "curl fetch failed").strip())
    return _extract_html_title_text(url, completed.stdout)


def _fetch_page_text(url: str) -> tuple[str, str]:
    errors: list[str] = []
    for fetcher in (_fetch_page_text_with_urlopen, _fetch_page_text_with_curl):
        try:
            title, text = fetcher(url)
            if text.strip():
                return title, text
            errors.append("fetched page has no readable text")
        except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError, subprocess.SubprocessError) as exc:
            errors.append(str(exc))
    raise URLError("; ".join(errors) or "failed to fetch page")


def _fetch_error_detail(exc: Exception) -> str:
    raw = str(exc)
    if "EOF occurred in violation of protocol" in raw or "SSL" in raw or "_ssl.c" in raw:
        return (
            "目标页面的 HTTPS/TLS 握手被站点或 CDN 中断，后端已尝试浏览器 UA 和 curl 兼容模式仍失败。"
            "请换一个可公开抓取的 URL，或切到“文案分析”粘贴页面正文继续。"
        )
    if "timed out" in raw.lower() or "timeout" in raw.lower():
        return "目标页面抓取超时。请稍后重试，或切到“文案分析”粘贴页面正文继续。"
    if "403" in raw or "Forbidden" in raw:
        return "目标页面拒绝后端抓取。请使用可公开访问页面，或切到“文案分析”粘贴页面正文继续。"
    return f"目标页面抓取失败：{raw}。可切到“文案分析”粘贴页面正文继续。"


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


def _page_type_template(page_type: str, primary_entity: str, market: str, target_users: list[str]) -> dict:
    normalized = (page_type or "").strip().lower()
    mapping = {
        "transport_pass": {
            "search_intents": ["route_coverage", "eligibility", "price_compare", "how_to_redeem", "worth_it"],
            "gaps": ["补充线路覆盖图、是否适合多城行程和兑换步骤。"] ,
            "modules": [
                {
                    "module_type": "coverage_matrix",
                    "title": f"{primary_entity} 覆盖范围与适用线路",
                    "body": f"按 {market} 旅客最常走的城市对，说明 {primary_entity} 覆盖范围、适用人群和不适用场景。",
                    "target_position": "after ai summary",
                    "priority": "high",
                },
                {
                    "module_type": "route_selector",
                    "title": "按行程选择合适方案",
                    "body": "用 1-3 日、跨城、亲子、首次到访等场景给出推荐路径和排除条件。",
                    "target_position": "before product cards",
                    "priority": "high",
                },
            ],
            "faq": [
                {
                    "question": f"{primary_entity} 适合哪些行程？",
                    "answer": "适合跨城移动频繁、需要提前比较覆盖范围和兑换规则的旅客；单点短途通常要先比较单次票价。",
                    "source_type": "generated",
                    "priority": "high",
                }
            ],
            "conversion_tips": ["在首屏直接给出适合/不适合人群和兑换前检查项。"],
        },
        "attraction_ticket": {
            "search_intents": ["opening_hours", "best_time", "ticket_compare", "family_fit", "refund_policy"],
            "gaps": ["补充游玩时长、预约要求、入场限制和适合人群。"] ,
            "modules": [
                {
                    "module_type": "visit_planner",
                    "title": f"{primary_entity} 游玩决策清单",
                    "body": "说明推荐停留时长、最佳到访时间、是否需要预约，以及家庭/情侣/首次游客的选择建议。",
                    "target_position": "after hero",
                    "priority": "high",
                }
            ],
            "faq": [
                {
                    "question": f"{primary_entity} 值得提前预订吗？",
                    "answer": "如果存在预约名额、旺季排队或家庭同行需求，应优先比较入场时间、退改条件和现场购票风险。",
                    "source_type": "generated",
                    "priority": "high",
                }
            ],
            "conversion_tips": ["把游玩时长、最佳拍照时段和入场限制放到价格上方。"],
        },
        "local_activity": {
            "search_intents": ["experience_fit", "duration", "meeting_point", "what_to_expect", "weather_risk"],
            "gaps": ["补充集合点、天气风险、体验流程和适合人群。"] ,
            "modules": [
                {
                    "module_type": "experience_flow",
                    "title": "活动流程与出发前准备",
                    "body": "按集合、体验、结束三个阶段解释需要携带什么、遇到天气变化怎么办，以及常见取消边界。",
                    "target_position": "after hero",
                    "priority": "high",
                }
            ],
            "faq": [
                {
                    "question": "这个活动更适合哪类旅客？",
                    "answer": f"优先服务 {', '.join(target_users[:2]) if target_users else '重视体验细节和安排确定性的旅客'}，需要提前说明体力要求、集合方式和取消条件。",
                    "source_type": "generated",
                    "priority": "medium",
                }
            ],
            "conversion_tips": ["把集合点地图、天气说明和取消边界做成短块，方便 AI 摘要。"],
        },
        "destination_guide": {
            "search_intents": ["first_time_plan", "where_to_stay", "how_many_days", "seasonality", "transport_mix"],
            "gaps": ["补充 1 日 / 3 日路线、季节差异和交通组合建议。"] ,
            "modules": [
                {
                    "module_type": "itinerary_blocks",
                    "title": f"{market} 旅客常用行程模板",
                    "body": "按首次到访、亲子和深度游三类场景给出行程块、交通衔接和预算边界。",
                    "target_position": "after ai summary",
                    "priority": "high",
                }
            ],
            "faq": [
                {
                    "question": f"第一次去 {market} 应该怎么安排？",
                    "answer": "先给出天数建议、核心区域分布、旺季注意事项和交通换乘逻辑，再引导到具体产品页或专题页。",
                    "source_type": "generated",
                    "priority": "high",
                }
            ],
            "conversion_tips": ["让结论先行，并给出不同天数与预算的线路分支。"],
        },
        "campaign_landing": {
            "search_intents": ["campaign_offer", "limited_time", "eligibility", "claim_steps", "comparison"],
            "gaps": ["补充活动截止时间、适用条件、领取路径和失败恢复说明。"] ,
            "modules": [
                {
                    "module_type": "campaign_rules",
                    "title": "活动规则与领取路径",
                    "body": "把有效期、适用条件、领取步骤、不可叠加说明和客服恢复路径放到同一块。",
                    "target_position": "below hero",
                    "priority": "high",
                }
            ],
            "faq": [
                {
                    "question": "活动失败或未生效时怎么处理？",
                    "answer": "需要提供可验证的规则、人工核验入口和恢复路径，避免只写模糊营销话术。",
                    "source_type": "generated",
                    "priority": "medium",
                }
            ],
            "conversion_tips": ["把截止时间和适用条件与 CTA 放在同一屏。"],
        },
    }
    return mapping.get(normalized, {"search_intents": [], "gaps": [], "modules": [], "faq": [], "conversion_tips": []})


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
    page_type_template = _page_type_template(request.page_type, primary_entity, request.market, target_users)
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
    search_intents = ["comparison", "price", "how-to", "booking", "eligibility"] + (page_type_template.get("search_intents") or [])
    search_intents = list(dict.fromkeys(search_intents))
    content_gaps = score_result.get("recommendations") or [
        "Add an AI-readable summary near the top of the page.",
        "Add FAQ answers for high-intent user questions.",
        "Add comparison and proof points for purchase confidence.",
    ]
    content_gaps.extend(page_type_template.get("gaps") or [])
    content_gaps = list(dict.fromkeys(content_gaps))

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
    injection_modules.extend(page_type_template.get("modules") or [])

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
    faq_items.extend(page_type_template.get("faq") or [])

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
    conversion_tips.extend(page_type_template.get("conversion_tips") or [])
    conversion_tips = list(dict.fromkeys(conversion_tips))

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
    except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=_fetch_error_detail(exc)) from exc

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
    except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=_fetch_error_detail(exc)) from exc

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
        "client_name": request.client_name or existing_task.get("client_name"),
        "brand_name": request.brand_name or existing_task.get("brand_name") or title,
        "target_engines": request.target_engines or existing_task.get("target_engines") or ["chatgpt", "perplexity"],
        "business_goal": request.business_goal or existing_task.get("business_goal"),
        "service_tier": existing_task.get("service_tier") or "growth",
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
    except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=_fetch_error_detail(exc)) from exc

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
        previous_package_id = task.get("package_id")
        package_id = request.package_id.strip() or None
        if package_id and not _db_get_service_package(package_id):
            raise HTTPException(status_code=404, detail="Service package not found.")
        task["package_id"] = package_id
        if package_id != previous_package_id:
            if package_id:
                assigned_package = _db_get_service_package(package_id)
                task["package_assigned_at"] = _now_iso()
                task["package_due_at"] = _iso_after_days(int((assigned_package or {}).get("delivery_days") or 14))
            else:
                task["package_assigned_at"] = None
                task["package_due_at"] = None
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


@app.post("/geo/projects/{task_id}/package-delivery")
def geo_project_package_delivery_update(task_id: str, request: GEOPackageDeliveryUpdateRequest):
    task = _db_get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Project not found.")
    feature_key = _feature_key(request.feature_key)
    assigned_package = _db_get_service_package(task.get("package_id")) if task.get("package_id") else None
    if not assigned_package:
        raise HTTPException(status_code=409, detail="Project has no assigned service package.")
    feature_name = next(
        (item for item in assigned_package.get("features") or [] if _feature_key(item) == feature_key),
        None,
    )
    if not feature_name:
        raise HTTPException(status_code=404, detail="Feature not found in assigned package.")
    now = _now_iso()
    existing = _db_get_package_delivery_update(task_id, feature_key)
    item = {
        "update_id": existing["update_id"] if existing else f"pkgupd_{uuid.uuid4().hex[:12]}",
        "task_id": task_id,
        "feature_key": feature_key,
        "feature_name": feature_name,
        "status": request.status.strip().lower() or "pending",
        "notes": (request.notes or "").strip() or None,
        "actor": _current_actor(),
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    _db_save_package_delivery_update(item)
    _db_add_audit(
        _current_actor(),
        "update_package_delivery",
        "package_delivery",
        item["update_id"],
        task_id,
        outcome=item["status"],
        detail={"feature_key": feature_key},
    )
    return item


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
    _db_save_experiment_event(
        {
            "event_id": f"expev_{uuid.uuid4().hex[:12]}",
            "experiment_id": item["experiment_id"],
            "task_id": request.task_id,
            "status": item["status"],
            "notes": item.get("notes"),
            "sample_size": None,
            "metric_value": None,
            "actor": _current_actor(),
            "created_at": now,
        }
    )
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
    _db_save_experiment_event(
        {
            "event_id": f"expev_{uuid.uuid4().hex[:12]}",
            "experiment_id": experiment_id,
            "task_id": item["task_id"],
            "status": item["status"],
            "notes": item.get("notes"),
            "sample_size": None,
            "metric_value": None,
            "actor": _current_actor(),
            "created_at": item["confirmed_at"],
        }
    )
    _db_add_audit(_current_actor(), "confirm_experiment", "experiment", experiment_id, item["task_id"], outcome=item["status"])
    return item


@app.post("/geo/experiments/{experiment_id}/events")
def geo_experiment_event_save(experiment_id: str, request: GEOExperimentEventRequest):
    item = _db_get_experiment(experiment_id)
    if not item:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    now = _now_iso()
    event = {
        "event_id": f"expev_{uuid.uuid4().hex[:12]}",
        "experiment_id": experiment_id,
        "task_id": item["task_id"],
        "status": request.status.strip().lower() or item["status"],
        "notes": (request.notes or "").strip() or None,
        "sample_size": max(0, int(request.sample_size)) if request.sample_size is not None else None,
        "metric_value": round(float(request.metric_value), 4) if request.metric_value is not None else None,
        "actor": _current_actor(),
        "created_at": now,
    }
    _db_save_experiment_event(event)
    item["status"] = event["status"]
    if event["notes"]:
        item["notes"] = event["notes"]
    item["updated_at"] = now
    _db_save_experiment(item)
    _db_add_audit(
        _current_actor(),
        "save_experiment_event",
        "experiment_event",
        event["event_id"],
        item["task_id"],
        outcome=event["status"],
        detail={"sample_size": event["sample_size"], "metric_value": event["metric_value"]},
    )
    return event


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


@app.patch("/geo/attributions/{attribution_id}")
def geo_attribution_update(attribution_id: str, request: GEOAttributionUpdateRequest):
    item = _db_get_attribution(attribution_id)
    if not item:
        raise HTTPException(status_code=404, detail="Attribution not found.")
    if request.source_type is not None:
        source_type = request.source_type.strip()
        if not source_type:
            raise HTTPException(status_code=400, detail="Source type cannot be empty.")
        item["source_type"] = source_type
    if request.source_name is not None:
        source_name = request.source_name.strip()
        if not source_name:
            raise HTTPException(status_code=400, detail="Source name cannot be empty.")
        item["source_name"] = source_name
    if request.session_ref is not None:
        item["session_ref"] = request.session_ref.strip() or None
    if request.lead_stage is not None:
        item["lead_stage"] = request.lead_stage.strip() or item["lead_stage"]
    if request.attributed_revenue is not None:
        item["attributed_revenue"] = round(float(request.attributed_revenue), 2)
    if request.evidence_url is not None:
        item["evidence_url"] = request.evidence_url.strip() or None
    if request.status is not None:
        item["status"] = request.status.strip() or item["status"]
    if request.notes is not None:
        item["notes"] = request.notes.strip() or None
    item["updated_at"] = _now_iso()
    _db_save_attribution(item)
    _db_add_audit(
        _current_actor(),
        "update_attribution",
        "attribution",
        attribution_id,
        item["task_id"],
        outcome=item["status"],
    )
    return _db_get_attribution(attribution_id)


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


@app.post("/geo/reports/{report_id}/share")
def geo_report_share(report_id: str, request: GEOReportShareRequest):
    report = _db_get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")
    if report["status"] != "confirmed":
        raise HTTPException(status_code=409, detail="Only confirmed reports can be marked as shared.")
    report["share_status"] = request.share_status.strip().lower() or "shared"
    report["share_channel"] = request.share_channel.strip()
    report["shared_at"] = _now_iso()
    report["share_notes"] = (request.notes or "").strip() or None
    _db_save_report(report)
    _db_add_audit(
        _current_actor(),
        "share_report",
        "report",
        report_id,
        report["task_id"],
        outcome=report["share_status"],
        detail={"channel": report["share_channel"]},
    )
    return report


@app.get("/geo/monitoring/connectors")
def geo_monitor_connectors(task_id: str | None = None, platform: str | None = None):
    return {"items": _db_monitor_connectors(task_id=task_id, platform=platform)}


@app.post("/geo/monitoring/connectors")
def geo_monitor_connector_save(request: GEOMonitorConnectorRequest):
    if not _db_get_task(request.task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    platform = request.platform.strip().lower()
    connector_id = f"conn_{hashlib.sha256(f'{request.task_id}:{platform}:{request.provider_name}'.encode()).hexdigest()[:12]}"
    now = _now_iso()
    existing = _db_get_monitor_connector(connector_id)
    item = {
        "connector_id": connector_id,
        "task_id": request.task_id,
        "platform": platform,
        "connector_type": request.connector_type.strip().lower() or "manual_audit",
        "provider_name": request.provider_name.strip(),
        "status": request.status.strip().lower() or "planned",
        "credential_env_var": (request.credential_env_var or "").strip() or None,
        "evidence_url": (request.evidence_url or "").strip() or None,
        "verification_method": request.verification_method.strip().lower() or "human_recorded",
        "notes": (request.notes or "").strip() or None,
        "owner": (request.owner or "").strip() or None,
        "next_check_at": (request.next_check_at or "").strip() or None,
        "recovery_hint": (request.recovery_hint or "").strip() or None,
        "last_error": existing.get("last_error") if existing else None,
        "last_checked_at": existing.get("last_checked_at") if existing else None,
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
    }
    _db_save_monitor_connector(item)
    _db_add_audit(_current_actor(), "save_monitor_connector", "monitor_connector", connector_id, request.task_id, outcome=item["status"])
    return _db_get_monitor_connector(connector_id)


@app.patch("/geo/monitoring/connectors/{connector_id}")
def geo_monitor_connector_update(connector_id: str, request: GEOMonitorConnectorStatusRequest):
    item = _db_get_monitor_connector(connector_id)
    if not item:
        raise HTTPException(status_code=404, detail="Monitoring connector not found.")
    if request.status is not None:
        status = request.status.strip().lower()
        if status:
            item["status"] = status
    if request.platform is not None:
        platform = request.platform.strip().lower()
        if not platform:
            raise HTTPException(status_code=400, detail="Platform cannot be empty.")
        item["platform"] = platform
    if request.connector_type is not None:
        connector_type = request.connector_type.strip().lower()
        if not connector_type:
            raise HTTPException(status_code=400, detail="Connector type cannot be empty.")
        item["connector_type"] = connector_type
    if request.provider_name is not None:
        provider_name = request.provider_name.strip()
        if not provider_name:
            raise HTTPException(status_code=400, detail="Provider name cannot be empty.")
        item["provider_name"] = provider_name
    if request.credential_env_var is not None:
        item["credential_env_var"] = request.credential_env_var.strip() or None
    if request.evidence_url is not None:
        item["evidence_url"] = request.evidence_url.strip() or None
    if request.last_error is not None:
        item["last_error"] = request.last_error.strip() or None
    if request.notes is not None:
        item["notes"] = request.notes.strip() or None
    if request.verification_method is not None:
        verification_method = request.verification_method.strip().lower()
        if not verification_method:
            raise HTTPException(status_code=400, detail="Verification method cannot be empty.")
        item["verification_method"] = verification_method
    if request.owner is not None:
        item["owner"] = request.owner.strip() or None
    if request.next_check_at is not None:
        item["next_check_at"] = request.next_check_at.strip() or None
    if request.recovery_hint is not None:
        item["recovery_hint"] = request.recovery_hint.strip() or None
    item["last_checked_at"] = _now_iso()
    item["updated_at"] = _now_iso()
    _db_save_monitor_connector(item)
    _db_add_audit(_current_actor(), "update_monitor_connector", "monitor_connector", connector_id, item["task_id"], outcome=item["status"])
    return _db_get_monitor_connector(connector_id)


@app.post("/geo/monitoring/connectors/{connector_id}/runs")
def geo_monitor_connector_run_save(connector_id: str, request: GEOMonitorConnectorRunRequest):
    item = _db_get_monitor_connector(connector_id)
    if not item:
        raise HTTPException(status_code=404, detail="Monitoring connector not found.")
    now = _now_iso()
    run = {
        "run_id": f"run_{uuid.uuid4().hex[:12]}",
        "connector_id": connector_id,
        "task_id": item["task_id"],
        "status": request.status.strip().lower() or item["status"],
        "notes": (request.notes or "").strip() or None,
        "evidence_url": (request.evidence_url or "").strip() or item.get("evidence_url"),
        "last_error": (request.last_error or "").strip() or None,
        "actor": _current_actor(),
        "created_at": now,
    }
    _db_save_connector_run(run)
    item["status"] = run["status"]
    item["evidence_url"] = run.get("evidence_url") or item.get("evidence_url")
    item["last_error"] = run.get("last_error")
    item["notes"] = run.get("notes") or item.get("notes")
    if request.next_check_at is not None:
        item["next_check_at"] = request.next_check_at.strip() or None
    item["last_checked_at"] = now
    item["updated_at"] = now
    _db_save_monitor_connector(item)
    _db_add_audit(_current_actor(), "save_connector_run", "monitor_connector", connector_id, item["task_id"], outcome=run["status"])
    return run


@app.get("/geo/actions")
def geo_gap_actions(task_id: str | None = None, status: str | None = None):
    return {"items": _db_gap_actions(task_id=task_id, status=status)}


@app.post("/geo/actions/bootstrap")
def geo_gap_actions_bootstrap(task_id: str):
    items = _bootstrap_gap_actions(task_id)
    _db_add_audit(_current_actor(), "bootstrap_gap_actions", "task", task_id, task_id, detail={"count": len(items)})
    return {"items": items}


@app.post("/geo/actions")
def geo_gap_action_save(request: GEOGapActionRequest):
    if not _db_get_task(request.task_id):
        raise HTTPException(status_code=404, detail="Project not found.")
    now = _now_iso()
    item = {
        "action_id": f"act_{uuid.uuid4().hex[:12]}",
        "task_id": request.task_id,
        "title": request.title.strip(),
        "action_type": request.action_type.strip().lower() or "content_gap",
        "source": request.source.strip().lower() or "manual",
        "priority": request.priority.strip().upper() or "P1",
        "status": request.status.strip().lower() or "accepted",
        "owner": (request.owner or "").strip() or None,
        "related_object_id": (request.related_object_id or "").strip() or None,
        "notes": (request.notes or "").strip() or None,
        "evidence_url": (request.evidence_url or "").strip() or None,
        "created_at": now,
        "updated_at": now,
        "completed_at": now if request.status.strip().lower() == "done" else None,
    }
    _db_save_gap_action(item)
    _db_add_audit(_current_actor(), "save_gap_action", "gap_action", item["action_id"], request.task_id, outcome=item["status"])
    return item


@app.patch("/geo/actions/{action_id}")
def geo_gap_action_update(action_id: str, request: GEOGapActionUpdateRequest):
    item = _db_get_gap_action(action_id)
    if not item:
        raise HTTPException(status_code=404, detail="Gap action not found.")
    if request.title is not None:
        title = request.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="Action title cannot be empty.")
        item["title"] = title
    if request.priority is not None:
        item["priority"] = request.priority.strip().upper() or item["priority"]
    if request.status is not None:
        item["status"] = request.status.strip().lower() or item["status"]
    if request.owner is not None:
        item["owner"] = request.owner.strip() or None
    if request.notes is not None:
        item["notes"] = request.notes.strip() or None
    if request.evidence_url is not None:
        item["evidence_url"] = request.evidence_url.strip() or None
    item["updated_at"] = _now_iso()
    item["completed_at"] = _now_iso() if item["status"] == "done" else None
    _db_save_gap_action(item)
    _db_add_audit(_current_actor(), "update_gap_action", "gap_action", action_id, item["task_id"], outcome=item["status"])
    return item


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
    with _db() as conn:
        existing = conn.execute("SELECT created_at FROM monitor_queries WHERE query_id = ?", (query_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO monitor_queries (
                query_id, task_id, query_text, category, competitor, engine,
                active, query_type, intent_stage, priority, reason, sample_target,
                language, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                existing["created_at"] if existing else now, now,
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
    with _db() as conn:
        conn.execute(
            """INSERT INTO source_observations (
                observation_id, task_id, query_id, source_domain, source_url, page_type,
                citation_count, notes, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    _db_save_trust_anchor(anchor)
    _db_add_audit(_current_actor(), "save_trust_anchor", "trust_anchor", anchor["anchor_id"], request.task_id)
    return anchor


@app.patch("/geo/monitoring/trust-anchors/{anchor_id}")
def geo_trust_anchor_update(anchor_id: str, request: GEOTrustAnchorUpdateRequest):
    anchor = _db_get_trust_anchor(anchor_id)
    if not anchor:
        raise HTTPException(status_code=404, detail="Trust anchor not found.")
    if request.channel is not None:
        channel = request.channel.strip().lower()
        if not channel:
            raise HTTPException(status_code=400, detail="Channel cannot be empty.")
        anchor["channel"] = channel
    if request.topic is not None:
        topic = request.topic.strip()
        if not topic:
            raise HTTPException(status_code=400, detail="Topic cannot be empty.")
        anchor["topic"] = topic
    if request.status is not None:
        anchor["status"] = request.status.strip() or anchor["status"]
    if request.owner is not None:
        anchor["owner"] = request.owner.strip() or None
    if request.target_url is not None:
        anchor["target_url"] = request.target_url.strip() or None
    if request.guidance is not None:
        anchor["guidance"] = request.guidance.strip() or anchor.get("guidance")
    if request.evidence_url is not None:
        anchor["evidence_url"] = request.evidence_url.strip() or None
    anchor["updated_at"] = _now_iso()
    _db_save_trust_anchor(anchor)
    _db_add_audit(
        _current_actor(),
        "update_trust_anchor",
        "trust_anchor",
        anchor_id,
        anchor["task_id"],
        outcome=anchor["status"],
    )
    return _db_get_trust_anchor(anchor_id)


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
    with _db() as conn:
        conn.execute(
            """INSERT INTO mention_checks (
                check_id, task_id, query_id, engine, brand_mentioned, mention_position,
                source_type, source_url, answer_excerpt, notes, cited_our_domain,
                competitor_mentions, confidence_weight, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                check["check_id"], check["task_id"], check["query_id"], check["engine"],
                int(check["brand_mentioned"]), check["mention_position"], check["source_type"],
                check["source_url"], check["answer_excerpt"], check["notes"],
                int(check["cited_our_domain"]), _json_dumps(check["competitor_mentions"]),
                check["confidence_weight"], check["checked_at"],
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
        "rollback_status": None,
        "rollback_note": None,
        "rollback_at": None,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    _db_save_publication(publication)
    _record_publication_event(publication, "pending_confirmation", "发布预览已创建，等待人工确认。")
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
    _record_publication_event(publication, publication["status"], publication.get("response_summary"))
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
    except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"上线页面校验失败：{_fetch_error_detail(exc)}") from exc

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
    _record_publication_event(publication, live_status, request.notes or "完成线上校验。")
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
    }
    _db_save_job(job)
    _record_publication_event(publication, "verify_scheduled", f"安排自动校验：{run_at}")
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
    _record_publication_event(publication, "pending_confirmation", "失败发布已重置，等待重新确认。")
    _db_add_audit(_current_actor(), "retry_publish", "publication", publication_id, publication["task_id"])
    return publication


@app.post("/cms/publications/rollback")
def cms_publication_rollback(request: CMSPublicationRollbackRequest):
    publication = _db_get_publication(request.publication_id)
    if not publication:
        raise HTTPException(status_code=404, detail="Publication not found.")
    if publication["status"] not in {"published", "verification_failed", "verified_live"}:
        raise HTTPException(status_code=409, detail="Only published publications can be marked for rollback.")
    publication["rollback_status"] = request.status.strip().lower() or "rollback_completed"
    publication["rollback_note"] = (request.notes or "").strip() or "人工确认已执行回滚。"
    publication["rollback_at"] = _now_iso()
    publication["updated_at"] = publication["rollback_at"]
    _db_save_publication(publication)
    _record_publication_event(publication, publication["rollback_status"], publication["rollback_note"])
    _db_add_audit(
        _current_actor(),
        "rollback_publication",
        "publication",
        publication["publication_id"],
        publication["task_id"],
        outcome=publication["rollback_status"],
    )
    return publication


@app.get("/geo/history")
def geo_history():
    return _db_history()


@app.post("/geo/articles/create")
def geo_article_create(request: GEOArticleCreateRequest):
    task = _db_get_task(request.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    now = _now_iso()
    result = task.get("latest_result") or {}
    title = (request.title or "").strip() or f"{result.get('title') or task.get('title') or 'GEO'} 文章优化方案"
    markdown = _build_geo_article_markdown(task, use_ai=request.use_ai, provider=request.provider, model=request.model)
    article_id = f"article_{uuid.uuid4().hex[:12]}"
    markdown_path = EXPORT_DIR / f"{_safe_file_stem(article_id + '_' + title)}.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    article = {
        "article_id": article_id,
        "task_id": request.task_id,
        "title": title,
        "status": "local_draft",
        "markdown_path": str(markdown_path),
        "feishu_url": None,
        "feishu_token": None,
        "feishu_response": None,
        "feishu_status": "not_synced",
        "last_feishu_sync_at": None,
        "public_url": None,
        "index_status": "feishu_created" if request.publish_to_feishu else "draft",
        "indexing_notes": None,
        "indexing_plan": _article_indexing_plan(task, title, None),
        "indexed_at": None,
        "last_index_checked_at": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    if request.publish_to_feishu:
        feishu = _sync_markdown_to_feishu(title, markdown_path, request.folder_token, request.feishu_identity)
        article["feishu_response"] = feishu.get("payload") or {"ok": feishu.get("ok")}
        article["feishu_url"] = feishu.get("external_url")
        article["feishu_token"] = feishu.get("external_id")
        article["last_feishu_sync_at"] = now
        article["feishu_status"] = "synced" if feishu.get("ok") else "failed"
        if feishu.get("ok"):
            article["status"] = "feishu_created"
            article["index_status"] = "feishu_created"
        else:
            article["status"] = "feishu_failed_local_draft"
            article["index_status"] = "draft"
            article["error"] = feishu.get("error") or "Failed to create Feishu document."
    article["indexing_plan"] = _article_indexing_plan(task, title, article.get("feishu_url"))
    _db_save_article(article)
    _db_save_article_index_event(
        {
            "event_id": f"aie_{uuid.uuid4().hex[:12]}",
            "article_id": article_id,
            "task_id": request.task_id,
            "index_status": article["index_status"],
            "public_url": article.get("public_url"),
            "notes": "文章草稿已创建。" if not article.get("error") else article.get("error"),
            "actor": _current_actor(),
            "created_at": now,
        }
    )
    _db_add_audit(
        _current_actor(),
        "create_geo_article",
        "article",
        article_id,
        request.task_id,
        outcome=article["status"],
        detail={"feishu_url": article.get("feishu_url"), "markdown_path": article.get("markdown_path")},
    )
    return article


@app.post("/geo/articles/{article_id}/feishu-sync")
def geo_article_feishu_sync(article_id: str, request: GEOArticleFeishuSyncRequest):
    article = _db_get_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    markdown_path = Path(article.get("markdown_path") or "")
    result = _sync_markdown_to_feishu(article["title"], markdown_path, request.folder_token, request.feishu_identity)
    article["feishu_response"] = result.get("payload") or {"ok": result.get("ok")}
    article["feishu_url"] = result.get("external_url")
    article["feishu_token"] = result.get("external_id")
    article["last_feishu_sync_at"] = _now_iso()
    article["feishu_status"] = "synced" if result.get("ok") else "failed"
    if result.get("ok"):
        article["status"] = "feishu_created"
        article["index_status"] = "feishu_created"
        article["error"] = None
    else:
        article["status"] = "feishu_failed_local_draft"
        article["error"] = result.get("error") or "Failed to sync article to Feishu."
    task = _db_get_task(article["task_id"])
    article["indexing_plan"] = _article_indexing_plan(task or {}, article["title"], article.get("feishu_url"), article.get("public_url"), article.get("index_status") or "draft")
    article["updated_at"] = _now_iso()
    _db_save_article(article)
    _db_save_article_index_event(
        {
            "event_id": f"aie_{uuid.uuid4().hex[:12]}",
            "article_id": article["article_id"],
            "task_id": article["task_id"],
            "index_status": article["index_status"],
            "public_url": article.get("public_url"),
            "notes": "飞书同步成功。" if result.get("ok") else article.get("error"),
            "actor": _current_actor(),
            "created_at": article["updated_at"],
        }
    )
    _db_add_audit(
        _current_actor(),
        "sync_article_feishu",
        "article",
        article_id,
        article["task_id"],
        outcome=article["feishu_status"],
        detail={"feishu_url": article.get("feishu_url"), "markdown_path": article.get("markdown_path")},
    )
    return article


@app.patch("/geo/articles/{article_id}/indexing")
def geo_article_indexing_update(article_id: str, request: GEOArticleIndexingRequest):
    article = _db_get_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    task = _db_get_task(article["task_id"])
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    now = _now_iso()
    status = request.index_status.strip() or "published"
    article["public_url"] = (request.public_url or "").strip() or article.get("public_url")
    article["index_status"] = status
    article["indexing_notes"] = (request.notes or "").strip() or article.get("indexing_notes")
    article["last_index_checked_at"] = now
    if status in {"indexed", "ai_cited"}:
        article["indexed_at"] = request.indexed_at or article.get("indexed_at") or now
    article["updated_at"] = now
    article["indexing_plan"] = _article_indexing_plan(task, article["title"], article.get("feishu_url"), article.get("public_url"), status)
    _db_save_article(article)
    _db_save_article_index_event(
        {
            "event_id": f"aie_{uuid.uuid4().hex[:12]}",
            "article_id": article_id,
            "task_id": article["task_id"],
            "index_status": status,
            "public_url": article.get("public_url"),
            "notes": article.get("indexing_notes"),
            "actor": _current_actor(),
            "created_at": now,
        }
    )
    _db_add_audit(
        _current_actor(),
        "update_article_indexing",
        "article",
        article_id,
        article["task_id"],
        outcome=status,
        detail={"public_url": article.get("public_url"), "index_status": status},
    )
    return article


@app.get("/geo/articles/{article_id}/indexing-checklist")
def geo_article_indexing_checklist(article_id: str):
    article = _db_get_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found.")
    return {"article_id": article_id, "markdown": _article_indexing_markdown(article), "article": article}


def _safe_file_stem(value: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_-]+", "_", value).strip("_")
    return stem[:80] or "geo_article"


def _article_indexing_plan(
    task: dict,
    title: str,
    feishu_url: str | None = None,
    public_url: str | None = None,
    index_status: str = "feishu_created",
) -> list[dict]:
    task_url = task.get("url") or ""
    public_target = public_url or "待填写公开 URL"
    feishu_target = feishu_url or "待创建飞书文档"
    return [
        {
            "step": "feishu_draft",
            "title": "飞书协作稿",
            "status": "done" if feishu_url else "todo",
            "detail": f"在飞书中完成《{title}》初稿、事实核对、图片和内部审阅。文档：{feishu_target}",
        },
        {
            "step": "public_publish",
            "title": "发布到可抓取公开页面",
            "status": "done" if public_url else "todo",
            "detail": "把飞书定稿发布到官网、博客、帮助中心或专题页。飞书私有文档通常不能直接被搜索引擎和大模型稳定抓取。",
        },
        {
            "step": "technical_indexing",
            "title": "提交收录信号",
            "status": "done" if index_status in {"submitted", "indexed", "ai_cited"} else "todo",
            "detail": f"将 {public_target} 加入 sitemap、站内入口、相关文章链接和 llms.txt / robots 可抓取路径，并在 Search Console 或站点发布系统中提交。",
        },
        {
            "step": "authority_sources",
            "title": "补强权威信源",
            "status": "todo",
            "detail": f"从原页面 {task_url}、官方说明、媒体报道和垂直社区反链到新文章，提升 Authority 与 Citation 机会。",
        },
        {
            "step": "ai_sampling",
            "title": "AI 平台采样复测",
            "status": "done" if index_status == "ai_cited" else "todo",
            "detail": "用 ChatGPT、豆包、DeepSeek、Kimi、Perplexity、Gemini 等平台运行目标 Query，记录 Mention、Citation、推荐位置和 Sources。",
        },
    ]


def _article_indexing_markdown(article: dict) -> str:
    lines = [
        f"# {article.get('title')} 收录推进清单",
        "",
        f"- 飞书文档：{article.get('feishu_url') or '未创建'}",
        f"- 公开 URL：{article.get('public_url') or '待发布'}",
        f"- 当前状态：{article.get('index_status') or article.get('status')}",
        "",
    ]
    for index, item in enumerate(article.get("indexing_plan") or [], 1):
        marker = "x" if item.get("status") == "done" else " "
        lines.extend([
            f"{index}. [{'x' if marker == 'x' else ' '}] {item.get('title')}",
            f"   - {item.get('detail')}",
        ])
    return "\n".join(lines)


def _record_report_export(
    format_name: str,
    result: dict,
    title: str,
    project_name: str | None = None,
    task_id: str | None = None,
    report_id: str | None = None,
):
    export = {
        "export_id": f"export_{uuid.uuid4().hex[:12]}",
        "report_id": report_id,
        "task_id": task_id,
        "project_name": project_name,
        "title": title,
        "format": format_name,
        "filepath": result.get("filepath") or result.get("filename"),
        "external_url": result.get("external_url"),
        "external_id": result.get("external_id"),
        "status": "success" if result.get("ok") else "failed",
        "note": result.get("note") or result.get("message") or result.get("error"),
        "actor": _current_actor(),
        "created_at": _now_iso(),
    }
    _db_save_report_export(export)
    if task_id:
        _db_add_audit(
            _current_actor(),
            "export_report",
            "report_export",
            export["export_id"],
            task_id,
            outcome=export["status"],
            detail={
                "format": format_name,
                "report_id": report_id,
                "filepath": export["filepath"],
                "external_url": export["external_url"],
            },
        )
    return export


def _sync_markdown_to_feishu(title: str, markdown_path: Path, folder_token: str | None = None, identity: str = "bot") -> dict:
    if not markdown_path.exists():
        return {"ok": False, "error": f"Markdown file not found: {markdown_path}"}
    synced = _create_feishu_doc_with_lark_cli(title, markdown_path, folder_token, identity)
    return {
        "ok": bool(synced.get("ok")),
        "external_url": synced.get("url"),
        "external_id": synced.get("token"),
        "payload": synced.get("payload"),
        "error": synced.get("error"),
    }


def _article_factor_rows(task: dict, result: dict, monitoring: dict) -> list[dict]:
    source_map = monitoring.get("source_map") or {}
    domain_count = len(source_map.get("domains") or [])
    mention_rate = monitoring.get("mention_rate") or 0
    citation_rate = monitoring.get("citation_rate") or 0
    recommendations = result.get("recommendations") or []
    gaps = result.get("content_gaps") or []
    assets = result.get("geo_assets") or {}
    keywords = assets.get("keywords") or []
    return [
        {
            "name": "时间性 Freshness",
            "score": "高",
            "diagnosis": "大模型更倾向引用能反映最新事实、政策、数据和观点的内容。",
            "action": "在文章开头标注更新时间，并加入近期价格、规则、开放时间、市场变化或数据点。",
        },
        {
            "name": "媒体权重 Authority",
            "score": f"{domain_count} 个信源",
            "diagnosis": "高权威来源、官方说明、垂直媒体和可验证出处能降低幻觉风险。",
            "action": "补充官方来源、权威媒体、Trip.com 页面链接和可信引用，避免无来源结论。",
        },
        {
            "name": "关键词覆盖 Keywords",
            "score": f"{len(keywords)} 个关键词",
            "diagnosis": "显性关键词、隐性关键词和语义相关词越完整，越容易被匹配到。",
            "action": "围绕核心 Query 扩展对比词、场景词、价格词、使用词和替代方案词。",
        },
        {
            "name": "互动信号 Engagement",
            "score": f"Mention {mention_rate}%",
            "diagnosis": "评论、收藏、分享、用户反馈和真实案例会提高内容被认可的概率。",
            "action": "在文章中加入用户选择场景、常见误区、FAQ 和可验证案例，方便后续沉淀互动。",
        },
        {
            "name": "问题相关性 Relevance",
            "score": f"Citation {citation_rate}%",
            "diagnosis": "内容越直接回答用户问题，越容易被大模型选作答案来源。",
            "action": "用问答式标题、对比表和结论先行结构，逐条解决高意图 Query。",
        },
    ]


def _build_geo_article_markdown(task: dict, use_ai: bool = False, provider: str = "openai", model: str | None = None) -> str:
    result = task.get("latest_result") or {}
    monitoring = _monitoring_summary(task["task_id"])
    title = result.get("title") or task.get("title") or task.get("brand_name") or "GEO 文章"
    url = task.get("url") or result.get("url") or ""
    gaps = result.get("content_gaps") or result.get("recommendations") or []
    recommendations = result.get("recommendations") or []
    faq_items = result.get("faq_items") or []
    modules = result.get("injection_modules") or []
    factors = _article_factor_rows(task, result, monitoring)
    query_lines = [
        f"- {item.get('query_text')}（{item.get('engine')} / {item.get('priority') or 'P1'}）"
        for item in (monitoring.get("queries") or [])[:8]
    ]
    factor_lines = [
        f"### {index + 1}. {item['name']}\n\n"
        f"- 当前信号：{item['score']}\n"
        f"- 诊断：{item['diagnosis']}\n"
        f"- 建议：{item['action']}"
        for index, item in enumerate(factors)
    ]
    faq_lines = [
        f"### {index + 1}. {item.get('question')}\n\n{item.get('answer')}"
        for index, item in enumerate(faq_items[:8])
    ]
    module_lines = [
        f"- **{item.get('module_type')}**：{item.get('title')}｜{item.get('target_position') or '页面模块'}"
        for item in modules[:8]
    ]
    markdown = "\n\n".join([
        f"# {title}：GEO 文章优化方案",
        f"> 来源页面：{url}\n>\n> 目标：让文章满足大模型选择信源时的五个关键条件，而不是只追求被传统搜索收录。",
        "## 一句话结论\n\n这篇文章需要同时补齐时间性、权威信源、关键词覆盖、互动信号和问题相关性，才能提高被 ChatGPT、豆包、DeepSeek、Kimi、Perplexity、Gemini 等 AI 平台引用的概率。",
        "## 五大影响因子诊断\n\n" + "\n\n".join(factor_lines),
        "## 高意图 Query 选题池\n\n" + ("\n".join(query_lines) if query_lines else "- 暂无监测 Query，请先在小程序中生成 Query。"),
        "## 页面内容缺口\n\n" + ("\n".join([f"- {item}" for item in gaps]) if gaps else "- 暂无明显内容缺口。"),
        "## 建议文章结构\n\n1. 最新更新时间与适用人群\n2. 核心问题的直接答案\n3. 官方规则与权威来源\n4. 产品/方案对比表\n5. 使用步骤与注意事项\n6. FAQ\n7. 相关 Trip.com 页面与外部可信来源",
        "## 可注入模块\n\n" + ("\n".join(module_lines) if module_lines else "- 暂无模块草稿。"),
        "## FAQ 草稿\n\n" + ("\n\n".join(faq_lines) if faq_lines else "- 暂无 FAQ 草稿。"),
        "## 下步执行\n\n" + ("\n".join([f"- {item}" for item in recommendations[:8]]) if recommendations else "- 完成文章初稿后，回到小程序录入 AI 平台采样并复测 Mention / Citation。"),
    ])
    if not use_ai:
        return markdown
    try:
        client = MultiLLMClient()
        prompt = (
            "你是 GEO 内容策略 Agent。请基于以下 Markdown，改写成适合飞书协作的中文文章方案。"
            "保留五大影响因子结构，补充更清晰的小标题、行动清单和可复制段落，不要编造事实。\n\n"
            f"{markdown}"
        )
        improved = client.generate_text(provider=provider, model=model, prompt=prompt, temperature=0.3)  # type: ignore[arg-type]
        return improved.strip() or markdown
    except Exception:
        return markdown


def _find_url_in_json(value) -> str | None:
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, dict):
        for key in ("url", "doc_url", "document_url", "share_url"):
            if isinstance(value.get(key), str) and value[key].startswith("http"):
                return value[key]
        for item in value.values():
            found = _find_url_in_json(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_url_in_json(item)
            if found:
                return found
    return None


def _find_token_in_json(value) -> str | None:
    if isinstance(value, dict):
        for key in ("document_id", "doc_token", "token", "obj_token"):
            if isinstance(value.get(key), str):
                return value[key]
        for item in value.values():
            found = _find_token_in_json(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_token_in_json(item)
            if found:
                return found
    return None


def _create_feishu_doc_with_lark_cli(title: str, markdown_path: Path, folder_token: str | None = None, identity: str = "bot") -> dict:
    lark_cli = shutil.which("lark-cli")
    if not lark_cli:
        return {"ok": False, "error": "lark-cli not found in PATH."}
    command = [
        lark_cli,
        "docs",
        "+create",
        "--as",
        "bot" if identity != "user" else "user",
        "--title",
        title,
        "--markdown",
        f"@{markdown_path.name}",
    ]
    if folder_token:
        command.extend(["--folder-token", folder_token])
    try:
        completed = subprocess.run(
            command,
            cwd=str(markdown_path.parent),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "lark-cli docs +create timed out."}
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": (completed.stderr or completed.stdout or "lark-cli docs +create failed.").strip(),
        }
    stdout = completed.stdout.strip()
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        payload = {"raw": stdout}
    return {
        "ok": True,
        "payload": payload,
        "url": _find_url_in_json(payload),
        "token": _find_token_in_json(payload),
    }


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
    publication_events = _db_publication_events(task_id=task_id, limit=200)
    task_feedback = [item for item in history["feedback_entries"] if item["task_id"] == task_id]
    task_jobs = [
        item
        for item in _db_jobs(limit=100)
        if item.get("payload", {}).get("task_id") == task_id
    ]
    task_experiments = _db_experiments(task_id)
    experiment_events = _db_experiment_events(task_id=task_id, limit=200)
    task_attributions = _db_attributions(task_id)
    task_reports = _db_reports(task_id)
    task_articles = _db_articles(task_id)
    article_events = _db_article_index_events(task_id=task_id, limit=200)
    events_by_article: dict[str, list[dict]] = {}
    for item in article_events:
        events_by_article.setdefault(item["article_id"], []).append(item)
    for item in task_articles:
        item["index_events"] = events_by_article.get(item["article_id"], [])
    task_gap_actions = _db_gap_actions(task_id)
    return {
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
        "cms_targets": _db_cms_targets(),
        "publications": task_publications,
        "publication_events": publication_events,
        "jobs": task_jobs,
        "monitoring": _monitoring_summary(task_id),
        "service_packages": _db_service_packages(status="active"),
        "experiments": task_experiments,
        "experiment_events": experiment_events,
        "attributions": task_attributions,
        "reports": task_reports,
        "report_exports": _db_report_exports(task_id=task_id, limit=100),
        "articles": task_articles,
        "article_index_events": article_events,
        "gap_actions": task_gap_actions,
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


# Feishu API Endpoints
@app.post("/feishu/auth/callback")
async def feishu_auth_callback(code: str | None = None, state: str | None = None):
    """Handle Feishu OAuth callback."""
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code required")

    try:
        from .feishu_service import get_feishu_service
        service = get_feishu_service()
        token_data = service.auth.get_user_token(code)

        return {
            "ok": True,
            "access_token": token_data.get("access_token"),
            "expires_in": token_data.get("expire")
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Auth failed: {str(exc)}") from exc


@app.get("/feishu/tables/{table_id}")
async def get_feishu_table_data(table_id: str, token: str | None = None):
    """Fetch data from a Feishu table."""
    try:
        from .feishu_service import get_feishu_service
        service = get_feishu_service()
        records = service.table_client.get_table_records(table_id, token)

        return {
            "ok": True,
            "table_id": table_id,
            "records": records
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch table: {str(exc)}") from exc


@app.post("/feishu/sync-tasks")
async def feishu_sync_geo_tasks(table_id: str, token: str | None = None):
    """Sync GEO tasks from Feishu table into system."""
    try:
        from .feishu_service import get_feishu_service
        service = get_feishu_service()
        tasks = service.sync_geo_tasks(table_id, token)

        return {
            "ok": True,
            "synced_count": len(tasks),
            "tasks": tasks
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(exc)}") from exc


@app.post("/feishu/notify")
async def feishu_send_notification(user_id: str, task_info: dict):
    """Send notification to Feishu user about task update."""
    try:
        from .feishu_service import get_feishu_service
        service = get_feishu_service()
        success = service.notify_task_update(user_id, task_info)

        if not success:
            raise Exception("Failed to send message")

        return {
            "ok": True,
            "user_id": user_id,
            "message": "Notification sent"
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Notification failed: {str(exc)}") from exc


@app.post("/feishu/create-doc")
async def feishu_create_article_doc(title: str, token: str | None = None):
    """Create a new GEO article template document in Feishu."""
    try:
        from .feishu_service import get_feishu_service
        service = get_feishu_service()
        doc = service.create_geo_template_doc(title, token)

        return {
            "ok": True,
            "doc": doc
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Document creation failed: {str(exc)}") from exc


# AI Monitoring API Endpoints
@app.post("/monitoring/generate-queries")
async def monitoring_generate_queries(brand_name: str, platform: str = "chatgpt",
                                     intent_types: List[str] | None = None, count: int = 12):
    """Generate monitoring queries for a brand."""
    if not intent_types:
        intent_types = ["comparison", "how-to", "buy", "worth", "scenario"]

    try:
        from .ai_monitoring_service import get_monitoring_service, AIPlatform
        service = get_monitoring_service()

        platform_enum = AIPlatform(platform.lower())
        queries = service.create_monitoring_queries(brand_name, intent_types, platform_enum, count=count)

        return {
            "ok": True,
            "brand_name": brand_name,
            "platform": platform,
            "queries_count": len(queries),
            "queries": queries
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Query generation failed: {str(exc)}") from exc


@app.post("/monitoring/check-visibility")
async def monitoring_check_visibility(query: str, platform: str, brand_terms: List[str]):
    """Check brand visibility for a query on a specific platform."""
    try:
        from .ai_monitoring_service import get_monitoring_service, AIPlatform
        service = get_monitoring_service()
        service.tracker.set_brand_terms(brand_terms)

        # For now, return mock response
        # In production, would use actual connector
        result = service.tracker.track_mention(
            AIPlatform(platform.lower()),
            query,
            mentioned=True,
            position=2
        )

        return {
            "ok": True,
            "result": result
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Visibility check failed: {str(exc)}") from exc


@app.post("/monitoring/parse-sources")
async def monitoring_parse_sources(answer_text: str, sources_text: str, brand_terms: List[str]):
    """Parse AI response and extract visibility metrics."""
    try:
        from .ai_monitoring_service import SourceParser
        parser = SourceParser()
        parsed = parser.parse_sources(answer_text, sources_text, brand_terms)

        return {
            "ok": True,
            "parsed_result": parsed
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Parsing failed: {str(exc)}") from exc


@app.get("/monitoring/report/{period}")
async def monitoring_get_report(period: str = "30d"):
    """Get AI visibility report for a period."""
    try:
        from .ai_monitoring_service import get_monitoring_service
        service = get_monitoring_service()
        report = service.get_visibility_report(period)

        return {
            "ok": True,
            "report": report
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {str(exc)}") from exc


@app.get("/monitoring/connectors")
async def monitoring_list_connectors():
    """List configured monitoring connectors."""
    try:
        from .ai_monitoring_service import get_monitoring_service
        service = get_monitoring_service()
        connectors = [conn.to_dict() for conn in service.connectors.values()]

        return {
            "ok": True,
            "connectors": connectors,
            "count": len(connectors)
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list connectors: {str(exc)}") from exc


# Report Export API Endpoints
@app.post("/reports/export/markdown")
async def export_report_markdown(
    project_name: str,
    title: str,
    data: dict,
    task_id: str | None = None,
    report_id: str | None = None,
):
    """Export report as Markdown."""
    try:
        from .report_export_service import get_report_export_service
        service = get_report_export_service()
        result = service.generate_markdown_report(title, data, project_name)
        _record_report_export("markdown", result, title, project_name, task_id, report_id)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Markdown export failed: {str(exc)}") from exc


@app.post("/reports/export/html")
async def export_report_html(
    project_name: str,
    title: str,
    data: dict,
    task_id: str | None = None,
    report_id: str | None = None,
):
    """Export report as HTML."""
    try:
        from .report_export_service import get_report_export_service
        service = get_report_export_service()
        result = service.generate_html_report(title, data, project_name)
        _record_report_export("html", result, title, project_name, task_id, report_id)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"HTML export failed: {str(exc)}") from exc


@app.post("/reports/export/json")
async def export_report_json(
    project_name: str,
    title: str,
    data: dict,
    task_id: str | None = None,
    report_id: str | None = None,
):
    """Export report as JSON."""
    try:
        from .report_export_service import get_report_export_service
        service = get_report_export_service()
        result = service.generate_json_report(title, data, project_name)
        _record_report_export("json", result, title, project_name, task_id, report_id)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"JSON export failed: {str(exc)}") from exc


@app.post("/reports/export/docx")
async def export_report_docx(
    project_name: str,
    title: str,
    data: dict,
    task_id: str | None = None,
    report_id: str | None = None,
):
    """Export report as Word (.docx)."""
    try:
        from .report_export_service import get_report_export_service
        service = get_report_export_service()
        result = service.generate_word_report(title, data, project_name)
        _record_report_export("docx", result, title, project_name, task_id, report_id)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Word export failed: {str(exc)}") from exc


@app.post("/reports/export/pdf")
async def export_report_pdf(
    project_name: str,
    title: str,
    data: dict,
    task_id: str | None = None,
    report_id: str | None = None,
):
    """Export report as PDF."""
    try:
        from .report_export_service import get_report_export_service
        service = get_report_export_service()
        result = service.generate_pdf_report(title, data, project_name)
        _record_report_export("pdf", result, title, project_name, task_id, report_id)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF export failed: {str(exc)}") from exc


@app.post("/reports/export/feishu")
async def export_report_feishu(
    project_name: str,
    title: str,
    data: dict,
    folder_token: str | None = None,
    identity: str = "bot",
    task_id: str | None = None,
    report_id: str | None = None,
):
    """Export report to a Feishu document via markdown handoff."""
    try:
        from .report_export_service import get_report_export_service
        service = get_report_export_service()
        markdown_result = service.generate_markdown_report(title, data, project_name)
        if not markdown_result.get("ok"):
            _record_report_export("feishu_doc", markdown_result, title, project_name, task_id, report_id)
            return markdown_result
        markdown_path = Path(markdown_result["filepath"])
        synced = _sync_markdown_to_feishu(title, markdown_path, folder_token, identity)
        result = {
            "ok": synced.get("ok", False),
            "format": "feishu_doc",
            "filepath": markdown_result.get("filepath"),
            "filename": markdown_result.get("filename"),
            "external_url": synced.get("external_url"),
            "external_id": synced.get("external_id"),
            "note": synced.get("error"),
            "payload": synced.get("payload"),
        }
        _record_report_export("feishu_doc", result, title, project_name, task_id, report_id)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Feishu export failed: {str(exc)}") from exc


# Claude URL Analysis API Endpoints
@app.post("/geo/analyze-with-claude")
async def geo_analyze_with_claude(
    url: str,
    title: str | None = None,
    analysis_type: str = "comprehensive",
    force_mode: str | None = None,
    client_name: str | None = None,
    brand_name: str | None = None,
    business_goal: str | None = None
):
    """Analyze URL using Claude-powered GEO analysis (dual-mode support)."""
    try:
        # Normalize and fetch URL
        url = _normalize_url(url)
        page_title, content = _fetch_page_text(url)
        title = title or page_title

        if len(content.split()) < 20:
            raise HTTPException(
                status_code=422,
                detail="Page content too short for analysis (minimum 20 words)"
            )

        # Get Claude analyzer with optional API key from env
        try:
            from backend.claude_url_analyzer import get_dual_mode_analyzer
        except ImportError:
            from claude_url_analyzer import get_dual_mode_analyzer
        analyzer = get_dual_mode_analyzer()

        # Perform analysis
        analysis_result = analyzer.analyze(url, title, content, force_mode)
        score_result = analyzer.score(url, title, content, force_mode)

        # Combine with basic GEO scoring
        base_score = score_content(content)

        # Create comprehensive result
        task_id = _build_task_id(url)
        result = {
            "task_id": task_id,
            "url": url,
            "title": title,
            "analysis_mode": analysis_result.get("mode_used"),
            "claude_analysis": analysis_result,
            "ai_score": score_result.get("ai_readiness_score", 0),
            "geo_score": base_score.get("geo_score", 0),
            "gaps": analysis_result.get("analysis", {}).get("content_gaps", score_result.get("gaps", [])),
            "recommendations": analysis_result.get("analysis", {}).get("recommendations", []),
            "analyzed_at": datetime.now(timezone.utc).isoformat()
        }

        # Save task if provided with business details
        if client_name or brand_name or business_goal:
            existing_task = _db_get_task(task_id) or {}
            _db_upsert_task({
                **existing_task,
                "task_id": task_id,
                "url": url,
                "title": title,
                "status": "analyzed",
                "latest_result": result,
                "client_name": client_name or existing_task.get("client_name"),
                "brand_name": brand_name or existing_task.get("brand_name") or title,
                "business_goal": business_goal or existing_task.get("business_goal"),
                "created_at": existing_task.get("created_at") or _now_iso(),
                "updated_at": _now_iso(),
            })

        return result

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=_fetch_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Claude analysis failed: {str(exc)}") from exc


@app.post("/geo/analyze-claude-gap-analysis")
async def geo_analyze_claude_gap_analysis(url: str, force_mode: str | None = None):
    """Perform Claude-powered gap analysis for a URL."""
    try:
        url = _normalize_url(url)
        title, content = _fetch_page_text(url)

        if len(content.split()) < 20:
            raise HTTPException(
                status_code=422,
                detail="Page content too short (minimum 20 words)"
            )

        try:
            from backend.claude_url_analyzer import get_dual_mode_analyzer
        except ImportError:
            from claude_url_analyzer import get_dual_mode_analyzer
        analyzer = get_dual_mode_analyzer()

        # Perform gap analysis
        gap_analysis = analyzer.claude_analyzer.analyze_content(
            url, title, content, "gap_analysis"
        )

        return {
            "ok": True,
            "url": url,
            "title": title,
            "gap_analysis": gap_analysis,
            "mode_used": gap_analysis.get("mode", "claude_api" if not gap_analysis.get("error") else "error")
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (HTTPError, URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=_fetch_error_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Gap analysis failed: {str(exc)}") from exc


@app.get("/geo/claude-config")
async def geo_get_claude_config():
    """Get Claude API configuration status."""
    try:
        import os
        try:
            from backend.claude_url_analyzer import get_dual_mode_analyzer
        except ImportError:
            from claude_url_analyzer import get_dual_mode_analyzer

        analyzer = get_dual_mode_analyzer()
        api_key = os.getenv("ANTHROPIC_API_KEY", "")

        return {
            "claude_configured": bool(api_key),
            "api_key_length": len(api_key),
            "api_key_preview": f"{api_key[:7]}...{api_key[-4:]}" if api_key else "Not configured",
            "default_model": "claude-3-5-sonnet-20241022",
            "modes_available": ["claude_api", "built_in"],
            "dual_mode_enabled": True,
            "fallback_mode": "built_in" if not bool(api_key) else "auto"
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Config check failed: {str(exc)}") from exc


@app.post("/geo/claude-config/set-api-key")
async def geo_set_claude_api_key(api_key: str):
    """Set Claude API key for current session."""
    try:
        if not api_key or not api_key.startswith("sk-ant-"):
            raise HTTPException(
                status_code=400,
                detail="Invalid API key format. Must start with 'sk-ant-'"
            )

        # Set in environment for this session
        import os
        os.environ["ANTHROPIC_API_KEY"] = api_key

        # Reinitialize analyzer with new key
        try:
            from backend.claude_url_analyzer import get_dual_mode_analyzer
        except ImportError:
            from claude_url_analyzer import get_dual_mode_analyzer
        global _analyzer
        _analyzer = None  # Reset global instance

        analyzer = get_dual_mode_analyzer(api_key)

        return {
            "ok": True,
            "message": "API key configured successfully",
            "configured": True,
            "key_preview": f"{api_key[:7]}...{api_key[-4:]}"
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Configuration failed: {str(exc)}") from exc
