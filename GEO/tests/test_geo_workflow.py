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
        self.assertEqual("reviewer", auth.required_role("POST", "/geo/version/review"))
        self.assertFalse(auth.has_role(viewer, "reviewer"))
        self.assertTrue(auth.has_role(reviewer, "operator"))

    def test_api_key_header_extraction(self):
        self.assertEqual("token-a", auth.extract_api_key("Bearer token-a", None))
        self.assertEqual("token-b", auth.extract_api_key("Bearer token-a", "token-b"))


if __name__ == "__main__":
    unittest.main()
