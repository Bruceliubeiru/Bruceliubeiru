# GEO Backend

## Purpose

Serve the GEO platform's delivery loop: diagnose, improve, review, publish, monitor, attribute, report, and operate.

## Current Closed Loops

### 1. Project And Package Ops

- Task/project detail with owner, target score, todos, business goal, and next action.
- Default service packages for `starter`, `growth`, and `pro`.
- Recommended package output based on target platforms, experiments, attribution, and reporting needs.

### 2. Monitoring And Auditable Connectors

- Multi-platform query generation and sampling persistence.
- Connector records for `official_api`, `manual_export`, and `manual_audit`.
- Connector blueprint suggestions with audit requirements and recovery-friendly run logs.

### 3. Source Map And Content Experiments

- Source/domain observation storage from pasted AI answers and sources.
- Source-map recommendations that can be converted into tracked actions.
- Trust anchor tasks and content experiment tracking.

### 4. Attribution And Effect Reporting

- Lead attribution records with evidence URL, stage, revenue, and confirmation status.
- Effect report generation and confirmation.
- Report export history for Markdown, HTML, JSON, DOCX, and PDF.

### 5. CMS And Publishing

- CMS target management.
- Publication preview, confirm, retry, and live verification.
- Job queue support for delayed retest and publication verification.

## Core Endpoints

```text
POST /geo/analyze
POST /geo/improve
POST /geo/version/save
POST /geo/version/review
POST /geo/inject
GET  /geo/tasks/{task_id}
POST /geo/projects/{task_id}
GET  /geo/service-packages
POST /geo/monitoring/connectors
POST /geo/monitoring/sources/parse
POST /geo/experiments
POST /geo/attributions
POST /geo/reports/generate
POST /cms/publications/preview
POST /cms/publications/confirm
POST /cms/publications/verify
POST /geo/articles/create
```
