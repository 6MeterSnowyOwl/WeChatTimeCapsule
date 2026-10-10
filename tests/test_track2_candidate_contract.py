"""Spec-derived candidate status contract; synthetic DTOs, no filesystem writes.

Status records cannot create or deserialize process write capabilities. All
state expectations are the explicit Spec section 6 literals, not annotations.
"""
import json
import unittest

from wechat_exporter.history_contracts import ArchiveScope
from wechat_exporter.track2_contracts import (
    CandidateRecord, FileIdentity, WritePermit, canonical_bytes, contract_digest,
    decode_dto, dto_document,
)


def synthetic_candidate(state):
    return CandidateRecord(
        archive_id="synthetic-archive", object_id="synthetic-object",
        scope=ArchiveScope("synthetic-account", "synthetic-conversation"),
        purpose="SYNTHETIC", root_id=FileIdentity(7, "01" * 16, 1),
        state=state, evidence_digest=None, last_error=None,
        operation_id="synthetic-operation",
    )


class CandidateStateContractTests(unittest.TestCase):
    def test_all_six_spec_states_construct_and_roundtrip_without_mapping(self):
        # Break caught: rejecting a required state or mapping it to another state.
        for state in ("BUILDING", "READY", "ACTIVATION_PENDING", "ACTIVE", "FAILED", "RECOVERY_REQUIRED"):
            with self.subTest(state=state):
                record, error_name = None, None
                try:
                    record = synthetic_candidate(state)
                except Exception as caught:
                    error_name = type(caught).__name__
                self.assertIsNone(error_name, "Spec state must construct; observed " + str(error_name))
                self.assertEqual(state, record.state)
                encoded = canonical_bytes(record)
                self.assertEqual("track2-v1", json.loads(encoded)["contract_version"])
                self.assertEqual(state, json.loads(encoded)["fields"]["state"])
                restored = decode_dto(CandidateRecord, encoded)
                self.assertEqual(state, restored.state)
                self.assertEqual(record, restored)
                self.assertEqual(contract_digest(record), contract_digest(restored))

    def test_obsolete_and_unknown_states_are_rejected_not_aliased(self):
        # Break caught: accepting BUILT/VERIFIED aliases or unknown status text.
        for state in ("BUILT", "VERIFIED", "UNKNOWN", "ready", "", True, None):
            with self.subTest(state=state):
                with self.assertRaises(ValueError) as caught:
                    synthetic_candidate(state)
                self.assertEqual("INVALID_TRACK2_CONTRACT", str(caught.exception))

    def test_disk_record_requires_explicit_valid_state_never_defaults_active(self):
        # Break caught: missing disk state silently defaults to ACTIVE.
        document = dto_document(synthetic_candidate("BUILDING"))
        del document["fields"]["state"]
        with self.assertRaises(ValueError) as caught:
            decode_dto(CandidateRecord, document)
        self.assertEqual("INVALID_TRACK2_CONTRACT", str(caught.exception))

    def test_active_status_is_not_a_deserializable_write_capability(self):
        # Break caught: loading a status record as a live filesystem permit.
        document = dto_document(synthetic_candidate("ACTIVE"))
        with self.assertRaises(ValueError) as caught:
            decode_dto(WritePermit, document)
        self.assertEqual("INVALID_TRACK2_CONTRACT", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
