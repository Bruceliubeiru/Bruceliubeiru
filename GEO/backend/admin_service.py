from collections import Counter
from datetime import datetime, timezone


def _score(task: dict) -> int:
    result = task.get("latest_result") or {}
    return int(result.get("geo_score") or 0)


def _task_summary(task: dict, versions: list[dict], injections: list[dict], retests: list[dict]) -> dict:
    task_id = task["task_id"]
    task_versions = [item for item in versions if item.get("task_id") == task_id]
    task_injections = [item for item in injections if item.get("task_id") == task_id]
    task_retests = retests.get(task_id, [])
    latest_retest = task_retests[0] if task_retests else task.get("latest_retest")
    return {
        "task_id": task_id,
        "title": task.get("title") or task.get("url"),
        "url": task.get("url"),
        "status": task.get("status") or "unknown",
        "geo_score": _score(task),
        "latest_version_id": task.get("latest_version_id"),
        "version_count": len(task_versions),
        "injection_count": len(task_injections),
        "retest_count": len(task_retests),
        "latest_retest": latest_retest,
        "updated_at": task.get("updated_at"),
    }


def build_admin_overview(history: dict) -> dict:
    tasks = history.get("tasks") or []
    versions = history.get("versions") or []
    injections = history.get("injections") or []
    retests = history.get("retests") or {}
    summaries = [_task_summary(task, versions, injections, retests) for task in tasks]

    pending_versions = [item for item in versions if item.get("status") == "pending_review"]
    failed_injections = [item for item in injections if item.get("status") == "failed"]
    improved_retests = [
        item
        for items in retests.values()
        for item in items
        if int(item.get("score_delta") or 0) > 0
    ]
    all_retests = [item for items in retests.values() for item in items]
    average_score = round(sum(item["geo_score"] for item in summaries) / len(summaries), 1) if summaries else 0
    average_delta = (
        round(sum(int(item.get("score_delta") or 0) for item in all_retests) / len(all_retests), 1)
        if all_retests
        else 0
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "tasks": len(tasks),
            "pending_reviews": len(pending_versions),
            "completed_injections": len([item for item in injections if item.get("status") == "completed"]),
            "failed_injections": len(failed_injections),
            "retests": len(all_retests),
            "improved_retests": len(improved_retests),
            "average_score": average_score,
            "average_delta": average_delta,
        },
        "status_counts": dict(Counter(item["status"] for item in summaries)),
        "attention": {
            "pending_reviews": pending_versions[:8],
            "failed_injections": failed_injections[:8],
            "needs_more_work": [
                item for item in all_retests if item.get("status") == "needs_more_work"
            ][:8],
        },
        "recent_tasks": summaries[:12],
        "recent_injections": injections[:8],
        "recent_retests": all_retests[:8],
    }


def filter_admin_tasks(history: dict, status: str | None, query: str | None, limit: int) -> list[dict]:
    summaries = [
        _task_summary(
            task,
            history.get("versions") or [],
            history.get("injections") or [],
            history.get("retests") or {},
        )
        for task in history.get("tasks") or []
    ]
    if status:
        summaries = [item for item in summaries if item["status"] == status]
    if query:
        needle = query.strip().lower()
        summaries = [
            item
            for item in summaries
            if needle in (item.get("title") or "").lower()
            or needle in (item.get("url") or "").lower()
            or needle in item["task_id"].lower()
        ]
    return summaries[: max(1, min(limit, 200))]
