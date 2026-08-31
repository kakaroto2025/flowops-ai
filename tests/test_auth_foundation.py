from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import main as api_main
from apps.api.processor import JobProcessor
from shared.models import AuthContext, Job, LocalStore
from tools.auth import (
    AuthenticatedIdentity,
    AuthProvider,
    AuthTokenError,
    InMemoryMembershipRepository,
    UserMembership,
    auth_mode,
)


class FakeAuthProvider(AuthProvider):
    def verify_token(self, token: str) -> AuthenticatedIdentity:
        if token == "expired":
            raise AuthTokenError("invalid or expired bearer token")
        return AuthenticatedIdentity(external_uid=token)


class AuthFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalStore(Path(self.tmp.name) / "state.json")
        self.original_store = api_main.store
        self.original_processor = api_main.processor
        api_main.store = self.store
        api_main.processor = JobProcessor(self.store)

    def tearDown(self):
        api_main.store = self.original_store
        api_main.processor = self.original_processor
        self.tmp.cleanup()

    def client(self) -> TestClient:
        return TestClient(api_main.app)

    def firebase_patches(self, memberships: list[UserMembership] | None = None):
        return (
            patch.dict(os.environ, {"AUTH_MODE": "firebase", "FIREBASE_PROJECT_ID": "flowops-test"}, clear=False),
            patch("apps.api.auth.get_auth_provider", return_value=FakeAuthProvider()),
            patch("apps.api.auth.get_membership_repository", return_value=InMemoryMembershipRepository(memberships or [])),
        )

    def test_auth_mode_defaults_to_development(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(auth_mode(), "development")

    def test_development_mode_allows_business_route_without_bearer_token(self):
        with patch.dict(os.environ, {"AUTH_MODE": "development"}, clear=False):
            response = self.client().get("/jobs")

        self.assertEqual(response.status_code, 200)

    def test_health_remains_public_in_firebase_mode(self):
        with patch.dict(os.environ, {"AUTH_MODE": "firebase", "FIREBASE_PROJECT_ID": "flowops-test"}, clear=False):
            response = self.client().get("/health")

        self.assertEqual(response.status_code, 200)

    def test_missing_bearer_token_returns_401(self):
        env, provider, repo = self.firebase_patches()
        with env, provider, repo:
            response = self.client().get("/jobs")

        self.assertEqual(response.status_code, 401)

    def test_malformed_bearer_token_returns_401(self):
        env, provider, repo = self.firebase_patches()
        with env, provider, repo:
            response = self.client().get("/jobs", headers={"Authorization": "Basic token"})

        self.assertEqual(response.status_code, 401)

    def test_expired_or_invalid_token_returns_401(self):
        env, provider, repo = self.firebase_patches()
        with env, provider, repo:
            response = self.client().get("/jobs", headers={"Authorization": "Bearer expired"})

        self.assertEqual(response.status_code, 401)

    def test_valid_uid_without_tenant_membership_returns_403(self):
        env, provider, repo = self.firebase_patches()
        with env, provider, repo:
            response = self.client().get("/jobs", headers={"Authorization": "Bearer uid_missing"})

        self.assertEqual(response.status_code, 403)

    def test_inactive_membership_returns_403(self):
        env, provider, repo = self.firebase_patches(
            [UserMembership(external_uid="uid_disabled", tenant_id="tenant_a", user_id="user_a", status="DISABLED")]
        )
        with env, provider, repo:
            response = self.client().get("/jobs", headers={"Authorization": "Bearer uid_disabled"})

        self.assertEqual(response.status_code, 403)

    def test_valid_bearer_token_resolves_server_side_tenant(self):
        env, provider, repo = self.firebase_patches(
            [UserMembership(external_uid="uid_a", tenant_id="tenant_a", user_id="user_a", roles=("admin",))]
        )
        self.store.add_job(Job(id="job_000001", source="test", tenant_id="tenant_a", user_id="user_a", document_count=0))

        with env, provider, repo:
            response = self.client().get("/jobs", headers={"Authorization": "Bearer uid_a"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["job_id"], "job_000001")

    def test_client_tenant_id_cannot_override_authenticated_tenant(self):
        invoice = Path(self.tmp.name) / "tenant_override.pdf"
        invoice.write_text(
            "\n".join(
                [
                    "Empresa: Auth Tenant Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "NF: AUTH-001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )
        env, provider, repo = self.firebase_patches(
            [UserMembership(external_uid="uid_a", tenant_id="tenant_a", user_id="user_a")]
        )
        with (
            env,
            provider,
            repo,
            patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"),
            patch("agents.document.agent.extract_with_gemini", return_value=self._gemini_payload()),
        ):
            with invoice.open("rb") as handle:
                response = self.client().post(
                    "/jobs/upload/run",
                    headers={"Authorization": "Bearer uid_a"},
                    data={"processing_region": "AUTO", "tenant_id": "tenant_other"},
                    files={"files": ("tenant_override.pdf", handle, "application/pdf")},
                )

        self.assertEqual(response.status_code, 200)
        job_id = response.json()["job"]["id"]
        document_id = response.json()["documents"][0]["id"]
        self.assertEqual(self.store.jobs[job_id].tenant_id, "tenant_a")
        self.assertEqual(self.store.documents[document_id].tenant_id, "tenant_a")

    def test_tenant_a_cannot_access_tenant_b_job_by_known_id(self):
        env, provider, repo = self.firebase_patches(
            [UserMembership(external_uid="uid_a", tenant_id="tenant_a", user_id="user_a")]
        )
        self.store.add_job(Job(id="job_000001", source="test", tenant_id="tenant_b", user_id="user_b", document_count=0))

        with env, provider, repo:
            response = self.client().get("/jobs/job_000001", headers={"Authorization": "Bearer uid_a"})

        self.assertEqual(response.status_code, 403)

    def test_firebase_failure_never_falls_back_to_development_user(self):
        env, provider, repo = self.firebase_patches(
            [UserMembership(external_uid="local_dev_user", tenant_id="tenant_default", user_id="local_dev_user")]
        )
        with env, provider, repo:
            response = self.client().get("/jobs", headers={"Authorization": "Bearer expired"})

        self.assertEqual(response.status_code, 401)

    def test_business_endpoint_receives_auth_context(self):
        auth = AuthContext(user_id="user_a", tenant_id="tenant_a", authenticated=True)
        processor = JobProcessor(self.store, auth_context=auth)
        job = processor.create_upload_job([self._invoice("AUTH_CONTEXT.pdf")])

        self.assertEqual(job.tenant_id, "tenant_a")
        self.assertEqual(job.user_id, "user_a")

    def _invoice(self, name: str) -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(
            "\n".join(
                [
                    "Empresa: Auth Context Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "NF: AUTH-CTX-001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def _gemini_payload(self) -> dict:
        return {
            "document_type": "invoice",
            "cnpj": "12.345.678/0001-90",
            "company_name": "Auth Tenant Ltda",
            "invoice_number": "AUTH-001",
            "issue_date": "14/08/2026",
            "total_amount": 100.0,
            "confidence": 0.98,
            "warnings": [],
            "gemini_usage_metadata": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        }


if __name__ == "__main__":
    unittest.main()
