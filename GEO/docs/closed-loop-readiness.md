# GEO Closed-loop Readiness

## Operational Loop

The application now enforces this URL workflow:

```text
URL analysis
-> AI/rule improvement
-> editable version
-> human approval
-> JSON delivery or CMS webhook injection
-> same-URL retest
-> persistent task history
```

## Implemented Controls

| Area | Current behavior |
|---|---|
| Task identity | Stable SHA-256-based task IDs survive backend restarts |
| Analysis | Public URL fetch, 100-point criteria, optional AI JSON, rule fallback |
| Improvement | Rule or AI-generated editable injection modules |
| Review | Only saved pending/rejected versions can be approved |
| Injection | Only approved versions can be delivered |
| Targets | Local JSON delivery artifact and public CMS webhook |
| Retest | Requires a completed injection for the same task and URL |
| Persistence | SQLite stores tasks, versions, injections, and retests |
| Recovery | History page can restore a task into the workbench |
| Operations | Web admin console shows task metrics, attention queues, and task timelines |
| Audit | SQLite operation log records analysis, version, approval, injection, and retest actions |
| Access control | Optional API Key authentication enforces viewer/operator/reviewer/admin roles and records trusted actors |
| Async operations | SQLite job queue persists immediate/scheduled retests, attempts, backoff, failures, and manual retries |
| Promotion projects | Every URL tracks owner, target score, stage, todos, next action, and measured effectiveness |
| Quality gate | Version completeness, Schema structure, and unsupported claims are checked before approval and publishing |
| CMS publishing | Environment-referenced credentials, publishing preview, explicit confirmation, result records, and failure visibility |
| Safety | Private page URLs and private webhook targets are blocked |
| Regression checks | Standard-library end-to-end workflow tests |

## External Production Dependencies

The application-side loop is complete. A production rollout still requires:

- a deployed HTTPS backend domain configured in the WeChat request domain list;
- a real CMS webhook contract and credentials;
- production secret storage, API key rotation, and audit retention;
- a release process that confirms the CMS draft was published before retest;
- an external cron/cloud scheduler calling the durable due-job runner;

Until a real CMS webhook is configured, `json_file` is a tracked delivery handoff,
not proof that the public page itself changed.
