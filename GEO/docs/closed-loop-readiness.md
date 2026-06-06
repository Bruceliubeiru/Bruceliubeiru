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
| CMS publishing | Environment-referenced credentials, publishing preview, explicit confirmation, live-page verification, result records, and failure visibility |
| CMS operations | Admin console can enable/disable publish targets, and operators can select a specific target/publication record before publish, retry, or verification |
| Brand knowledge | Approved brand facts can be maintained in the admin console and injected into AI analysis/improvement prompts |
| Knowledge traceability | AI improvement modules retain knowledge item IDs and quality reports show citation coverage |
| Live verification automation | Published pages can be checked immediately or by durable retryable verification jobs |
| Feedback learning | Mini-program operators can record accept/edit/reject feedback, review feedback history, and feed later improvement runs |
| AI traceability | LLM call logs persist provider/model/status plus prompt/response excerpts for audit and quota tracking |
| Safety | Private page URLs and private webhook targets are blocked |
| Regression checks | Standard-library end-to-end workflow tests |
| Commercial intake | URL projects retain customer, brand, business goal, service tier, owner, target score, and target AI platforms |
| Service packages | Admin can maintain reusable commercial packages, bind them to projects, and track package-backed delivery expectations |
| Multi-platform visibility | Projects seed ChatGPT/Perplexity/Gemini monitoring queries and persist brand mention, position, source type, and answer evidence |
| AI source map | Citation domains and page types are aggregated into prioritized comparison, FAQ, content-cluster, media, and trust-anchor tasks |
| Ethical trust anchors | External contribution tasks require accountable ownership and evidence; default guidance forbids impersonation and fabricated experience |
| Content experiments | Mini-program and admin can register hypotheses, A/B content variants, confirm winning structures, and persist outcomes |
| Lead attribution | Operators can store attributable leads, evidence links, revenue, and confirmation state per project |
| Effect reporting | Projects can generate and confirm persistent GEO effect reports from monitoring, experiments, retests, and attributable revenue |

## External Client Delivery Standard

A project is ready to accept as a paid GEO engagement when it has:

1. A public URL, customer name, brand name, owner, business goal, and target AI platforms.
2. A baseline 100-point GEO diagnosis and at least three monitoring questions per target platform.
3. An AI source map or an explicit first-week source observation plan.
4. An approved content version, quality gate result, and traceable knowledge citations.
5. A confirmed delivery target, live verification, scheduled retest, and AI-platform visibility checks.

The system can now operate this workflow manually and persist its evidence. The remaining
production gap is automated collection from each AI platform. Until official APIs and account
credentials are connected, operators record answer excerpts, positions, and sources through
the monitoring endpoints instead of relying on unreviewable scraping.

## External Production Dependencies

The application-side loop is complete. A production rollout still requires:

- a deployed HTTPS backend domain configured in the WeChat request domain list;
- a real CMS webhook contract and credentials;
- production secret storage, API key rotation, and audit retention;
- a release process that confirms the CMS draft was published before retest;
- an external cron/cloud scheduler calling the durable due-job runner;

Until a real CMS webhook is configured, `json_file` is a tracked delivery handoff,
not proof that the public page itself changed.
