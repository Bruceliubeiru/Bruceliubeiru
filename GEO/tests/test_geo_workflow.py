import tempfile
import unittest
from os import environ
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

from backend import auth, main


SAMPLE_CONTENT = """
GEO Growth OS is a content workflow for marketing teams and product operators.
It helps users explain products clearly, create quote-ready answers, and publish
structured content. For example, teams can compare options, review evidence,
and answer common questions. FAQ: How does it work? It creates a clear summary,
use cases, comparison guidance, proof points, and numbered publishing steps.
Research data and customer case evidence should be reviewed before publishing.
1. Diagnose the page. 2. Improve the content. 3. Approve and deliver the version.
"""


class GEOWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "test.db"
        self.export_dir = root / "exports"
        self.db_patch = patch.object(main, "DB_PATH", self.db_path)
        self.export_patch = patch.object(main, "EXPORT_DIR", self.export_dir)
        self.fetch_patch = patch.object(
            main,
            "_fetch_page_text",
            return_value=("Test GEO Page", SAMPLE_CONTENT),
        )
        self.dns_patch = patch.object(
            main.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        )
        self.db_patch.start()
        self.export_patch.start()
        self.fetch_patch.start()
        self.dns_patch.start()
        main.TASK_STORE.clear()
        main.VERSION_STORE.clear()
        main.RETEST_STORE.clear()
        main._init_db()

    def tearDown(self):
        self.dns_patch.stop()
        self.fetch_patch.stop()
        self.export_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _analyze(self):
        return main.geo_analyze(main.GEOAnalyzeRequest(url="https://example.com/geo"))

    def _approved_version(self):
        result = self._analyze()
        workflow = main.geo_improve(main.GEOImproveRequest(result=result))
        version = main.geo_version_save(
            main.GEOVersionSaveRequest(
                task_id=result["task_id"],
                url=result["url"],
                modules=workflow["improved_modules"],
                workflow=workflow,
            )
        )
        approved = main.geo_version_review(
            main.GEOReviewRequest(version_id=version["version_id"], action="approve")
        )
        return result, approved

    def test_task_id_is_stable_and_rerun_preserves_version(self):
        result, approved = self._approved_version()
        rerun = self._analyze()
        detail = main.geo_task_detail(result["task_id"])
        self.assertEqual(result["task_id"], rerun["task_id"])
        self.assertEqual(approved["version_id"], detail["task"]["latest_version_id"])
        self.assertEqual("approved", detail["task"]["status"])

    def test_complete_closed_loop(self):
        result, approved = self._approved_version()
        injection = main.geo_inject(
            main.GEOInjectRequest(version_id=approved["version_id"], target="json_file")
        )
        retest = main.geo_retest(
            main.GEORetestRequest(
                task_id=result["task_id"],
                url=result["url"],
                previous_score=result["geo_score"],
                version_id=approved["version_id"],
                injection_id=injection["injection_id"],
            )
        )
        detail = main.geo_task_detail(result["task_id"])

        self.assertEqual("completed", injection["status"])
        self.assertTrue(Path(injection["artifact_path"]).exists())
        self.assertEqual(injection["injection_id"], retest["injection_id"])
        self.assertEqual("ineffective", retest["effect_details"]["verdict"])
        self.assertEqual("retested", detail["task"]["status"])
        self.assertEqual(1, len(detail["versions"]))
        self.assertEqual(1, len(detail["injections"]))
        self.assertEqual(1, len(detail["retests"]))

    def test_retest_requires_completed_injection(self):
        result, approved = self._approved_version()
        with self.assertRaises(HTTPException) as context:
            main.geo_retest(
                main.GEORetestRequest(
                    task_id=result["task_id"],
                    url=result["url"],
                    previous_score=result["geo_score"],
                    version_id=approved["version_id"],
                )
            )
        self.assertEqual(409, context.exception.status_code)

    def test_unapproved_version_cannot_be_injected(self):
        result = self._analyze()
        workflow = main.geo_improve(main.GEOImproveRequest(result=result))
        version = main.geo_version_save(
            main.GEOVersionSaveRequest(
                task_id=result["task_id"],
                url=result["url"],
                modules=workflow["improved_modules"],
                workflow=workflow,
            )
        )
        with self.assertRaises(HTTPException) as context:
            main.geo_inject(main.GEOInjectRequest(version_id=version["version_id"]))
        self.assertEqual(409, context.exception.status_code)

    def test_private_webhook_is_blocked(self):
        with patch.object(main.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]):
            with self.assertRaises(ValueError):
                main._validate_public_webhook_url("http://localhost/hook")

    def test_private_page_url_is_blocked(self):
        with patch.object(main.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.2", 443))]):
            with self.assertRaises(ValueError):
                main._normalize_url("https://internal.example.com")

    def test_ai_analysis_failure_uses_rules_fallback(self):
        with patch("backend.main.MultiLLMClient", side_effect=RuntimeError("quota exhausted")):
            result = main.geo_analyze(
                main.GEOAnalyzeRequest(url="https://example.com/ai", use_ai=True)
            )
        self.assertEqual("rules", result["analysis_source"])
        self.assertIn("fallback_rules_used", result["ai_status"])

    def test_admin_overview_surfaces_operational_state(self):
        result, approved = self._approved_version()
        injection = main.geo_inject(
            main.GEOInjectRequest(version_id=approved["version_id"], target="json_file")
        )
        main.geo_retest(
            main.GEORetestRequest(
                task_id=result["task_id"],
                url=result["url"],
                previous_score=result["geo_score"],
                injection_id=injection["injection_id"],
            )
        )

        overview = main.admin_overview()
        tasks = main.admin_tasks(status="retested", q="Test GEO", limit=20)
        detail = main.admin_task_detail(result["task_id"])
        audit_logs = main.admin_audit_logs(limit=20, task_id=result["task_id"])

        self.assertEqual(1, overview["metrics"]["tasks"])
        self.assertEqual(1, overview["metrics"]["completed_injections"])
        self.assertEqual(1, overview["metrics"]["retests"])
        self.assertEqual(result["task_id"], tasks["items"][0]["task_id"])
        self.assertEqual(1, len(detail["injections"]))
        self.assertGreaterEqual(len(audit_logs["items"]), 5)
        self.assertEqual("retest", audit_logs["items"][0]["action"])
        self.assertEqual(result["task_id"], audit_logs["items"][0]["task_id"])

    def test_admin_audit_logs_support_filters(self):
        result, approved = self._approved_version()
        main.geo_inject(
            main.GEOInjectRequest(version_id=approved["version_id"], target="json_file")
        )

        review_logs = main.admin_audit_logs(action="approve", actor="local-dev", limit=20)
        failed_logs = main.admin_audit_logs(outcome="failed", limit=20)
        task_logs = main.admin_audit_logs(task_id=result["task_id"], outcome="success", limit=20)

        self.assertEqual(1, len(review_logs["items"]))
        self.assertEqual("approve", review_logs["items"][0]["action"])
        self.assertEqual("local-dev", review_logs["items"][0]["actor"])
        self.assertEqual([], failed_logs["items"])
        self.assertTrue(task_logs["items"])
        self.assertTrue(all(item["task_id"] == result["task_id"] for item in task_logs["items"]))
        self.assertTrue(all(item["outcome"] == "success" for item in task_logs["items"]))

    def test_authenticated_identity_is_used_for_audit_actor(self):
        result = self._analyze()
        workflow = main.geo_improve(main.GEOImproveRequest(result=result))
        identity_token = auth.set_current_identity(auth.AuthIdentity("alice@example.com", "operator"))
        try:
            version = main.geo_version_save(
                main.GEOVersionSaveRequest(
                    task_id=result["task_id"],
                    url=result["url"],
                    modules=workflow["improved_modules"],
                    workflow=workflow,
                    editor="forged@example.com",
                )
            )
        finally:
            auth.reset_current_identity(identity_token)

        logs = main.admin_audit_logs(action="save_version", limit=20)
        self.assertEqual("alice@example.com", version["editor"])
        self.assertEqual("alice@example.com", logs["items"][0]["actor"])
        self.assertEqual("forged@example.com", logs["items"][0]["detail"]["claimed_editor"])

    def test_scheduled_retest_job_completes_and_persists_result(self):
        result, approved = self._approved_version()
        injection = main.geo_inject(main.GEOInjectRequest(version_id=approved["version_id"]))
        job = main.geo_schedule_retest(
            main.GEORetestScheduleRequest(
                task_id=result["task_id"],
                url=result["url"],
                previous_score=result["geo_score"],
                injection_id=injection["injection_id"],
            ),
            BackgroundTasks(),
        )

        completed = main.admin_run_due_jobs(limit=10)["items"][0]
        stored = main.admin_jobs(status="completed", limit=10)["items"][0]
        claimed_again = main._run_job(job["job_id"])

        self.assertEqual(job["job_id"], completed["job_id"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual(completed["job_id"], stored["job_id"])
        self.assertEqual(result["task_id"], stored["result"]["task_id"])
        self.assertEqual(1, claimed_again["attempts"])

    def test_service_package_assignment_and_project_detail(self):
        result = self._analyze()
        package = main.geo_service_package_save(
            main.GEOServicePackageRequest(
                name="增长闭环套餐",
                tier="pro",
                price_cny=12800,
                delivery_days=21,
                platforms=["chatgpt", "perplexity"],
                features=["监测", "实验", "归因", "报告"],
            )
        )
        project = main.geo_project_update(
            result["task_id"],
            main.GEOProjectUpdateRequest(
                client_name="Acme",
                brand_name="Acme AI",
                package_id=package["package_id"],
                service_tier="pro",
            ),
        )
        detail = main.geo_task_detail(result["task_id"])

        self.assertEqual(package["package_id"], project["package_id"])
        self.assertEqual("增长闭环套餐", detail["project"]["package_name"])
        self.assertTrue(detail["service_packages"])

    def test_experiment_attribution_and_report_flow(self):
        result = self._analyze()
        experiment = main.geo_experiment_save(
            main.GEOExperimentRequest(
                task_id=result["task_id"],
                name="FAQ 对比实验",
                hypothesis="增加 FAQ 和对比表后提及率会上升",
                variant_a="旧版内容",
                variant_b="新版内容",
            )
        )
        confirmed_experiment = main.geo_experiment_confirm(
            experiment["experiment_id"],
            main.GEOExperimentConfirmRequest(status="won", winner="variant_b"),
        )
        attribution = main.geo_attribution_save(
            main.GEOAttributionRequest(
                task_id=result["task_id"],
                source_type="ai_platform",
                source_name="ChatGPT 推荐",
                attributed_revenue=5000,
                status="confirmed",
            )
        )
        report = main.geo_report_generate(
            main.GEOReportGenerateRequest(task_id=result["task_id"], period_label="近 30 天")
        )
        confirmed_report = main.geo_report_confirm(
            report["report_id"],
            main.GEOReportConfirmRequest(status="confirmed"),
        )
        history = main.geo_history()

        self.assertEqual("won", confirmed_experiment["status"])
        self.assertEqual("confirmed", attribution["status"])
        self.assertEqual(result["task_id"], report["task_id"])
        self.assertEqual("confirmed", confirmed_report["status"])
        self.assertTrue(history["experiments"])
        self.assertTrue(history["attributions"])
        self.assertTrue(history["reports"])

    def test_failed_job_waits_then_can_be_retried(self):
        result = self._analyze()
        job = main.geo_schedule_retest(
            main.GEORetestScheduleRequest(
                task_id=result["task_id"],
                url=result["url"],
                previous_score=result["geo_score"],
                injection_id="missing",
                max_attempts=2,
            ),
            BackgroundTasks(),
        )
        failed_once = main._run_job(job["job_id"])

        self.assertEqual("retry_wait", failed_once["status"])
        self.assertEqual(1, failed_once["attempts"])
        self.assertIn("not found", failed_once["last_error"])
        retried = main.admin_retry_job(job["job_id"], BackgroundTasks())
        self.assertEqual("queued", retried["status"])
        self.assertEqual(0, retried["attempts"])

    def test_project_management_exposes_owner_target_todos_and_next_action(self):
        result = self._analyze()
        project = main.geo_project_update(
            result["task_id"],
            main.GEOProjectUpdateRequest(owner="Bruce", target_score=88, todos=["补充证据"]),
        )
        detail = main.geo_project_detail(result["task_id"])

        self.assertEqual("Bruce", project["owner"])
        self.assertEqual(88, detail["project"]["target_score"])
        self.assertEqual(["补充证据"], detail["project"]["todos"])
        self.assertEqual("improve", detail["project"]["next_action_key"])

    def test_project_next_action_tracks_publication_and_retest_job_states(self):
        result, approved = self._approved_version()
        target = main.cms_target_save(
            main.CMSPublishTargetRequest(name="State CMS", webhook_url="https://cms.example.com/publish")
        )
        preview = main.cms_publication_preview(
            main.CMSPublishPreviewRequest(version_id=approved["version_id"], target_id=target["target_id"])
        )
        detail = main.geo_task_detail(result["task_id"])
        self.assertEqual("confirm_publish", detail["project"]["next_action_key"])

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return b'{"published":true}'

        with patch.object(main, "urlopen", return_value=Response()):
            published = main.cms_publication_confirm(
                main.CMSPublishConfirmRequest(publication_id=preview["publication_id"], confirmation="PUBLISH")
            )
        detail = main.geo_task_detail(result["task_id"])
        self.assertEqual("verify_publish", detail["project"]["next_action_key"])

        preview_terms = [item["title"] for item in preview["preview"]["modules"]]
        live_copy = " ".join(preview_terms)
        with patch.object(main, "_fetch_page_text", return_value=("Live page", live_copy)):
            verified = main.cms_publication_verify(
                main.CMSPublicationVerifyRequest(publication_id=published["publication_id"])
            )
        self.assertEqual("verified_live", verified["status"])
        detail = main.geo_task_detail(result["task_id"])
        self.assertEqual("schedule_retest", detail["project"]["next_action_key"])

        main.geo_schedule_retest(
            main.GEORetestScheduleRequest(
                task_id=result["task_id"],
                url=result["url"],
                previous_score=result["geo_score"],
                version_id=approved["version_id"],
                injection_id=published["injection_id"],
                run_at="2999-01-01T00:00:00+00:00",
            ),
            BackgroundTasks(),
        )
        detail = main.geo_task_detail(result["task_id"])
        self.assertEqual("wait_retest_job", detail["project"]["next_action_key"])

    def test_quality_gate_blocks_unsafe_version_and_allows_normal_version(self):
        result = self._analyze()
        blocked = main.geo_version_save(
            main.GEOVersionSaveRequest(
                task_id=result["task_id"],
                url=result["url"],
                modules=[{
                    "module_type": "hero",
                    "title": "Guaranteed",
                    "body": "We guarantee this is always the number one option for every customer.",
                    "target_position": "hero",
                }],
            )
        )
        self.assertEqual("blocked", blocked["quality_report"]["status"])
        with self.assertRaises(HTTPException) as context:
            main.geo_version_review(main.GEOReviewRequest(version_id=blocked["version_id"]))
        self.assertEqual(409, context.exception.status_code)

        workflow = main.geo_improve(main.GEOImproveRequest(result=result))
        normal = main.geo_version_save(
            main.GEOVersionSaveRequest(
                task_id=result["task_id"],
                url=result["url"],
                modules=workflow["improved_modules"],
                workflow=workflow,
            )
        )
        self.assertEqual("passed", normal["quality_report"]["status"])

    def test_cms_publication_requires_preview_and_confirmation(self):
        result, approved = self._approved_version()
        target = main.cms_target_save(
            main.CMSPublishTargetRequest(name="Test CMS", webhook_url="https://cms.example.com/publish")
        )
        preview = main.cms_publication_preview(
            main.CMSPublishPreviewRequest(version_id=approved["version_id"], target_id=target["target_id"])
        )
        self.assertEqual("pending_confirmation", preview["status"])
        with self.assertRaises(HTTPException):
            main.cms_publication_confirm(
                main.CMSPublishConfirmRequest(publication_id=preview["publication_id"], confirmation="yes")
            )

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return b'{"published":true}'

        with patch.object(main, "urlopen", return_value=Response()):
            published = main.cms_publication_confirm(
                main.CMSPublishConfirmRequest(publication_id=preview["publication_id"], confirmation="PUBLISH")
            )
        self.assertEqual("published", published["status"])
        self.assertTrue(published["injection_id"])

    def test_cms_target_can_be_disabled_and_enabled(self):
        target = main.cms_target_save(
            main.CMSPublishTargetRequest(name="Toggle CMS", webhook_url="https://cms.example.com/publish")
        )
        disabled = main.cms_target_update_status(
            target["target_id"],
            main.CMSPublishTargetStatusRequest(enabled=False),
        )
        enabled = main.cms_target_update_status(
            target["target_id"],
            main.CMSPublishTargetStatusRequest(enabled=True),
        )

        self.assertFalse(disabled["enabled"])
        self.assertTrue(enabled["enabled"])

    def test_failed_cms_publication_can_return_to_confirmation(self):
        result, approved = self._approved_version()
        target = main.cms_target_save(
            main.CMSPublishTargetRequest(name="Fail CMS", webhook_url="https://cms.example.com/fail")
        )
        preview = main.cms_publication_preview(
            main.CMSPublishPreviewRequest(version_id=approved["version_id"], target_id=target["target_id"])
        )
        with patch.object(main, "urlopen", side_effect=TimeoutError("timeout")):
            failed = main.cms_publication_confirm(
                main.CMSPublishConfirmRequest(publication_id=preview["publication_id"], confirmation="PUBLISH")
            )
        retried = main.cms_publication_retry(failed["publication_id"])
        self.assertEqual("failed", failed["status"])
        self.assertEqual("pending_confirmation", retried["status"])

    def test_published_cms_publication_can_be_verified_live(self):
        result, approved = self._approved_version()
        target = main.cms_target_save(
            main.CMSPublishTargetRequest(name="Test CMS", webhook_url="https://cms.example.com/publish")
        )
        preview = main.cms_publication_preview(
            main.CMSPublishPreviewRequest(version_id=approved["version_id"], target_id=target["target_id"])
        )

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return b'{"published":true}'

        with patch.object(main, "urlopen", return_value=Response()):
            published = main.cms_publication_confirm(
                main.CMSPublishConfirmRequest(publication_id=preview["publication_id"], confirmation="PUBLISH")
            )
        verified = main.cms_publication_verify(
            main.CMSPublicationVerifyRequest(
                publication_id=published["publication_id"],
                expected_terms=["GEO Growth OS"],
                notes="live page checked",
            )
        )
        self.assertEqual("verified_live", verified["status"])
        self.assertEqual("verified_live", verified["live_status"])
        self.assertIn("GEO Growth OS", verified["live_summary"]["matched_terms"])

    def test_published_cms_publication_can_schedule_automatic_verification(self):
        result, approved = self._approved_version()
        target = main.cms_target_save(
            main.CMSPublishTargetRequest(name="Auto Verify CMS", webhook_url="https://cms.example.com/publish")
        )
        preview = main.cms_publication_preview(
            main.CMSPublishPreviewRequest(version_id=approved["version_id"], target_id=target["target_id"])
        )

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, *_): return b'{"published":true}'

        with patch.object(main, "urlopen", return_value=Response()):
            published = main.cms_publication_confirm(
                main.CMSPublishConfirmRequest(publication_id=preview["publication_id"], confirmation="PUBLISH")
            )
        expected_terms = [item["title"] for item in preview["preview"]["modules"]]
        with patch.object(main, "_fetch_page_text", return_value=("Live page", " ".join(expected_terms))):
            job = main.cms_publication_verify_schedule(
                main.CMSPublicationVerifyScheduleRequest(publication_id=published["publication_id"]),
                BackgroundTasks(),
            )
            completed = main.admin_run_due_jobs(limit=10)["items"][0]

        self.assertEqual("publication_verify", job["job_type"])
        self.assertEqual("completed", completed["status"])
        self.assertEqual("verified_live", completed["result"]["status"])

    def test_knowledge_and_feedback_are_persisted_in_task_context(self):
        knowledge = main.geo_knowledge_save(
            main.GEOKnowledgeItemRequest(
                brand="example",
                category="facts",
                title="Brand wording",
                content="Use precise product language and avoid unsupported guarantees.",
            )
        )
        result = main.geo_analyze(main.GEOAnalyzeRequest(url="https://example.com/geo"))
        workflow = main.geo_improve(main.GEOImproveRequest(result=result))
        feedback = main.geo_feedback_save(
            main.GEOFeedbackRequest(
                task_id=result["task_id"],
                verdict="needs_edit",
                notes="运营要求补充真实案例证据。",
            )
        )
        detail = main.geo_task_detail(result["task_id"])

        self.assertTrue(result["knowledge_snapshot"])
        self.assertEqual("Brand wording", result["knowledge_snapshot"][0]["title"])
        self.assertIn("knowledge_snapshot", workflow)
        self.assertEqual("needs_edit", feedback["verdict"])
        self.assertEqual(1, len(detail["feedback"]))

        fake_output = """{"improved_modules":[{"module_type":"ai_summary","title":"Brand summary","body":"Use precise product language supported by the approved brand knowledge entry.","target_position":"below hero","priority":"high","change_reason":"clarity","acceptance_check":"reviewed"}]}"""
        with patch("backend.main.MultiLLMClient") as client_cls:
            client_cls.return_value.generate_text.return_value = fake_output
            ai_workflow = main.geo_improve(
                main.GEOImproveRequest(result=result, use_ai=True, provider="openai")
            )
        version = main.geo_version_save(
            main.GEOVersionSaveRequest(
                task_id=result["task_id"],
                url=result["url"],
                modules=ai_workflow["improved_modules"],
                workflow=ai_workflow,
            )
        )
        self.assertEqual([knowledge["knowledge_id"]], ai_workflow["improved_modules"][0]["knowledge_citations"])
        self.assertEqual(100, version["quality_report"]["citation_coverage"]["percent"])

    def test_llm_logs_are_saved_for_ai_calls(self):
        fake_output = """{"page_summary":{},"geo_assets":{},"content_gaps":[],"injection_modules":[],"faq_items":[],"schema_suggestions":[],"conversion_tips":[]}"""
        with patch("backend.main.MultiLLMClient") as client_cls:
            client_cls.return_value.generate_text.return_value = fake_output
            result = main.geo_analyze(
                main.GEOAnalyzeRequest(url="https://example.com/geo", use_ai=True, provider="openai")
            )
        logs = main.admin_llm_logs(task_id=result["task_id"], limit=20)
        self.assertEqual("ai", result["analysis_source"])
        self.assertEqual("geo_analyze", logs["items"][0]["action"])
        self.assertEqual("success", logs["items"][0]["status"])

    def test_commercial_project_seeds_multi_platform_monitoring(self):
        result = main.geo_analyze(
            main.GEOAnalyzeRequest(
                url="https://example.com/geo",
                client_name="Example Industrial",
                brand_name="Example Machines",
                target_engines=["chatgpt", "perplexity", "gemini"],
            )
        )
        main.geo_project_update(
            result["task_id"],
            main.GEOProjectUpdateRequest(owner="Bruce"),
        )
        detail = main.geo_task_detail(result["task_id"])

        self.assertEqual("Example Industrial", detail["project"]["client_name"])
        self.assertEqual(["chatgpt", "perplexity", "gemini"], detail["project"]["target_engines"])
        self.assertEqual(9, detail["monitoring"]["active_query_count"])
        self.assertTrue(detail["project"]["commercial_readiness"]["ready"])

    def test_source_map_generates_page_and_trust_recommendations(self):
        result = self._analyze()
        query = main.geo_monitor_query_save(
            main.GEOMonitorQueryRequest(
                task_id=result["task_id"],
                query_text="best GEO platform vs alternatives",
                engine="perplexity",
            )
        )
        main.geo_source_observation_save(
            main.GEOSourceObservationRequest(
                task_id=result["task_id"],
                query_id=query["query_id"],
                source_domain="industry.example",
                page_type="comparison",
                citation_count=4,
            )
        )
        source_map = main.geo_source_map(result["task_id"])

        self.assertEqual("industry.example", source_map["domains"][0]["domain"])
        self.assertEqual("comparison", source_map["page_types"][0]["page_type"])
        self.assertEqual("创建对比决策页", source_map["recommendations"][0]["title"])

    def test_mention_tracking_calculates_platform_visibility(self):
        result = self._analyze()
        query = main.geo_monitor_query_save(
            main.GEOMonitorQueryRequest(
                task_id=result["task_id"],
                query_text="best GEO growth tools",
                engine="chatgpt",
            )
        )
        main.geo_mention_check_save(
            main.GEOMentionCheckRequest(
                task_id=result["task_id"],
                query_id=query["query_id"],
                engine="chatgpt",
                brand_mentioned=True,
                mention_position=2,
                source_type="official",
            )
        )
        main.geo_mention_check_save(
            main.GEOMentionCheckRequest(
                task_id=result["task_id"],
                query_id=query["query_id"],
                engine="chatgpt",
                brand_mentioned=False,
            )
        )
        summary = main.geo_monitoring_summary(result["task_id"])

        self.assertEqual(50, summary["mention_rate"])
        self.assertEqual(2, summary["average_position"])
        self.assertEqual(1, summary["mention_count"])

    def test_trust_anchor_defaults_to_authentic_reviewable_guidance(self):
        result = self._analyze()
        anchor = main.geo_trust_anchor_save(
            main.GEOTrustAnchorRequest(
                task_id=result["task_id"],
                channel="reddit",
                topic="Answer a procurement question with verified evidence",
            )
        )
        self.assertIn("不伪装用户", anchor["guidance"])
        self.assertEqual("planned", anchor["status"])

    def test_query_generator_creates_prd_intent_pool(self):
        result = main.geo_analyze(
            main.GEOAnalyzeRequest(
                url="https://example.com/geo",
                brand_name="JR Pass",
                target_engines=["chatgpt"],
            )
        )
        generated = main.geo_monitor_queries_generate(
            main.GEOQueryGenerateRequest(task_id=result["task_id"], query_count=8, languages=["en"])
        )
        query_types = {item["query_type"] for item in generated["items"]}
        priorities = {item["priority"] for item in generated["items"]}

        self.assertEqual(8, generated["count"])
        self.assertIn("best", query_types)
        self.assertIn("compare", query_types)
        self.assertIn("worth", query_types)
        self.assertIn("P0", priorities)
        self.assertTrue(all(item["sample_target"] == 3 for item in generated["items"]))

    def test_source_parser_records_mentions_sources_citations_and_confidence(self):
        result = main.geo_analyze(
            main.GEOAnalyzeRequest(
                url="https://example.com/geo",
                brand_name="GEO Growth OS",
                target_engines=["perplexity"],
            )
        )
        generated = main.geo_monitor_queries_generate(
            main.GEOQueryGenerateRequest(task_id=result["task_id"], query_count=2, languages=["en"])
        )
        query_id = generated["items"][0]["query_id"]
        parsed = main.geo_source_answer_parse(
            main.GEOSourceParseRequest(
                task_id=result["task_id"],
                query_id=query_id,
                platform="perplexity",
                answer_text=(
                    "GEO Growth OS is a useful option. CompetitorX is also mentioned.\n"
                    "Sources: https://example.com/geo and https://media.example/guide"
                ),
                sources_text="https://example.com/geo\nhttps://media.example/guide",
                competitors=["CompetitorX"],
            )
        )
        summary = main.geo_monitoring_summary(result["task_id"])

        self.assertTrue(parsed["check"]["brand_mentioned"])
        self.assertTrue(parsed["check"]["cited_our_domain"])
        self.assertEqual(["CompetitorX"], parsed["check"]["competitor_mentions"])
        self.assertGreaterEqual(len(parsed["source_observations"]), 2)
        self.assertEqual(100, summary["citation_rate"])
        self.assertEqual("low", summary["sampling"]["confidence_level"])
        self.assertEqual(1, summary["competitor_gap"][0]["mentions"])

    def test_monitor_connector_and_gap_actions_are_persisted_and_bootstrapped(self):
        result = main.geo_analyze(
            main.GEOAnalyzeRequest(
                url="https://example.com/geo",
                brand_name="GEO Growth OS",
                target_engines=["chatgpt", "perplexity"],
            )
        )
        connector = main.geo_monitor_connector_save(
            main.GEOMonitorConnectorRequest(
                task_id=result["task_id"],
                platform="chatgpt",
                connector_type="official_api",
                provider_name="OpenAI Responses API",
                status="connected",
                credential_env_var="OPENAI_API_KEY",
            )
        )
        actions = main.geo_gap_actions_bootstrap(result["task_id"])["items"]
        updated = main.geo_gap_action_update(
            actions[0]["action_id"],
            main.GEOGapActionUpdateRequest(status="done", evidence_url="https://example.com/evidence"),
        )
        detail = main.geo_task_detail(result["task_id"])

        self.assertEqual("connected", connector["status"])
        self.assertTrue(detail["monitoring"]["connectors"])
        self.assertEqual(1, detail["project"]["action_progress"]["done"])
        self.assertTrue(any(item["action_type"] == "connector_setup" for item in actions))
        self.assertEqual("done", updated["status"])

    def test_effect_report_includes_connector_and_action_metrics(self):
        result = main.geo_analyze(
            main.GEOAnalyzeRequest(
                url="https://example.com/geo",
                brand_name="GEO Growth OS",
                target_engines=["chatgpt"],
            )
        )
        query = main.geo_monitor_query_save(
            main.GEOMonitorQueryRequest(
                task_id=result["task_id"],
                query_text="best GEO growth tools",
                engine="chatgpt",
            )
        )
        main.geo_mention_check_save(
            main.GEOMentionCheckRequest(
                task_id=result["task_id"],
                query_id=query["query_id"],
                engine="chatgpt",
                brand_mentioned=True,
                mention_position=1,
                cited_our_domain=True,
            )
        )
        main.geo_monitor_connector_save(
            main.GEOMonitorConnectorRequest(
                task_id=result["task_id"],
                platform="chatgpt",
                connector_type="official_api",
                provider_name="OpenAI Responses API",
                status="connected",
            )
        )
        action = main.geo_gap_action_save(
            main.GEOGapActionRequest(
                task_id=result["task_id"],
                title="同步高频信源结构",
                action_type="source_map",
                status="done",
            )
        )
        report = main.geo_report_generate(
            main.GEOReportGenerateRequest(task_id=result["task_id"], period_label="近 7 天")
        )

        self.assertEqual(1, report["metrics"]["connected_connectors"])
        self.assertEqual(100, report["metrics"]["gap_action_completion"])
        self.assertIn("已接入 1 个监测连接", report["findings"][2])
        self.assertEqual("done", action["status"])


class AuthTest(unittest.TestCase):
    API_KEYS = (
        '{"view-token":{"name":"view@example.com","role":"viewer"},'
        '"op-token":{"name":"op@example.com","role":"operator"},'
        '"review-token":{"name":"review@example.com","role":"reviewer"}}'
    )

    def test_local_development_defaults_to_admin(self):
        with patch.dict(environ, {"GEO_AUTH_REQUIRED": "false", "GEO_API_KEYS": ""}, clear=False):
            identity = auth.resolve_identity(None)
        self.assertEqual(auth.AuthIdentity("local-dev", "admin"), identity)

    def test_required_auth_resolves_configured_identity(self):
        with patch.dict(
            environ,
            {"GEO_AUTH_REQUIRED": "true", "GEO_API_KEYS": self.API_KEYS},
            clear=False,
        ):
            self.assertIsNone(auth.resolve_identity(None))
            self.assertEqual("operator", auth.resolve_identity("op-token").role)

    def test_role_matrix_protects_review_and_allows_hierarchy(self):
        viewer = auth.AuthIdentity("view@example.com", "viewer")
        reviewer = auth.AuthIdentity("review@example.com", "reviewer")

        self.assertEqual("viewer", auth.required_role("GET", "/admin/api/overview"))
        self.assertEqual("operator", auth.required_role("POST", "/admin/api/jobs/run-due"))
        self.assertEqual("operator", auth.required_role("POST", "/geo/inject"))
        self.assertEqual("viewer", auth.required_role("GET", "/geo/monitoring/summary"))
        self.assertEqual("reviewer", auth.required_role("POST", "/geo/version/review"))
        self.assertFalse(auth.has_role(viewer, "reviewer"))
        self.assertTrue(auth.has_role(reviewer, "operator"))

    def test_api_key_header_extraction(self):
        self.assertEqual("token-a", auth.extract_api_key("Bearer token-a", None))
        self.assertEqual("token-b", auth.extract_api_key("Bearer token-a", "token-b"))


if __name__ == "__main__":
    unittest.main()
