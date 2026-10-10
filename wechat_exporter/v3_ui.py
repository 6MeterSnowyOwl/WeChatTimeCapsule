"""Pure T9 selection, presentation and request refusal; no operational service.

Supplied DTOs and cached labels are unauthenticated metadata. This controller
never opens a profile, discovers sources, runs a callback, mutates a binding,
or issues a capability. Successful metadata comparisons remain unavailable
requests until genuine reviewed runtime/lease/approval/persistence producers
exist. This module supplies no widgets or native termination evidence.
"""
from dataclasses import dataclass
from pathlib import Path
import re

from .candidate_lifecycle import admit_candidate_action
from .fork_authorization import _typed, validate_formal_activation
from .track2_contracts import (ActivationApproval, CandidateRecord, ForkReceipt,
    Track2Error, VerificationReceipt)


@dataclass(frozen=True, repr=False)
class Selection:
    """Explicit lexical selection only; namespace is not a physical proof."""
    namespace: Path
    object_id: str
    candidate_id: str
    purpose: str


@dataclass(frozen=True)
class RequestResult:
    """Presentation result only, never a permission, permit or service receipt."""
    code: str


@dataclass(frozen=True, repr=False)
class CandidateView:
    object_id: str
    candidate_id: str
    state: str
    kind: str
    requests: tuple[str, ...]
    preserved_v2: str = "PRESERVED_V2_READONLY"
    normal_save_authorized: bool = False
    operations_available: bool = False


_PRESENTATION = {
    "BUILDING": ("CANDIDATE", ("build",)),
    "READY": ("CANDIDATE", ("verify", "request_activation")),
    "ACTIVATION_PENDING": ("CANDIDATE", ("recover",)),
    "ACTIVE": ("ACTIVE_REFERENCE", ("verify",)),
    "FAILED": ("CANDIDATE", ("inspect_evidence", "request_fresh_candidate")),
    "RECOVERY_REQUIRED": ("CANDIDATE", ("recover",)),
}


def _selection_valid(selection):
    try:
        if type(selection) is not Selection or not isinstance(selection.namespace, Path):
            return False
        text = str(selection.namespace)
        text.encode("utf-8", errors="strict")
        if (not selection.namespace.is_absolute() or ".." in selection.namespace.parts
                or any(ord(character) < 32 for character in text)):
            return False
        for value in (selection.object_id, selection.candidate_id):
            if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
                return False
            if value.upper() in {"CON", "PRN", "AUX", "NUL",
                    *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))}:
                return False
        return type(selection.purpose) is str and selection.purpose in ("SYNTHETIC", "REHEARSAL", "FORMAL")
    except (ValueError, TypeError, AttributeError):
        return False


class V3Controller:
    """Requests validate supplied metadata and then refuse unavailable execution.

No service argument or callback is accepted. Thus cancellation performs no
service call; a cached ACTIVE/READY state cannot independently allow a save.
"""
    def __init__(self, selection):
        if not _selection_valid(selection):
            raise ValueError("INVALID_SELECTION")
        self.selection = selection

    def _candidate_error(self, candidate):
        if not _selection_valid(self.selection):
            return RequestResult("INVALID_SELECTION")
        if (not _typed(candidate, CandidateRecord)
                or candidate.object_id != self.selection.object_id
                or candidate.archive_id != self.selection.candidate_id
                or candidate.purpose != self.selection.purpose):
            return RequestResult("BINDING_CONFLICT")
        return None

    def present(self, candidate):
        error = self._candidate_error(candidate)
        if error is not None:
            return error
        kind, requests = _PRESENTATION[candidate.state]
        return CandidateView(candidate.object_id, candidate.archive_id,
                             candidate.state, kind, requests)

    def _request(self, candidate, action, **metadata):
        error = self._candidate_error(candidate)
        if error is not None:
            return error
        try:
            admit_candidate_action(candidate, action, **metadata)
        except Track2Error as caught:
            return RequestResult(caught.code)
        return RequestResult("OPERATION_UNAVAILABLE")

    def request_build(self, candidate):
        return self._request(candidate, "build")

    def request_verify(self, candidate):
        return self._request(candidate, "verify")

    def request_activation(self, candidate, approval, *, receipt=None, fork=None, confirmed=False):
        if confirmed is False:
            return RequestResult("CANCELLED")
        error = self._candidate_error(candidate)
        if error is not None:
            return error
        if (confirmed is not True or candidate.purpose == "REHEARSAL"
                or not _typed(approval, ActivationApproval)
                or not _typed(receipt, VerificationReceipt) or not _typed(fork, ForkReceipt)):
            return RequestResult("ACTIVATION_APPROVAL_REQUIRED")
        if candidate.state != "READY":
            return RequestResult("TRACK2_RECOVERY_REQUIRED" if candidate.state in
                ("ACTIVATION_PENDING", "RECOVERY_REQUIRED") else "CANDIDATE_NOT_READY")
        try:
            validate_formal_activation(approval, receipt, fork)
        except Track2Error as caught:
            return RequestResult(caught.code)
        if (candidate.object_id != fork.object_id or candidate.archive_id != fork.archive_id
                or candidate.scope != fork.scope or candidate.root_id != receipt.root_id
                or candidate.operation_id != fork.operation_id):
            return RequestResult("BINDING_CONFLICT")
        if candidate.purpose != approval.purpose or candidate.purpose != fork.purpose:
            return RequestResult("ACTIVATION_APPROVAL_REQUIRED")
        if candidate.evidence_digest != receipt.evidence_digest or candidate.last_error is not None:
            return RequestResult("CANDIDATE_NOT_READY")
        # No current READY/runtime/lease/approval-origin producer exists here.
        return RequestResult("OPERATION_UNAVAILABLE")

    def request_save(self, candidate, *, mode="quick"):
        error = self._candidate_error(candidate)
        if error is not None:
            return error
        if candidate.state in ("ACTIVATION_PENDING", "RECOVERY_REQUIRED"):
            return RequestResult("TRACK2_RECOVERY_REQUIRED")
        # Valid quick/full mode still cannot supply a live binding or permit.
        return RequestResult("RUNTIME_BINDING_UNVERIFIED")

    def request_recovery(self, candidate, *, intent_operation_id=None,
                         intent_digest=None, expected_intent_digest=None):
        return self._request(candidate, "recover", intent_operation_id=intent_operation_id,
            intent_digest=intent_digest, expected_intent_digest=expected_intent_digest)

    def request_boundary(self, boundary):
        # Each real StageB/C/release/delivery approval is independently absent.
        return RequestResult("NOT_AUTHORIZED")
