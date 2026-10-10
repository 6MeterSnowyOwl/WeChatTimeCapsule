"""Synthetic I10-I15 migration; no serializer, target, settings or real DB."""
from copy import deepcopy
from dataclasses import asdict, replace
import unittest
from uuid import UUID

from history_fixtures import identity_inventory, identity_row, legacy_record
from wechat_exporter.history_contracts import ArchiveScope, HistoryError, ScopeAuthorization
from wechat_exporter.identity_migration import plan_identity_migration
from wechat_exporter.message_identity import resolve_canonical_rows


class HistoricalMigrationTests(unittest.TestCase):
    def migrate(self, records, inventory=None, authorization=None):
        inventory = inventory if inventory is not None else identity_inventory()
        authorization = authorization or ScopeAuthorization(inventory.scope, True)
        return plan_identity_migration(tuple(records), inventory, authorization)

    def assert_error(self, code, records, inventory=None, authorization=None):
        before = deepcopy(tuple(asdict(record) for record in records))
        with self.assertRaises(HistoryError) as caught:
            self.migrate(records, inventory, authorization)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(tuple(asdict(record) for record in records), before)

    def test_unique_legacy_binding_preserves_body_media(self):
        old = legacy_record()
        before = deepcopy(asdict(old))
        inventory = identity_inventory(identity_row())
        plan = self.migrate((old,), inventory)
        self.assertEqual(plan.legacy_matched_count, 1)
        self.assertEqual(plan.newly_discovered_count, 0)
        self.assertEqual(plan.archive_only_count, 0)
        self.assertEqual(len(plan.records), 1)
        mapped = plan.records[0]
        self.assertEqual(mapped.canonical_identity, "server:101")
        self.assertEqual(mapped.binding_status, "server_bound")
        self.assertEqual(mapped.scope, inventory.scope)
        self.assertIs(mapped.message, old.message)
        self.assertEqual((mapped.media_status, mapped.media_asset, mapped.media_kind),
                         ("success", "synthetic-archived.png", "images"))
        self.assertEqual(asdict(old), before)
        self.assertEqual(mapped.source_refs, (inventory.rows[0].locator,))
        self.assertEqual(mapped.technical_observations, ({"raw_local_type": 1,
                                                        "storage_classes": ("integer",) * 5},))
        self.assertIs(resolve_canonical_rows(inventory, plan.records).records[0].message, old.message)

    def test_multiple_legacy_candidates_rejected(self):
        for server in ("101", "102"):
            with self.subTest(same_server=server == "101"):
                self.assert_error("AMBIGUOUS_LEGACY_BINDING", (legacy_record(),),
                                  identity_inventory(identity_row(), identity_row(source=1, server=server)))

    def test_duplicate_same_source_observation_is_still_two_candidates(self):
        row = identity_row()
        self.assert_error("AMBIGUOUS_LEGACY_BINDING", (legacy_record(),), identity_inventory(row, row))

    def test_missing_legacy_becomes_archive_only(self):
        old = legacy_record()
        first = self.migrate((old,))
        record = first.records[0]
        self.assertTrue((record.canonical_identity or "").startswith("archive:"))
        token = UUID(record.canonical_identity[8:])
        self.assertEqual(token.version, 4)
        self.assertEqual(len(record.canonical_identity[8:]), 32)
        self.assertEqual(record.binding_status, "archive_only")
        self.assertEqual(first.archive_only_count, 1)
        self.assertEqual(first.legacy_matched_count, 0)
        self.assertEqual(first.newly_discovered_count, 0)
        second = self.migrate(first.records)
        self.assertEqual(second.records[0].identity, record.identity)
        self.assertEqual(second.records[0].legacy_aliases, record.legacy_aliases)
        self.assertIs(second.records[0].message, old.message)

    def test_missing_records_get_distinct_opaque_ids(self):
        plan = self.migrate((legacy_record(), legacy_record(local="2", create_time=1700000001)))
        self.assertEqual(len({record.identity for record in plan.records}), 2)
        self.assertTrue(all(record.identity.startswith("archive:") for record in plan.records))

    def test_archive_only_exact_rebind(self):
        first = self.migrate((legacy_record(),))
        promoted = self.migrate(first.records, identity_inventory(identity_row()))
        record = promoted.records[0]
        self.assertEqual(record.identity, "server:101")
        self.assertEqual(promoted.legacy_matched_count, 1)
        self.assertEqual(promoted.archive_only_count, 0)
        self.assertEqual(promoted.newly_discovered_count, 0)
        self.assertIs(record.message, first.records[0].message)
        self.assertEqual(record.legacy_aliases, first.records[0].legacy_aliases)
        self.assertEqual(record.media_asset, first.records[0].media_asset)

    def test_changed_local_id_unresolved_counterpart(self):
        self.assert_error("POSSIBLE_LEGACY_COUNTERPART", (legacy_record(),),
                          identity_inventory(identity_row(local="9")))
        archived_only = self.migrate((legacy_record(),)).records
        self.assert_error("POSSIBLE_LEGACY_COUNTERPART", archived_only,
                          identity_inventory(identity_row(local="9")))

    def test_origin_unconfirmed_rejected_before_mapping(self):
        inventory = identity_inventory(identity_row())
        self.assert_error("LEGACY_SCOPE_UNVERIFIED", (legacy_record(),), inventory,
                          ScopeAuthorization(inventory.scope, False))

    def test_authorization_and_record_scope_are_verified(self):
        other = ArchiveScope("synthetic-other", "synthetic-conversation")
        inventory = identity_inventory(identity_row())
        self.assert_error("IDENTITY_SCOPE_MISMATCH", (legacy_record(),), inventory,
                          ScopeAuthorization(other, True))
        self.assert_error("IDENTITY_SCOPE_MISMATCH", (legacy_record(scope=other),), inventory)
        self.assert_error("IDENTITY_SCOPE_MISMATCH", (legacy_record(),),
                          identity_inventory(identity_row(scope=other)))

    def test_binding_invalid_server_and_alias_conflict(self):
        for number, (server, storage) in enumerate((("0", "integer"), ("", "null"),
                                                   ("1.0", "real"), ("1e3", "text"))):
            with self.subTest(case=number):
                self.assert_error("INVALID_SERVER_ID", (legacy_record(),),
                                  identity_inventory(identity_row(server=server,
                                    storage=(storage,) + ("integer",) * 4)))
        self.assert_error("LEGACY_ALIAS_CONFLICT", (legacy_record(), legacy_record()),
                          identity_inventory(identity_row()))
        self.assert_error("LEGACY_ALIAS_CONFLICT",
                          (legacy_record(), legacy_record(local="9", create_time=1700000001)),
                          identity_inventory(identity_row(), identity_row(local="9", source=1,
                              create_time=1700000001)))

    def test_full_inventory_required_before_uniqueness_claim(self):
        inventory = replace(identity_inventory(identity_row()), mode="quick")
        self.assert_error("FULL_INVENTORY_REQUIRED", (legacy_record(),), inventory)

    def alias(self, *, local="1", create_time=1700000000, sort_seq=10, scope=None):
        return {"scope": scope or identity_inventory().scope, "local_id": local,
                "create_time": create_time, "sort_seq": sort_seq, "identity": "local:" + local}

    def test_original_v2_alias_is_retained_without_input_mutation(self):
        old = legacy_record()
        plan = self.migrate((old,), identity_inventory(identity_row()))
        self.assertEqual(plan.records[0].legacy_aliases, (self.alias(),))
        self.assertEqual(old.legacy_aliases, ())
        again = self.migrate(plan.records, identity_inventory(identity_row()))
        self.assertEqual(again.records[0].legacy_aliases, (self.alias(),))
        self.assertEqual(again.legacy_matched_count, 0)

    def test_alias_scope_and_identity_conflicts_even_without_source(self):
        other = ArchiveScope("synthetic-other", "synthetic-conversation")
        alias = self.alias()
        for number, invalid in enumerate((self.alias(scope=other),
                                          {**alias, "identity": "local:9"},
                                          {**alias, "identity": "server:101"},
                                          {**alias, "sort_seq": True},
                                          {**alias, "local_id": 1},
                                          {key: value for key, value in alias.items() if key != "identity"})):
            with self.subTest(case=number):
                self.assert_error("LEGACY_ALIAS_CONFLICT", (legacy_record(legacy_aliases=(invalid,)),))

    def test_alias_triple_claimed_by_distinct_old_records_rejected(self):
        alias = self.alias()
        self.assert_error("LEGACY_ALIAS_CONFLICT", (
            legacy_record(legacy_aliases=(alias,)),
            legacy_record(local="9", create_time=1700000001, legacy_aliases=(alias,)),
        ))

    def test_alias_candidates_count_observations_without_server_deduplication(self):
        aliases = (self.alias(), self.alias(local="9", create_time=1700000001, sort_seq=11))
        old = legacy_record(legacy_aliases=aliases)
        for server in ("101", "102"):
            with self.subTest(same_server=server == "101"):
                self.assert_error("AMBIGUOUS_LEGACY_BINDING", (old,), identity_inventory(
                    identity_row(), identity_row(local="9", source=1, server=server,
                                                 create_time=1700000001, sort_seq=11)))

    def test_duplicate_alias_does_not_count_same_observation_twice(self):
        alias = self.alias()
        old = legacy_record(legacy_aliases=(alias, deepcopy(alias)))
        plan = self.migrate((old,), identity_inventory(identity_row()))
        self.assertEqual(plan.legacy_matched_count, 1)
        self.assertEqual(plan.records[0].identity, "server:101")

    def test_shared_unresolved_position_with_one_exact_match_is_unsafe(self):
        self.assert_error("POSSIBLE_LEGACY_COUNTERPART", (legacy_record(), legacy_record(local="9")),
                          identity_inventory(identity_row()))

    def test_all_conflicts_are_checked_before_returning_any_mapping(self):
        valid = legacy_record()
        missing = legacy_record(local="9", create_time=1700000010, sort_seq=20,
                                legacy_aliases=({**self.alias(local="9"), "identity": "invalid"},))
        self.assert_error("LEGACY_ALIAS_CONFLICT", (valid, missing), identity_inventory(identity_row()))

    def test_existing_bound_collision_is_not_silently_merged(self):
        row = identity_row()
        bound = legacy_record(local="9", canonical_identity="server:101", scope=row.locator.scope,
                              binding_status="server_bound", source_refs=(row.locator,))
        self.assert_error("LEGACY_ALIAS_CONFLICT", (legacy_record(), bound), identity_inventory(row))

    def test_missing_bound_source_preserves_canonical_and_media(self):
        bound = self.migrate((legacy_record(),), identity_inventory(identity_row())).records[0]
        plan = self.migrate((bound,))
        self.assertEqual(plan.records[0].identity, "server:101")
        self.assertIs(plan.records[0].message, bound.message)
        self.assertEqual(plan.records[0].source_refs, bound.source_refs)
        self.assertEqual(plan.records[0].media_asset, bound.media_asset)
        self.assertEqual(plan.legacy_matched_count, 0)
        self.assertEqual(plan.archive_only_count, 0)

    def test_promotion_does_not_bypass_historical_observation_gate(self):
        old = legacy_record(technical_observations=({"raw_local_type": 49,
                                                    "storage_classes": ("integer",) * 5},))
        self.assert_error("UNVERIFIED_IDENTITY_INVARIANT", (old,), identity_inventory(identity_row()))

    def test_incomplete_full_inventory_cannot_prove_binding(self):
        inventory = identity_inventory(identity_row())
        for changes in (dict(counts=()), dict(sources=()),
                        dict(counts=(replace(inventory.counts[0], raw_total=2),))):
            with self.subTest(fields=tuple(changes)):
                self.assert_error("SOURCE_COUNT_MISMATCH", (legacy_record(),),
                                  replace(inventory, **changes))

    def test_raw_and_decoded_technical_positions_must_agree(self):
        row = identity_row()
        for field, value in (("local_id", "9"), ("create_time", 1700000001), ("sort_seq", 11)):
            with self.subTest(field=field):
                self.assert_error("SOURCE_IDENTITY_MISMATCH", (legacy_record(),),
                                  identity_inventory(replace(row, message=replace(row.message, **{field: value}))))
        self.assert_error("SOURCE_IDENTITY_MISMATCH", (legacy_record(),),
                          identity_inventory(replace(row, locator=replace(row.locator,
                                                                         expected_server_id="102"))))

    def test_same_server_technical_conflicts_use_t3_policy(self):
        first = identity_row()
        for field, value in (("create_time", 1700000001), ("sort_seq", 11)):
            with self.subTest(field=field):
                self.assert_error("IDENTITY_INVARIANT_CONFLICT", (legacy_record(),),
                                  identity_inventory(first, identity_row(local="9", source=1, **{field: value})))
        self.assert_error("UNVERIFIED_IDENTITY_INVARIANT", (legacy_record(),),
                          identity_inventory(first, identity_row(local="9", source=1, raw_type=49)))

    def test_mapped_legacy_supplies_authority_for_relocated_representation(self):
        old = legacy_record()
        inventory = identity_inventory(identity_row(), identity_row(local="9", source=1,
                                                                   content="synthetic alternate"))
        plan = self.migrate((old,), inventory)
        self.assertEqual(plan.legacy_matched_count, 1)
        self.assertEqual(len(plan.records), 1)
        self.assertIs(plan.records[0].message, old.message)
        self.assertEqual(len(plan.records[0].source_refs), 2)

    def test_migration_returns_old_records_only_with_zero_new_count(self):
        old = legacy_record()
        plan = self.migrate((old,), identity_inventory(identity_row(), identity_row(
            local="9", source=1, server="102", create_time=1700000010, sort_seq=20)))
        self.assertEqual(len(plan.records), 1)
        self.assertEqual(plan.newly_discovered_count, 0)
        self.assertIs(plan.records[0].message, old.message)


if __name__ == "__main__":
    unittest.main()
