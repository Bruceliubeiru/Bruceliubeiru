import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

try:
    from backend.geo_scoring import score_content
except ImportError:
    from geo_scoring import score_content

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


def _update_stage_status(workflow: dict, key: str, status: str) -> dict:
    stages = workflow.get("stages") or []
    for stage in stages:
        if stage.get("key") == key:
            stage["status"] = status
    workflow["stages"] = stages
    return workflow


def _build_version_id(task_id: str) -> str:
    existing_count = sum(1 for version in VERSION_STORE.values() if version.get("task_id") == task_id)
    return f"{task_id}_v{existing_count + 1}"


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
    if not parsed.netloc or "." not in parsed.netloc:
        raise ValueError("Please provide a valid website URL.")
    return value


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
    package = _build_content_package(url, title, content, request, score_result)
    task_id = f"geo_{abs(hash(url)) % 100000000}"
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
    TASK_STORE[task_id] = {
        "task_id": task_id,
        "url": url,
        "title": title,
        "status": "analyzed",
        "latest_result": result,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    return result


@app.post("/geo/improve")
def geo_improve(request: GEOImproveRequest):
    workflow = _build_improvement_workflow(request.result)

    if request.use_ai and request.provider:
        prompt = f"""
You are a GEO content injection editor.
Improve the following content package so it can be injected into a landing page CMS.
Keep all claims consistent with the provided page facts. Do not invent price, rating, inventory, or review counts.
Return concise module copy that is AI-readable, conversion-aware, and easy to review.

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
            workflow["ai_output"] = output
            workflow["ai_status"] = "generated"
        except Exception as exc:
            workflow["ai_status"] = f"fallback_rules_used: {exc}"

    task_id = request.result.get("task_id")
    if task_id:
        task = TASK_STORE.setdefault(
            task_id,
            {
                "task_id": task_id,
                "url": request.result.get("url"),
                "title": request.result.get("title"),
                "created_at": _now_iso(),
            },
        )
        task["status"] = "draft_ready"
        task["latest_workflow"] = workflow
        task["updated_at"] = _now_iso()

    return workflow


@app.post("/geo/version/save")
def geo_version_save(request: GEOVersionSaveRequest):
    if not request.modules:
        raise HTTPException(status_code=400, detail="No modules to save.")

    workflow = request.workflow or {}
    workflow = _update_stage_status(workflow, "review", "pending")
    version_id = _build_version_id(request.task_id)
    payload = workflow.get("injection_payload") or {
        "url": request.url,
        "task_id": request.task_id,
        "version_status": "pending_review",
        "cms_fields": [
            {
                "field": module.get("injection_field") or f"cms.{module.get('module_type', 'content')}",
                "module_type": module.get("module_type"),
                "title": module.get("title"),
                "body": module.get("body"),
                "target_position": module.get("target_position"),
                "priority": module.get("priority"),
            }
            for module in request.modules
        ],
    }
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
    VERSION_STORE[version_id] = version

    task = TASK_STORE.setdefault(
        request.task_id,
        {"task_id": request.task_id, "url": request.url, "created_at": _now_iso()},
    )
    task["status"] = "pending_review"
    task["latest_version_id"] = version_id
    task["updated_at"] = _now_iso()

    return version


@app.post("/geo/version/review")
def geo_version_review(request: GEOReviewRequest):
    version = VERSION_STORE.get(request.version_id)
    if not version:
        raise HTTPException(status_code=404, detail="Version not found.")

    if request.action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Action must be approve or reject.")

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

    task_id = version.get("task_id")
    if task_id in TASK_STORE:
        TASK_STORE[task_id]["status"] = status
        TASK_STORE[task_id]["latest_version_id"] = request.version_id
        TASK_STORE[task_id]["updated_at"] = _now_iso()

    return version


@app.post("/geo/retest")
def geo_retest(request: GEORetestRequest):
    try:
        url = _normalize_url(request.url)
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
    RETEST_STORE.setdefault(request.task_id, []).append(retest)

    if request.task_id in TASK_STORE:
        TASK_STORE[request.task_id]["status"] = "retested"
        TASK_STORE[request.task_id]["latest_retest"] = retest
        TASK_STORE[request.task_id]["updated_at"] = _now_iso()

    return retest


@app.get("/geo/history")
def geo_history():
    tasks = sorted(TASK_STORE.values(), key=lambda item: item.get("updated_at", ""), reverse=True)
    return {
        "tasks": tasks,
        "versions": list(VERSION_STORE.values()),
        "retests": RETEST_STORE,
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
