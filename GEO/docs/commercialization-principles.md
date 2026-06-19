# GEO Commercialization Principles

## Purpose

This document turns the current GEO prototype into a clearer commercial thesis,
product positioning, and 90-day execution checklist.

The core judgment is simple:

- model intelligence will keep commoditizing generic generation;
- GEO should not compete as "one more AI writing tool";
- GEO should compete as a governed AI-visibility delivery system with proof.

## Product Positioning

### What GEO is

GEO is a vertical operating system for AI visibility delivery.

In Phase 1, it should serve `Hong Kong/Japan cross-border travel` teams with:

- internal operators who monitor, improve, publish, verify, and report;
- internal reviewers/approvers who control client-facing output;
- client viewers/approvers who can see scoped dashboards, approvals, proofs, and reports.

### What GEO is not

GEO should not be positioned as:

- a generic AI SEO content generator;
- a self-serve prompt playground;
- a blind scraping tool for answer engines;
- a multi-agent demo looking for a business case;
- a token-metered wrapper around foundation models.

## Commercial Thesis

If model capabilities continue to improve every quarter, the durable value moves
up the stack.

For GEO, the moat is not raw generation quality. The moat is the operating
system around it:

1. Monitoring and benchmark visibility against competitors.
2. Action workflows that turn findings into page-level work.
3. Human approval before publication.
4. Release proof and live verification after publication.
5. Weekly reporting with scoped customer access.
6. Workspace/customer isolation, permissions, and auditability.

Generation remains important, but it should be treated as a replaceable module.

## Product Principles

### 1. Sell the loop, not the model

Every customer-facing workflow should reinforce:

`monitor -> diagnose -> improve -> approve -> publish -> verify -> retest -> report`

If a feature does not strengthen this loop, it is probably Phase 2 or later.

### 2. Governance is a product feature

Enterprise buyers do not only ask "can it do the task?"
They also ask:

- who can see this data?
- who approved this change?
- what exactly was published?
- can we prove it went live?
- can one customer ever see another customer's work?

This means tenancy, approval, session auth, release proof, and audit history are
not back-office concerns. They are core product value.

### 3. Manual-first is acceptable if proof is strong

In the first 90 days, GEO does not need to fully automate every data source.
It needs trustworthy evidence and repeatable delivery.

That means:

- manual or partner-authorized monitoring capture is acceptable;
- blind scraping should stay out of the MVP;
- generic webhook plus manual/live verification is acceptable;
- operator input is fine when it improves trust and delivery reliability.

### 4. Domain packs beat generic breadth

The first commercial wedge should be a travel-specific query and content pack for:

- pass purchase and eligibility;
- route planning and local alternatives;
- reservation, refund, and policy questions;
- attraction comparison and itinerary planning;
- airport transfer and seasonal travel intent.

Vertical depth will beat generic horizontal claims.

### 5. Client-safe surfaces matter more than feature count

The admin viewer mode should become the main client-facing product.
Clients should be able to:

- see current visibility benchmarks;
- review pending approvals;
- inspect release proofs;
- read weekly reports;
- never enter internal audit queues or unrelated workspaces.

The mini-program should stay operator-only in Phase 1.

### 6. Price around outcomes, not tokens

Internally GEO can track token and provider costs.
Externally the commercial story should move toward:

- workspace/customer delivery scope;
- weekly reporting cadence;
- monitored query coverage;
- approved and verified release volume;
- visibility improvement or retained delivery SLA.

Even before full outcome pricing, GEO should avoid telling the market it is just
"LLM calls with a UI."

## Phase 1 Offer

### Ideal early customer

The best first design partners are teams that already own web content and need
AI visibility help, but do not want to build an internal GEO function.

Examples:

- JR Pass or transport pass sellers;
- Japan local tour/activity operators serving Hong Kong travelers;
- OTA-like travel planners with destination guides and product pages;
- ticketing, airport transfer, and itinerary service teams.

### Phase 1 promise

Within one weekly operating cycle, GEO should help a partner:

1. See where they appear or disappear in AI answers.
2. Identify competitor citation gaps and unsupported-answer incidents.
3. Approve page or content actions with clear ownership.
4. Publish safely through a controlled workflow.
5. Verify live evidence and report weekly progress.

## 90-Day Execution Checklist

### Days 0-30

Make the backend commercially safe and scope-aware.

- Use `DATABASE_URL` as the persistence source of truth.
- Move persistence behind SQLAlchemy Core repositories and Alembic migrations.
- Add first-class `organizations`, `workspaces`, `customers`, memberships,
  invites, and browser sessions.
- Enforce workspace/customer scoping on all business records and read paths.
- Add invite-link plus session-cookie auth for browser users.
- Make admin and mini-program environment-driven instead of localhost-bound.

### Days 31-60

Turn monitoring into the product centerpiece.

- Add `monitor_runs` as the parent record for every collection event.
- Ship travel query packs for `zh-HK` and `en`.
- Add benchmark views for visibility share, citation share, competitor gap, and
  unsupported-answer incidents.
- Add role-aware viewer mode in admin for client users.
- Generate weekly customer reports from monitoring, releases, retests, and
  attribution inputs.

### Days 61-90

Harden delivery and pilot operations.

- Add `page_gap_actions` and `release_proofs`.
- Require client-visible signoff before final publish.
- Add a due-job worker, structured logs, backups, and restore drill.
- Produce HTML/PDF weekly report exports.
- Run internal dry runs, then onboard `2-3` design partners.
- Freeze scope around pilot bugs, trust gaps, and approval friction.

## Build More, Build Less

### Double down on

- benchmark visibility and citation evidence;
- scoped approvals and release proofs;
- tenant-safe reporting and dashboards;
- operator efficiency for weekly cycles;
- travel-specific templates, queries, and reporting language.

### Defer on purpose

- self-serve SaaS onboarding;
- SSO and enterprise billing;
- fully automated agent-traffic ingestion;
- broad multi-vertical expansion;
- fancy multi-agent orchestration without clear delivery value;
- mini-program access for client users;
- direct CMS-specific adapters beyond what pilots require.

## Commercial Metrics

The first commercial version is working if, within 90 days, GEO can support
`2-3` pilot partners on real domains without cross-customer leakage and with a
repeatable weekly delivery loop.

Phase 1 metrics should focus on:

- monitored queries per workspace;
- visibility share and citation share movement;
- approved actions completed on time;
- verified releases per reporting cycle;
- unsupported-answer incidents found and resolved;
- weekly report delivery reliability;
- operator hours saved per customer cycle.

## Current Repository Implications

Based on the current codebase direction, the repo should continue evolving
toward these three surfaces:

1. FastAPI backend as the governed system of record.
2. Admin console as the unified internal and client viewer workspace.
3. WeChat mini-program as the operator execution surface.

This also implies a strong architectural rule:

the same commercial workflow should be traceable end to end from task creation
to report export, with scope, approval, proof, and history preserved at every
step.

## Practical North Star

When choosing what to build next, prefer the option that makes GEO better at:

- proving what changed;
- showing who approved it;
- isolating customer data safely;
- demonstrating visibility progress clearly;
- helping operators deliver one more reliable weekly cycle.

If a feature is impressive in demo but weak in proof, safety, or repeatable
delivery, it should wait.
