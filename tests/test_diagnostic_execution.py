from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from diagnostic_execution import (
    BackendCapabilities,
    ExecutionContractError,
    ExecutionPlan,
    FakeExecutionBackend,
    PreflightReport,
    ResourceLimits,
    WorkerAdmissionController,
    WorkerRequest,
)
from diagnostic_trust import (
    ApprovalIssuanceService,
    SqliteApprovalConsumptionStore,
    TrustPolicy,
    consume_approval,
    verify_approval,
)


class EphemeralTestSigner:
    """Test-only in-memory signer; no key bytes are saved to disk."""

    key_id = "execution-test-key"

    def __init__(self):
        self._key = Ed25519PrivateKey.generate()

    @property
    def public_bytes(self):
        return self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, message):
        return self._key.sign(message)


class DiagnosticExecutionTests(unittest.TestCase):
    def setUp(self):
        self.limits = ResourceLimits(2, 2 * 1024**3, 1024**3, 600, 32, 4 * 1024**2)
        self.signer = EphemeralTestSigner()
        self.issuer = ApprovalIssuanceService(self.signer)
        self.trust = TrustPolicy(
            {self.signer.key_id: self.signer.public_bytes},
            approval_key_ids=frozenset({self.signer.key_id}),
        )
        self.now = datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc)
        self.plan = ExecutionPlan(
            project_id="localcodingagent",
            project_instance_id="a" * 32,
            run_id="b" * 32,
            run_nonce="d" * 32,
            source_sha256="1" * 64,
            backend_id="fake-hyperv",
            backend_version="1.0",
            vm_identity="vm-guid-123",
            architecture="x86_64",
            operation_id="diagnostic-noop-v1",
            launcher_sha256="2" * 64,
            media_hashes={"input_iso": "3" * 64, "output_template": "4" * 64},
            resource_limits=self.limits,
        )
        capabilities = BackendCapabilities(
            "fake-hyperv", "1.0", frozenset({"x86_64"}), True, 2,
            ResourceLimits(4, 8 * 1024**3, 3 * 1024**3, 3600, 128, 16 * 1024**2),
        )
        self.backend = FakeExecutionBackend(capabilities, PreflightReport(
            "fake-hyperv", "vm-guid-123", "x86_64", True, True, True,
            "2026-11-22T20:00:00Z", {"mode": "fake-only"}))

    def grant(self, action):
        payload = {
            "approval_id": format(len(self.backend.calls) % 16, "x") * 32,
            "project_id": self.plan.project_id,
            "project_instance_id": self.plan.project_instance_id,
            "run_id": self.plan.run_id,
            "run_nonce": self.plan.run_nonce,
            "source_sha256": self.plan.source_sha256,
            "backend_id": self.plan.backend_id,
            "backend_version": self.plan.backend_version,
            "vm_identity": self.plan.vm_identity,
            "architecture": self.plan.architecture,
            "operation_id": self.plan.operation_id,
            "launcher_sha256": self.plan.launcher_sha256,
            "media_hashes": dict(self.plan.media_hashes),
            "resource_limits": self.limits.as_dict(),
            "permitted_action": action,
            "issued_at": "2026-11-22T20:00:00Z",
            "expires_at": "2026-11-22T20:10:00Z",
        }
        raw = self.issuer.issue_approval(payload)
        approval = verify_approval(raw, self.trust, now=self.now, required_action=action)
        with tempfile.TemporaryDirectory() as temporary:
            store = SqliteApprovalConsumptionStore(Path(temporary) / "consume.sqlite3")
            grant = consume_approval(approval, store, required_action=action, now=self.now)
        return grant

    def test_fake_backend_runs_only_the_explicitly_granted_sequence(self):
        preflight = self.backend.preflight(self.plan)
        self.assertTrue(preflight.isolated and preflight.networking_disabled)
        media = self.backend.prepare_media(self.plan, self.grant("prepare_media"))
        self.backend.attach(self.plan, media, self.grant("attach_media"))
        worker = self.backend.start(self.plan, media, self.grant("start_worker"))
        self.backend.invoke_explicit_operation(
            self.plan, worker, self.grant("invoke_operation"))
        self.backend.stop(self.plan, worker, self.grant("stop_worker"))
        self.backend.detach(self.plan, media, self.grant("detach_media"))
        collected = self.backend.collect_read_only(
            self.plan, media, Path("C:/quarantine/run-b"), self.grant("collect_output"))
        self.assertTrue(collected.read_only)
        self.backend.cleanup(self.plan, media, self.grant("cleanup"))
        self.assertEqual(self.backend.calls.count("invoke_explicit_operation"), 1)

    def test_fake_backend_rejects_wrong_action_and_wrong_target_without_effect(self):
        wrong_action = self.grant("start_worker")
        from diagnostic_trust import TrustContractError
        with self.assertRaises(TrustContractError):
            self.backend.prepare_media(self.plan, wrong_action)
        altered_plan = ExecutionPlan(
            **{**self.plan.__dict__, "vm_identity": "other-vm"})
        with self.assertRaises(TrustContractError):
            self.backend.prepare_media(altered_plan, self.grant("prepare_media"))
        self.assertNotIn("prepare_media", self.backend.calls)

    def test_worker_admission_enforces_aggregate_caps_unique_scratch_and_media(self):
        scheduler = WorkerAdmissionController(
            max_workers=2,
            aggregate_limits=ResourceLimits(4, 4 * 1024**3, 2 * 1024**3,
                                            1200, 64, 8 * 1024**2),
        )
        first = WorkerRequest("worker-a", "run-a", Path("C:/scratch/a"), ("media-a",), self.limits)
        lease = scheduler.admit(first)
        with self.assertRaises(ExecutionContractError):
            scheduler.admit(WorkerRequest(
                "worker-b", "run-b", Path("C:/scratch/b"), ("media-a",), self.limits))
        with self.assertRaises(ExecutionContractError):
            scheduler.admit(WorkerRequest(
                "worker-b", "run-b", Path("C:/scratch/a"), ("media-b",), self.limits))
        with self.assertRaises(ExecutionContractError):
            scheduler.admit(WorkerRequest(
                "worker-b", "run-b", Path("C:/scratch/b"), ("media-b",),
                ResourceLimits(3, 1, 1, 1, 1, 1)))
        second = scheduler.admit(WorkerRequest(
            "worker-b", "run-b", Path("C:/scratch/b"), ("media-b",), self.limits))
        with self.assertRaises(ExecutionContractError):
            scheduler.admit(WorkerRequest(
                "worker-c", "run-c", Path("C:/scratch/c"), ("media-c",), self.limits))
        scheduler.release(lease)
        scheduler.release(second)

    def test_plan_identity_must_match_fake_capability_record(self):
        altered = ExecutionPlan(**{**self.plan.__dict__, "architecture": "arm64"})
        with self.assertRaises(ExecutionContractError):
            self.backend.preflight(altered)


if __name__ == "__main__":
    unittest.main()
