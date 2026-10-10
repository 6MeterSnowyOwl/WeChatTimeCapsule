"""Native diagnostic behavior, not product termination certification.

Breaks caught: absent atomic containment, incomplete job reap, released/missing
lease, missing reader descendant, late native write, and startup ownership gap.
"""
import json
import ctypes
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch, Mock
import _track2_native_supervisor as diagnostic

from _track2_native_supervisor import (run_diagnostic, Channel, ProtocolError,
    strict_json, read_json, MAX_JSON, NativeJob, Blocked, create_operation, kernel_api)


def finish_startup_operation(temp, ownership):
    """Only the same outer-owned operation ledger may authorize tree removal."""
    if not ownership.safe:
        temp._finalizer.detach()
        ownership.fail()
        raise diagnostic.CleanupUncertain("STARTUP_TEST_RESOURCES_RETAINED")
    try:
        temp.cleanup()
    except BaseException:
        temp._finalizer.detach()
        ownership.fail()
        raise


class NativeDiagnosticTests(unittest.TestCase):
    def check_case(self, mode):
        facts = run_diagnostic(mode)
        print("NATIVE_DIAGNOSTIC=" + json.dumps(facts, sort_keys=True), flush=True)
        self.assertEqual("PASS", facts["status"], facts)
        self.assertTrue(facts["cleanup_complete"])
        self.assertTrue(facts["process_signaled"])
        self.assertTrue(facts["baseline_unchanged"])
        self.assertTrue(facts["owner_handles_closed"])
        self.assertGreaterEqual(facts["observer_handles_closed"], 2)
        return facts

    def test_returned_media_completion_reaps_real_reader(self):
        facts = self.check_case("normal")
        self.assertEqual(0, facts["job_active_after"])
        self.assertEqual("success", facts["media_status"])
        self.assertTrue(facts["reader_descendant_observed"])

    def test_native_hang_cancel_reaps_descendants_and_releases_same_lock(self):
        facts = self.check_case("native")
        self.assertEqual(0, facts["job_active_after"])
        self.assertGreaterEqual(facts["contained_before"], 2)
        self.assertTrue(facts["reacquire_handles_closed"])
        for name in ("native_entry", "lease_held", "reader_descendant_observed",
                     "descendants_signaled", "fresh_process_reacquired_same_lock",
                     "no_late_write", "no_late_success"):
            self.assertTrue(facts[name], name)

    def test_pre_resume_supervisor_death_kills_atomically_contained_child(self):
        facts = self.check_case("pre_resume_death")
        self.assertTrue(facts["creation_complete"])
        self.assertFalse(facts["resumed"])
        self.assertTrue(facts["supervisor_signaled"])
        self.assertTrue(facts["worker_never_started"])
        self.assertTrue(facts.get("terminated_actual_supervisor", False),
                        "must terminate the actual job owner, not only the venv redirector")


class ProtocolTests(unittest.TestCase):
    def event(self, phase="LEASE", sequence=1, operation="synthetic-operation", payload=None):
        return {"operation": operation, "sequence": sequence, "phase": phase,
                "payload": payload if payload is not None else {"pid": 7, "lock_id": {}}}

    def test_success_cannot_arrive_before_lease_and_reader(self):
        channel = Channel(Path("unused"), "synthetic-operation")
        with self.assertRaises(ProtocolError):
            channel.accept(self.event("SUCCESS", payload={"media_status": "success", "baseline_unchanged": True}))

    def test_payload_unknown_fields_and_wrong_types_are_refused(self):
        for payload in ({"pid": True, "lock_id": {}}, {"pid": 7, "lock_id": {}, "authority": True}):
            with self.subTest(payload=payload), self.assertRaises(ProtocolError):
                Channel(Path("unused"), "synthetic-operation").accept(self.event(payload=payload))

    def test_duplicate_partial_oversized_and_nonfinite_json_are_rejected(self):
        for data in (b'{"operation":"a","operation":"b"}', b'{"operation":',
                     b" " * (MAX_JSON + 1), b'{"a":NaN}'):
            with self.subTest(size=len(data)), self.assertRaises(ProtocolError):
                strict_json(data)

    def test_wrong_operation_stale_unknown_and_late_success_are_refused(self):
        for event in (self.event(operation="wrong"), self.event(sequence=0), self.event("UNKNOWN")):
            with self.subTest(event=event), self.assertRaises(ProtocolError):
                Channel(Path("unused"), "synthetic-operation").accept(event)
        channel = Channel(Path("unused"), "synthetic-operation")
        channel.cancelled = True
        with self.assertRaises(ProtocolError):
            channel.accept(self.event("SUCCESS", payload={"media_status": "success", "baseline_unchanged": True}))

    def test_oversized_file_is_bounded_and_partial_file_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-native-json-") as directory:
            path = Path(directory) / "message.json"
            for content in (b"x" * (MAX_JSON + 100), b'{"sequence":'):
                path.write_bytes(content)
                with self.assertRaises(ProtocolError):
                    read_json(path)

    def test_cancel_rejects_success_even_after_valid_lease_and_reader(self):
        channel = Channel(Path("unused"), "synthetic-operation")
        channel.accept(self.event(payload={"pid": 7, "lock_id": {
            "volume_serial": 1, "file_id_128": "a" * 32, "link_count": 1}}))
        channel.accept(self.event("READER", sequence=2, payload={"reader_pid": 8}))
        channel.cancelled = True
        with self.assertRaises(ProtocolError):
            channel.accept(self.event("SUCCESS", sequence=3,
                                      payload={"media_status": "success", "baseline_unchanged": True}))


class StartupCleanupTests(unittest.TestCase):
    def test_unsupported_creation_binding_fails_closed_without_assignment_fallback(self):
        ownership = diagnostic.CleanupState()
        temp, root, _ = create_operation("normal")
        real = kernel_api()
        calls = []
        class UnsupportedCreation:
            def __getattr__(self, name):
                if name == "AssignProcessToJobObject":
                    raise AssertionError("INSECURE_ASSIGN_FALLBACK")
                return getattr(real, name)
            def CreateProcessW(self, *args):
                calls.append("creation-binding-attempt")
                ctypes.set_last_error(50)
                return 0
        try:
            with patch("_track2_native_supervisor.kernel_api", return_value=UnsupportedCreation()):
                with self.assertRaisesRegex(Blocked, "CreateProcessW_JOB_LIST:WinError=50"):
                    NativeJob(root / "request.json", cleanup=ownership)
            self.assertEqual(["creation-binding-attempt"], calls)
            self.assertFalse((root / "evidence/event-001.json").exists())
            print("INJECTED_UNSUPPORTED_API=WinError50; no process created; no assignment fallback", flush=True)
        finally:
            finish_startup_operation(temp, ownership)


class CleanupFailureSafetyTests(unittest.TestCase):
    """Injected failure branches only. No child/native process is launched here."""

    def exercise(self, failure, *, pre_ack=False):
        temp, root, request = create_operation("pre_resume_death")
        self.addCleanup(temp.cleanup)  # Safe: the Popen below is entirely a double.
        previous_latch = diagnostic._UNSAFE_REASON
        self.addCleanup(setattr, diagnostic, "_UNSAFE_REASON", previous_latch)
        diagnostic._UNSAFE_REASON = None
        calls = []
        helper = Mock(pid=202)
        helper.poll.return_value = None
        helper.wait.side_effect = subprocess.TimeoutExpired("synthetic-owned-helper", 5)
        helper.kill.side_effect = OSError("synthetic-kill-failed") if failure == "kill" else None
        helper._handle.Close.side_effect = lambda: calls.append("popen-close")
        if failure == "close":
            helper.poll.return_value = 0
            helper.wait.side_effect = None
            helper.wait.return_value = 0
        event = {"phase": "CREATED", "payload": {"pid": 303, "supervisor_pid": 404, "suspended": True}}
        def close(_kernel, handle):
            calls.append(handle)
            if failure == "close" and handle == 303:
                raise OSError("synthetic-observer-close-failed")
        with patch.object(diagnostic, "token_facts", return_value={"elevated": False}), \
             patch.object(diagnostic, "create_operation", return_value=(temp, root, request)), \
             patch.object(diagnostic, "kernel_api", return_value=Mock(TerminateProcess=Mock(return_value=1))), \
             patch.object(diagnostic, "fixed_command", return_value=["SYNTHETIC-NOT-EXECUTED"]), \
             patch.object(diagnostic.subprocess, "Popen", return_value=helper) as launch, \
             patch.object(diagnostic.Channel, "next", side_effect=RuntimeError("synthetic-no-ack") if pre_ack else None,
                          return_value=event), \
             patch.object(diagnostic, "open_observer", side_effect=lambda _k, pid, **_kw: pid), \
             patch.object(diagnostic, "process_signaled", side_effect=([False, False] + [True] * 10)
                          if failure == "close" else None, return_value=False), \
             patch.object(diagnostic, "close_handle", side_effect=close), \
             patch.object(temp, "cleanup", wraps=temp.cleanup) as cleanup:
            result = run_diagnostic("pre_resume_death")
            cleanup_count = cleanup.call_count
            retained = root.exists()
            first_calls = list(calls)
            second = run_diagnostic("pre_resume_death")
            launch_count = launch.call_count
        return result, second, first_calls, cleanup_count, retained, launch_count

    def test_kill_error_without_magic_word_retains_tree_and_latches_next_launch(self):
        result, second, calls, cleanup_count, retained, count = self.exercise("kill")
        self.assertEqual(0, cleanup_count, "uncertain child ownership must never delete its tree")
        self.assertTrue(retained)
        self.assertEqual("BLOCKED", result["status"])
        self.assertFalse(result["cleanup_complete"])
        self.assertEqual(1, count, "latched second request must not launch")
        self.assertEqual("BLOCKED", second["status"])
        self.assertNotIn("retained_root", result, "public diagnostic record must omit private temp path")

    def test_wait_timeout_still_attempts_every_observer_and_popen_close(self):
        result, _, calls, cleanup_count, retained, count = self.exercise("wait")
        self.assertCountEqual([303, 404, "popen-close"], calls)
        self.assertEqual(0, cleanup_count)
        self.assertTrue(retained)
        self.assertEqual(1, count)
        self.assertEqual("BLOCKED", result["status"])

    def test_one_observer_close_error_cannot_skip_remaining_handle_closes(self):
        result, _, calls, cleanup_count, retained, _ = self.exercise("close")
        self.assertCountEqual([303, 404, "popen-close"], calls)
        self.assertEqual(0, cleanup_count)
        self.assertTrue(retained)
        self.assertEqual("BLOCKED", result["status"])

    def test_pre_ack_failure_retains_unknown_tree_and_stops_further_launch(self):
        result, _, calls, cleanup_count, retained, count = self.exercise("kill", pre_ack=True)
        self.assertEqual(0, cleanup_count)
        self.assertTrue(retained)
        self.assertEqual(["popen-close"], calls)
        self.assertEqual(1, count)
        self.assertEqual("BLOCKED", result["status"])

    def test_latched_stop_blocks_direct_native_job_before_any_kernel_call(self):
        with patch.object(diagnostic, "_UNSAFE_REASON", "synthetic-fault-latch"), \
             patch.object(diagnostic, "kernel_api", side_effect=AssertionError("FORBIDDEN_OS_ADMISSION")) as api:
            caught = None
            try:
                NativeJob(Path("unused-request"))
            except BaseException as error:
                caught = error
            self.assertIsInstance(caught, Blocked)
            self.assertEqual(0, api.call_count)

    def test_latched_stop_blocks_direct_death_helper_before_popen(self):
        temp, root, request = create_operation("pre_resume_death")
        self.addCleanup(temp.cleanup)
        with patch.object(diagnostic, "_UNSAFE_REASON", "synthetic-fault-latch"), \
             patch.object(diagnostic, "fixed_command", return_value=["SYNTHETIC-NOT-EXECUTED"]), \
             patch.object(diagnostic.subprocess, "Popen", side_effect=AssertionError("FORBIDDEN_POPEN")) as launch:
            caught = None
            try:
                diagnostic._run_death(root, request, {})
            except BaseException as error:
                caught = error
            self.assertIsInstance(caught, Blocked)
            self.assertEqual(0, launch.call_count)


class StartupRealCleanupTests(unittest.TestCase):

    def test_injected_post_creation_error_reaps_real_suspended_process(self):
        ownership = diagnostic.CleanupState()
        temp, root, _ = create_operation("normal")
        observed = []
        real_close = NativeJob.close
        def observing_close(job):
            had_process = bool(job.process)
            real_close(job)
            observed.append((had_process, job.reaped, job.job, job.process, job.thread))
        try:
            with patch.object(NativeJob, "close", observing_close):
                with self.assertRaisesRegex(Blocked, "INJECTED_AFTER_CREATE"):
                    NativeJob(root / "request.json", fail_at="after_create", cleanup=ownership)
            self.assertEqual([(True, True, None, None, None)], observed)
            self.assertFalse((root / "evidence/event-001.json").exists())
            print("INJECTED_STARTUP_CLEANUP=actual_suspended_process_reaped_AND_jobActive0", flush=True)
        finally:
            finish_startup_operation(temp, ownership)

    def test_injected_pre_creation_error_closes_job_without_child(self):
        ownership = diagnostic.CleanupState()
        temp, root, _ = create_operation("normal")
        observed = []
        real_close = NativeJob.close
        def observing_close(job):
            observed.append(bool(job.process))
            real_close(job)
            self.assertIsNone(job.job)
        try:
            with patch.object(NativeJob, "close", observing_close):
                with self.assertRaisesRegex(Blocked, "INJECTED_AFTER_JOB"):
                    NativeJob(root / "request.json", fail_at="after_job", cleanup=ownership)
            self.assertEqual([False], observed)
        finally:
            finish_startup_operation(temp, ownership)


class StartupWrapperRetentionTests(unittest.TestCase):
    """Run the real outer test methods with a no-process constructor failure."""

    def test_all_real_startup_wrappers_retain_their_tree_on_unproven_cleanup(self):
        cases = (
            (StartupRealCleanupTests, "test_injected_post_creation_error_reaps_real_suspended_process"),
            (StartupRealCleanupTests, "test_injected_pre_creation_error_closes_job_without_child"),
            (StartupCleanupTests, "test_unsupported_creation_binding_fails_closed_without_assignment_fallback"),
        )
        for owner, method in cases:
            with self.subTest(wrapper=method):
                temp, root, request = create_operation("normal")
                seen = []
                def unproven_constructor(_job, _request, *, cleanup=None, **_options):
                    # Fault below the actual outer wrapper; absolutely no OS job/process.
                    state = cleanup if cleanup is not None else diagnostic.CleanupState()
                    state.started()
                    state.fail()
                    seen.append(state)
                    raise diagnostic.CleanupUncertain("synthetic-reap-failed")
                try:
                    with patch.object(diagnostic, "_UNSAFE_REASON", None), \
                         patch(__name__ + ".create_operation", return_value=(temp, root, request)), \
                         patch.object(NativeJob, "__init__", new=unproven_constructor), \
                         patch.object(temp, "cleanup", wraps=temp.cleanup) as cleanup:
                        caught = None
                        try:
                            getattr(owner(), method)()
                        except BaseException as error:
                            caught = error
                        self.assertIsNotNone(caught, "the inner real test must not pass an unproven failure")
                        self.assertEqual(1, len(seen))
                        self.assertFalse(seen[0].safe)
                        self.assertEqual(0, cleanup.call_count, "outer wrapper must not delete an unproven operation")
                        self.assertTrue(root.exists())
                        self.assertFalse(temp._finalizer.alive, "implicit finalizer must also be detached")
                        self.assertIsNotNone(diagnostic._UNSAFE_REASON)
                finally:
                    # This regression constructor never launches, so test-only reclamation is known safe.
                    temp.cleanup()
