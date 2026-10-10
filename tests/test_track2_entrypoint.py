"""T9 pure request/default-denial tests; no live authority or widget claims."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_track2_authorization as approval_fixtures
from wechat_exporter import v3_entrypoint as entry
from wechat_exporter import v3_ui as ui
from wechat_exporter.history_contracts import ArchiveScope
from wechat_exporter.track2_contracts import CandidateRecord, FileIdentity, contract_digest


class EntrypointPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="synthetic-task9-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.namespace = self.root / "雪鸮-namespace"
        self.selection = ui.Selection(self.namespace, "synthetic-object", "synthetic-candidate", "SYNTHETIC")
        self.controller = ui.V3Controller(self.selection)
        self.candidate = CandidateRecord("synthetic-candidate", "synthetic-object",
            ArchiveScope("雪鸮-account", "会話-peer"), "SYNTHETIC",
            FileIdentity(7, "01" * 16, 1), "READY", "1" * 64, None, "synthetic-operation")

    def invoke(self, arguments):
        stream = io.StringIO()
        with redirect_stdout(stream), redirect_stderr(stream):
            result = entry.main(arguments)
        return result, stream.getvalue()

    def arguments(self, action):
        return [action, "--namespace", str(self.namespace), "--object-id", "synthetic-object",
            "--candidate-id", "synthetic-candidate", "--purpose", "SYNTHETIC"]

    def assert_code(self, code, result):
        self.assertEqual(code, getattr(result, "code", None))

    def test_no_arguments_refuses_with_nonzero_sanitized_selection_message(self):
        status, text = self.invoke([])
        self.assertEqual(2, status)
        self.assertIn("EXPLICIT_SELECTION_REQUIRED", text)

    def test_help_and_explicit_noop_are_nonwriting_zero_exit(self):
        status, text = self.invoke(["--help"])
        self.assertEqual(0, status)
        self.assertIn("no authority", text)
        status, text = self.invoke(self.arguments("noop"))
        self.assertEqual(0, status)
        self.assertIn("NOOP_NO_AUTHORITY", text)
        self.assertFalse(self.namespace.exists())

    def test_missing_each_selection_field_is_refused_before_profile_creation(self):
        base = self.arguments("build")
        for option in ("--namespace", "--object-id", "--candidate-id", "--purpose"):
            index = base.index(option)
            with self.subTest(option=option):
                status, text = self.invoke(base[:index] + base[index + 2:])
                self.assertEqual(2, status)
                self.assertNotIn(str(self.namespace), text)
        self.assertFalse(self.namespace.exists())

    def test_unknown_option_purpose_and_private_argument_are_never_echoed(self):
        private = "C:/PRIVATE/秘密-path-token"
        for args in (["--save-root", private], ["unknown-private-action", private],
                self.arguments("build")[:-1] + [private],
                self.arguments("build") + ["--approved", "true", "--synthetic", private]):
            with self.subTest(arguments=args):
                status, text = self.invoke(args)
                self.assertEqual(2, status)
                for rejected in (private, str(self.namespace), "unknown-private-action"):
                    self.assertNotIn(rejected, text)

    def test_relative_namespace_and_nonopaque_ids_fail_closed(self):
        for field, value in (("namespace", Path("relative")), ("namespace", self.root / ".." / "private"),
                ("object_id", "显示名称"), ("candidate_id", "../candidate"),
                ("object_id", "CON"), ("purpose", "unknown")):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "INVALID_SELECTION"):
                    ui.V3Controller(replace(self.selection, **{field: value}))

    def test_valid_unicode_namespace_and_scope_are_accepted_as_metadata_only(self):
        view = self.controller.present(self.candidate)
        self.assertEqual("READY", getattr(view, "state", None))
        self.assertFalse(getattr(view, "normal_save_authorized", True))

    def test_malformed_utf8_selection_is_stably_refused_without_echo(self):
        for field, value in (("namespace", self.root / "bad-\ud800"),
                ("object_id", "bad-\ud800"), ("purpose", "\ud800")):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "INVALID_SELECTION"):
                    ui.V3Controller(replace(self.selection, **{field: value}))
        args = self.arguments("build")
        args[args.index("--namespace") + 1] = str(self.root / "bad-\ud800")
        status, text = self.invoke(args)
        self.assertEqual(2, status)
        self.assertNotIn("bad-", text)

    def test_all_six_states_distinguish_preserved_candidate_and_active_without_permission(self):
        expected = (("BUILDING", "CANDIDATE", ("build",)),
            ("READY", "CANDIDATE", ("verify", "request_activation")),
            ("ACTIVATION_PENDING", "CANDIDATE", ("recover",)),
            ("ACTIVE", "ACTIVE_REFERENCE", ("verify",)),
            ("FAILED", "CANDIDATE", ("inspect_evidence", "request_fresh_candidate")),
            ("RECOVERY_REQUIRED", "CANDIDATE", ("recover",)))
        for state, kind, requests in expected:
            with self.subTest(state=state):
                view = self.controller.present(replace(self.candidate, state=state))
                self.assertEqual(state, getattr(view, "state", None))
                self.assertEqual(kind, getattr(view, "kind", None))
                self.assertEqual(requests, getattr(view, "requests", None))
                self.assertEqual("PRESERVED_V2_READONLY", getattr(view, "preserved_v2", None))
                self.assertFalse(getattr(view, "normal_save_authorized", True))
                self.assertFalse(getattr(view, "operations_available", True))

    def test_wrong_object_candidate_purpose_and_corrupt_dto_cannot_be_presented(self):
        for candidate in (None, {"state": "ACTIVE"},
                replace(self.candidate, object_id="other"),
                replace(self.candidate, archive_id="other"),
                replace(self.candidate, purpose="FORMAL"),
                replace(self.candidate, operation_id="bad-\ud800")):
            with self.subTest(kind=type(candidate).__name__):
                self.assert_code("BINDING_CONFLICT", self.controller.present(candidate))

    def test_cached_ready_and_active_cannot_authorize_either_save_mode(self):
        for state in ("BUILDING", "READY", "ACTIVATION_PENDING", "ACTIVE", "FAILED", "RECOVERY_REQUIRED"):
            code = "TRACK2_RECOVERY_REQUIRED" if state in ("ACTIVATION_PENDING", "RECOVERY_REQUIRED") else "RUNTIME_BINDING_UNVERIFIED"
            for mode in ("quick", "full"):
                with self.subTest(state=state, mode=mode):
                    self.assert_code(code, self.controller.request_save(replace(self.candidate, state=state), mode=mode))
        self.assert_code("RUNTIME_BINDING_UNVERIFIED", self.controller.request_save(self.candidate, mode="arbitrary"))

    def test_unavailable_requests_have_nonzero_cli_exit_and_no_namespace_io(self):
        for action in ("build", "verify", "save", "recover"):
            with self.subTest(action=action):
                status, text = self.invoke(self.arguments(action))
                self.assertEqual(3, status)
                self.assertIn("OPERATION_UNAVAILABLE", text)
        status, text = self.invoke(self.arguments("activate"))
        self.assertEqual(3, status)
        self.assertIn("ACTIVATION_APPROVAL_REQUIRED", text)
        self.assertFalse(self.namespace.exists())

    def test_build_verify_and_recovery_validate_state_before_unavailable_execution(self):
        self.assert_code("OPERATION_UNAVAILABLE", self.controller.request_build(replace(self.candidate, state="BUILDING")))
        self.assert_code("CANDIDATE_NOT_READY", self.controller.request_build(self.candidate))
        self.assert_code("OPERATION_UNAVAILABLE", self.controller.request_verify(self.candidate))
        self.assert_code("TRACK2_RECOVERY_REQUIRED", self.controller.request_verify(replace(self.candidate, state="RECOVERY_REQUIRED")))
        self.assert_code("OPERATION_UNAVAILABLE", self.controller.request_recovery(replace(self.candidate, state="RECOVERY_REQUIRED")))
        pending = replace(self.candidate, state="ACTIVATION_PENDING")
        self.assert_code("TRACK2_RECOVERY_REQUIRED", self.controller.request_recovery(pending))
        self.assert_code("OPERATION_UNAVAILABLE", self.controller.request_recovery(pending,
            intent_operation_id="synthetic-operation", intent_digest="2" * 64, expected_intent_digest="2" * 64))

    def test_canceled_activation_never_mutates_candidate_or_namespace(self):
        before = contract_digest(self.candidate)
        self.assert_code("CANCELLED", self.controller.request_activation(self.candidate, {"approved": True}, confirmed=False))
        self.assertEqual(before, contract_digest(self.candidate))
        self.assertFalse(self.namespace.exists())

    def test_confirmation_and_arbitrary_json_do_not_supply_activation_authority(self):
        for approval in (None, {"approved": True}, True, lambda: True):
            self.assert_code("ACTIVATION_APPROVAL_REQUIRED", self.controller.request_activation(self.candidate, approval, confirmed=True))
        self.assert_code("ACTIVATION_APPROVAL_REQUIRED", self.controller.request_activation(self.candidate, None, confirmed="yes"))

    def test_exact_typed_activation_chain_remains_non_authoritative_and_unavailable(self):
        fixture = approval_fixtures.ApprovalMetadataTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        approval, receipt, fork = fixture.chain()
        candidate = replace(self.candidate, scope=fork.scope, root_id=receipt.root_id,
            operation_id=fork.operation_id, evidence_digest=receipt.evidence_digest)
        before = tuple(contract_digest(item) for item in (candidate, approval, receipt, fork))
        self.assert_code("OPERATION_UNAVAILABLE", self.controller.request_activation(candidate,
            approval, receipt=receipt, fork=fork, confirmed=True))
        for changed in (replace(approval, candidate_id="other"),
                replace(approval, verification_digest="5" * 64),
                replace(approval, approval_id="bad-\ud800")):
            result = self.controller.request_activation(candidate, changed, receipt=receipt, fork=fork, confirmed=True)
            self.assertIn(getattr(result, "code", None), ("BINDING_CONFLICT", "ACTIVATION_APPROVAL_REQUIRED"))
        self.assertEqual(before, tuple(contract_digest(item) for item in (candidate, approval, receipt, fork)))

    def test_rehearsal_cannot_request_activation_even_with_confirmation(self):
        controller = ui.V3Controller(replace(self.selection, purpose="REHEARSAL"))
        self.assert_code("ACTIVATION_APPROVAL_REQUIRED", controller.request_activation(
            replace(self.candidate, purpose="REHEARSAL"), {"approved": True}, confirmed=True))

    def test_real_stage_and_release_delivery_boundaries_are_independently_denied(self):
        for purpose in ("SYNTHETIC", "REHEARSAL", "FORMAL"):
            controller = ui.V3Controller(replace(self.selection, purpose=purpose))
            for boundary in ("StageB", "StageCfork", "StageCactivation", "Release", "private-delivery"):
                with self.subTest(purpose=purpose, boundary=boundary):
                    self.assert_code("NOT_AUTHORIZED", controller.request_boundary(boundary))

    def test_actual_subprocess_import_help_denials_never_open_temp_roots_or_legacy_modules(self):
        # Audit actual filesystem events, including reads; no fake writer spies.
        sentinel = self.root / "legacy-config-sentinel"
        sentinel.write_bytes(b"unchanged synthetic legacy bytes")
        project = Path.cwd()
        script = r'''
import sys, os, json, pathlib
project, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
sys.path[:0] = [str(project / ".venv-release-py312/Lib/site-packages"), str(project)]
events = []
def audit(event, args):
    if event == "open" or event.startswith("os."):
        paths = [str(value) for value in args if isinstance(value, (str, bytes, os.PathLike))]
        if any(str(root).casefold() in value.casefold() for value in paths):
            events.append(event)
        if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            events.append("write-open")
        if event in ("os.mkdir", "os.remove", "os.rename", "os.rmdir"):
            events.append("mutation")
sys.addaudithook(audit)
from wechat_exporter import v3_entrypoint, v3_ui
selection = ["--namespace", str(root / "new-namespace"), "--object-id", "synthetic-object",
    "--candidate-id", "synthetic-candidate", "--purpose", "SYNTHETIC"]
statuses = [v3_entrypoint.main([]), v3_entrypoint.main(["--help"])]
statuses += [v3_entrypoint.main([action] + selection) for action in ("noop", "build", "activate", "save")]
blocked = [name for name in sys.modules if name in ("wechat_exporter.service", "wechat_exporter.gui",
    "wechat_exporter.main", "wechat_exporter.bootstrap", "wechat_exporter.config", "wechat_exporter.settings",
    "wechat_exporter.v3_profile", "wechat_exporter.v3_registry")]
print("AUDIT_JSON=" + json.dumps({"events": events, "blocked": blocked, "statuses": statuses}))
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-S", "-X", "utf8", "-c", script,
            str(project), str(self.root)], cwd=project, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout.split("AUDIT_JSON=")[-1])
        self.assertEqual([], payload["events"])
        self.assertEqual([], payload["blocked"])
        self.assertEqual([2, 0, 0, 3, 3, 3], payload["statuses"])
        self.assertEqual(b"unchanged synthetic legacy bytes", sentinel.read_bytes())
        self.assertFalse(self.namespace.exists())


if __name__ == "__main__":
    unittest.main()
