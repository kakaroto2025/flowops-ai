from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import auth as api_auth
from apps.api import main as api_main
from apps.api.processor import JobProcessor
from shared.models import AuthContext, Job, LocalStore
from tools.auth import (
    AuthenticatedIdentity,
    AuthProvider,
    AuthTokenError,
    FirestoreMembershipRepository,
    InMemoryMembershipRepository,
    MembershipRepositoryError,
    UserMembership,
    auth_mode,
)


class FakeAuthProvider(AuthProvider):
    def verify_token(self, token: str) -> AuthenticatedIdentity:
        if token == "expired":
            raise AuthTokenError("invalid or expired bearer token")
        return AuthenticatedIdentity(external_uid=token)


class FakeFirestoreMembershipClient:
    def __init__(self, documents: dict[str, dict] | None = None, fail_reads: bool = False):
        self.documents = documents or {}
        self.fail_reads = fail_reads
        self.collection_calls: list[str] = []
        self.document_reads: list[str] = []
        self.query_calls = 0
        self.write_calls = 0

    def collection(self, name: str):
        self.collection_calls.append(name)
        return FakeMembershipCollection(self, name)


class FakeMembershipCollection:
    def __init__(self, client: FakeFirestoreMembershipClient, name: str):
        self.client = client
        self.name = name

    def document(self, document_id: str):
        return FakeMembershipDocument(self.client, self.name, document_id)

    def stream(self):
        self.client.query_calls += 1
        raise AssertionError("membership lookup must not scan collections")


class FakeMembershipDocument:
    def __init__(self, client: FakeFirestoreMembershipClient, collection: str, document_id: str):
        self.client = client
        self.collection = collection
        self.document_id = document_id

    def get(self):
        if self.client.fail_reads:
            raise RuntimeError("firestore unavailable")
        self.client.document_reads.append(self.document_id)
        payload = self.client.documents.get(self.document_id)
        return FakeMembershipSnapshot(payload)

    def set(self, payload):
        self.client.write_calls += 1
        raise AssertionError("membership login must not auto-create records")


class FakeMembershipSnapshot:
    def __init__(self, payload: dict | None):
        self.payload = payload
        self.exists = payload is not None

    def to_dict(self):
        return dict(self.payload or {})


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

    def test_firebase_mode_uses_persistent_membership_repository_factory(self):
        with (
            patch.dict(os.environ, {"AUTH_MODE": "firebase", "FIREBASE_PROJECT_ID": "flowops-test"}, clear=False),
            patch("apps.api.auth.FirestoreMembershipRepository.from_env") as factory,
        ):
            api_auth.get_membership_repository()

        factory.assert_called_once()

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

    def test_firestore_membership_exact_uid_lookup_succeeds(self):
        client = FakeFirestoreMembershipClient(
            {
                "uid_a": {
                    "external_uid": "uid_a",
                    "tenant_id": "tenant_a",
                    "user_id": "user_a",
                    "status": "active",
                    "roles": ["admin", "member"],
                }
            }
        )
        repository = FirestoreMembershipRepository(
            project_id="flowops-test",
            database="(default)",
            firestore_client=client,
        )

        membership = repository.get_by_external_uid("uid_a")

        self.assertEqual(membership.tenant_id, "tenant_a")
        self.assertEqual(membership.roles, ("admin", "member"))
        self.assertEqual(client.collection_calls, ["memberships"])
        self.assertEqual(client.document_reads, ["uid_a"])
        self.assertEqual(client.query_calls, 0)
        self.assertEqual(client.write_calls, 0)

    def test_firestore_membership_missing_uid_returns_none_without_auto_creation(self):
        client = FakeFirestoreMembershipClient()
        repository = FirestoreMembershipRepository(
            project_id="flowops-test",
            database="(default)",
            firestore_client=client,
        )

        self.assertIsNone(repository.get_by_external_uid("uid_missing"))
        self.assertEqual(client.document_reads, ["uid_missing"])
        self.assertEqual(client.write_calls, 0)

    def test_firestore_membership_malformed_payload_fails_closed(self):
        malformed_cases = [
            {"external_uid": "other_uid", "tenant_id": "tenant_a", "user_id": "user_a", "status": "active"},
            {"external_uid": "uid_a", "tenant_id": "../tenant", "user_id": "user_a", "status": "active"},
            {"external_uid": "uid_a", "tenant_id": "tenant_a", "user_id": "user/a", "status": "active"},
            {"external_uid": "uid_a", "tenant_id": "tenant_a", "user_id": "user_a", "status": "active", "roles": []},
        ]

        for index, payload in enumerate(malformed_cases):
            with self.subTest(index=index):
                repository = FirestoreMembershipRepository(
                    project_id="flowops-test",
                    database="(default)",
                    firestore_client=FakeFirestoreMembershipClient({"uid_a": payload}),
                )
                with self.assertRaises(MembershipRepositoryError):
                    repository.get_by_external_uid("uid_a")

    def test_firestore_membership_backend_failure_fails_closed_at_api(self):
        env, provider, _repo = self.firebase_patches(
            [UserMembership(external_uid="uid_a", tenant_id="tenant_a", user_id="user_a")]
        )
        failing_repository = FirestoreMembershipRepository(
            project_id="flowops-test",
            database="(default)",
            firestore_client=FakeFirestoreMembershipClient(fail_reads=True),
        )
        with env, provider, patch("apps.api.auth.get_membership_repository", return_value=failing_repository):
            response = self.client().get("/jobs", headers={"Authorization": "Bearer uid_a"})

        self.assertEqual(response.status_code, 403)

    def test_multiple_users_same_tenant_are_supported(self):
        repository = FirestoreMembershipRepository(
            project_id="flowops-test",
            database="(default)",
            firestore_client=FakeFirestoreMembershipClient(
                {
                    "uid_a": {"external_uid": "uid_a", "tenant_id": "tenant_shared", "user_id": "user_a", "status": "active"},
                    "uid_b": {"external_uid": "uid_b", "tenant_id": "tenant_shared", "user_id": "user_b", "status": "active"},
                }
            ),
        )

        self.assertEqual(repository.get_by_external_uid("uid_a").tenant_id, "tenant_shared")
        self.assertEqual(repository.get_by_external_uid("uid_b").tenant_id, "tenant_shared")

    def test_users_from_different_tenants_remain_isolated(self):
        repository = FirestoreMembershipRepository(
            project_id="flowops-test",
            database="(default)",
            firestore_client=FakeFirestoreMembershipClient(
                {
                    "uid_a": {"external_uid": "uid_a", "tenant_id": "tenant_a", "user_id": "user_a", "status": "active"},
                    "uid_b": {"external_uid": "uid_b", "tenant_id": "tenant_b", "user_id": "user_b", "status": "active"},
                }
            ),
        )

        self.assertEqual(repository.get_by_external_uid("uid_a").tenant_id, "tenant_a")
        self.assertEqual(repository.get_by_external_uid("uid_b").tenant_id, "tenant_b")

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
