"""Explicit synthetic verification and pure returned-path policy only.

No real RuntimeBindingVerifier producer, reader/session/media factory, runner,
native supervisor or writer is implemented here. AuthenticatedHistoryDB is a
caller assertion, not authentication. The re-exported origin helper validates
metadata only and still needs an independent reviewed human-confirmation flow.
"""
import math

from .fork_authorization import authorize_archive_origin
from .history_contracts import ArchiveScope, HistoryError, SourceSetChanged
from .track2_contracts import RuntimeProof, Track2Error, V3Profile


def verify_synthetic_runtime(profile: V3Profile, base_db: object,
                             scope: ArchiveScope, verifier: object) -> RuntimeProof:
    """Validate one explicitly injected synthetic proof without reading a source.

An explicit profile DTO declares test purpose; it is not live namespace/lease
authority. Non-synthetic profiles are rejected before touching the verifier.
Proofs remain process objects: no serialization, reconstruction or discovery.
"""
    denied = "RUNTIME_BINDING_UNVERIFIED"
    try:
        if type(profile) is not V3Profile or type(scope) is not ArchiveScope:
            raise Track2Error(denied)
        profile.__post_init__()
        scope.__post_init__()
        if profile.purpose != "SYNTHETIC":
            raise Track2Error(denied)
        verify = getattr(verifier, "verify", None)
        if not callable(verify):
            raise Track2Error(denied)
        proof = verify(base_db, scope)
        if type(proof) is not RuntimeProof:
            raise Track2Error(denied)
        proof.__post_init__()
        if type(proof.scope) is not ArchiveScope or proof.scope != scope or proof.synthetic is not True:
            raise Track2Error(denied)
        proof.scope.__post_init__()
    except Exception:
        raise Track2Error(denied) from None
    return proof


def ensure_returned_before_deadline(deadline: float, returned_at: float) -> None:
    """Reject a returned result at/past the existing attempt deadline.

Caller supplies monotonic observations from that same existing attempt. This
does not start/reset a deadline, cancel a call, close resources or prove OS
termination. A nonreturning synchronous native call cannot reach this check.
"""
    try:
        valid = (type(deadline) in (int, float) and type(returned_at) in (int, float)
            and math.isfinite(deadline) and math.isfinite(returned_at)
            and deadline > 0 and 0 <= returned_at < deadline)
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise HistoryError("SOURCE_READ_TIMEOUT")


def source_retry_allowed(error: object, *, retries_used: int, t7_prepared: bool,
                         durable_vault_installed: bool) -> bool:
    """Pure eligibility only: one source-drift retry before both durable edges.

No retry loop, transaction recovery, session cleanup or termination is owned
here. Supplied flags do not prove a transaction's state or resource release.
The caller's existing Core failure remains untouched when eligibility is false.
"""
    return (type(error) is SourceSetChanged and error.code == "SOURCE_SET_CHANGED"
        and type(retries_used) is int and retries_used == 0
        and type(t7_prepared) is bool and t7_prepared is False
        and type(durable_vault_installed) is bool and durable_vault_installed is False)
