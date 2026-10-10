"""Synthetic in-memory ownership policy tests; no writer or permit fixtures."""
from contextlib import ExitStack
import copy
import unittest
from unittest.mock import patch

from wechat_exporter.track2_contracts import Track2Error
from wechat_exporter.v3_capsule import plan_current_managed_leaves


FIXED = ("state/messages.jsonl", "state/schema.json", "archive_state.json",
         "target.json", "chat.md", "capsule_meta.json", "integrity/current.json")


class ManagedLeafPlannerTests(unittest.TestCase):
    def plan(self, new=(), existing=(), prior=()):
        return plan_current_managed_leaves(
            new_current_assets=new, existing_ordinary_leaves=existing,
            prior_current_asset_references=prior)

    def refuse(self, new=(), existing=(), prior=()):
        with self.assertRaises(Track2Error) as caught:
            self.plan(new, existing, prior)
        self.assertEqual(caught.exception.code, "TRACK2_RECOVERY_REQUIRED")
        self.assertEqual(str(caught.exception), "TRACK2_RECOVERY_REQUIRED")
        self.assertIsNone(caught.exception.__cause__)

    def test_empty_inputs_return_exact_seven_metadata_leaves(self):
        self.assertEqual(self.plan(), FIXED)

    def test_exact_existing_metadata_preimages_remain_managed(self):
        self.assertEqual(self.plan(existing=FIXED), FIXED)

    def test_only_new_current_assets_are_appended(self):
        self.assertEqual(self.plan(("assets/z.png", "assets/a/one.jpg")),
                         FIXED + ("assets/a/one.jpg", "assets/z.png"))

    def test_input_permutations_keep_deterministic_output(self):
        wanted = FIXED + ("assets/A.png", "assets/z.png")
        self.assertEqual(self.plan(["assets/z.png", "assets/A.png"],
                                  ["target.json", "chat.md"], ["assets/old.png"]), wanted)
        self.assertEqual(self.plan(["assets/A.png", "assets/z.png"],
                                  ["chat.md", "target.json"], ["assets/old.png"]), wanted)

    def test_existing_asset_is_never_replaced(self):
        self.refuse(("assets/old.png",), ("assets/old.png",))

    def test_prior_reference_is_refused_even_when_file_absent(self):
        self.refuse(("assets/old.png",), (), ("assets/old.png",))

    def test_prior_and_existing_exact_overlap_is_allowed(self):
        self.assertEqual(self.plan(("assets/new.png",), ("assets/old.png",),
                                  ("assets/old.png",)), FIXED + ("assets/new.png",))

    def test_case_aliased_existing_or_prior_asset_is_refused(self):
        self.refuse(("assets/a.png",), ("assets/A.png",))
        self.refuse(("assets/a.png",), (), ("assets/A.png",))

    def test_duplicate_and_case_aliased_names_in_each_input_are_refused(self):
        for names in (("assets/a.png", "assets/a.png"),
                      ("assets/a.png", "assets/A.png")):
            for operand in ("new", "existing", "prior"):
                with self.subTest(operand=operand, names=names):
                    self.refuse(**{operand: names})

    def test_file_parent_collisions_within_each_input_are_refused(self):
        for operand in ("new", "existing", "prior"):
            with self.subTest(operand=operand):
                self.refuse(**{operand: ("assets/a", "assets/a/child.png")})

    def test_new_assets_cannot_use_existing_file_or_directory_leaf(self):
        for new, existing in ((("assets/a/child.png",), ("assets/a",)),
                              (("assets/a",), ("assets/a/child.png",)),
                              (("assets/a.png",), ("assets",))):
            with self.subTest(new=new, existing=existing):
                self.refuse(new, existing)

    def test_new_assets_cannot_collide_with_prior_file_parent(self):
        self.refuse(("assets/a/child.png",), (), ("assets/a",))
        self.refuse(("assets/a",), (), ("assets/a/child.png",))

    def test_component_case_aliases_are_refused_within_or_across_inputs(self):
        self.refuse(("assets/Dir/a.png", "assets/dir/b.png"))
        self.refuse(("assets/Dir/a.png",), ("assets/dir/b.png",))
        self.refuse(("assets/Dir/a.png",), (), ("assets/dir/b.png",))
        self.refuse(existing=("state/assets/a.png", "State/other.json"))

    def test_existing_alias_or_parent_of_fixed_metadata_is_refused(self):
        for name in ("CHAT.MD", "STATE/schema.json", "state", "chat.md/child",
                     "INTEGRITY/old.json", "integrity/current.json/child"):
            with self.subTest(name=name):
                self.refuse(existing=(name,))

    def test_historical_and_vault_leaves_are_excluded_from_managed_output(self):
        existing = ("snapshot_index.json", "snapshots/one/chat.md",
                    "integrity/one.json", "state/assets/old.png", "manifest.json")
        self.assertEqual(self.plan(existing=existing), FIXED)

    def test_proposals_outside_exact_current_assets_namespace_are_refused(self):
        for name in ("snapshot_index.json", "snapshots/one/chat.md", "integrity/one.json",
                     "state/assets/a.png", "manifest.json", "state/schema.json", "chat.md",
                     "assets", "Assets/a.png", "other/a.png"):
            with self.subTest(name=name):
                self.refuse((name,))

    def test_prior_reference_inventory_only_accepts_current_asset_leaves(self):
        for name in ("chat.md", "state/assets/a.png", "snapshots/one/a.png", "Assets/a.png", "assets"):
            with self.subTest(name=name):
                self.refuse(prior=(name,))

    def test_traversal_absolute_backslash_ads_and_special_spellings_are_refused(self):
        names = ("/assets/a.png", "C:/assets/a.png", "assets/../a.png", "assets/./a.png",
                 "assets//a.png", "assets/a.png/", "assets\\a.png", "assets/a.png:stream",
                 "//server/share/a.png", "\\\\?\\C:\\assets\\a.png", "assets/a?.png", "assets/a*.png",
                 "assets/a|b.png", "assets/<a>.png", "assets/a\"b.png", "assets/A~1.png",
                 "assets/a. ", "assets/a.", "assets/a ", "assets/a\x00.png", "assets/a\n.png",
                 ".synthetic-transaction/a", ".SYNTHETIC-TRANSACTION/a")
        for name in names:
            for operand in ("new", "existing", "prior"):
                with self.subTest(operand=operand, name=name):
                    self.refuse(**{operand: (name,)})

    def test_windows_reserved_devices_are_refused_in_every_component(self):
        for device in ("CON", "con.txt", "PRN", "AUX", "NUL.png", "COM0", "COM9.txt",
                       "LPT0", "LPT9", "COM¹", "COM².txt", "LPT³", "CLOCK$", "CONIN$", "CONOUT$"):
            for name in ("assets/" + device, "assets/" + device + "/leaf.png"):
                for operand in ("new", "existing", "prior"):
                    with self.subTest(operand=operand, name=name):
                        self.refuse(**{operand: (name,)})

    def test_invalid_containers_are_refused_without_iterating_them(self):
        for value in (None, "assets/a.png", b"\xff", {"assets/a.png"}, {"a": 1}, 1, True):
            for operand in ("new", "existing", "prior"):
                with self.subTest(operand=operand, value=value):
                    self.refuse(**{operand: value})

    def test_invalid_names_and_malformed_utf8_bytes_are_sanitized(self):
        for value in (None, 1, False, (), {}, b"assets/\xff.png", b"assets/\xed\xa0\x80.png", ""):
            for operand in ("new", "existing", "prior"):
                with self.subTest(operand=operand, value=value):
                    self.refuse(**{operand: (value,)})

    def test_high_and_low_surrogate_strings_are_sanitized(self):
        for name in ("assets/\ud800.png", "assets/\udfff.png", "assets/\ud800\udfff.png"):
            for operand in ("new", "existing", "prior"):
                with self.subTest(operand=operand, name=name):
                    self.refuse(**{operand: (name,)})

    def test_valid_cjk_and_emoji_names_are_preserved(self):
        self.assertEqual(self.plan(("assets/雪鸮/😀.png",), ("snapshots/旧/chat.md",),
                                  ("assets/旧/🦉.png",)), FIXED + ("assets/雪鸮/😀.png",))

    def test_inputs_remain_unchanged_on_success_and_refusal(self):
        new, existing, prior = ["assets/new.png"], ["assets/old.png"], ["assets/old.png"]
        before = copy.deepcopy((new, existing, prior))
        self.assertEqual(self.plan(new, existing, prior), FIXED + ("assets/new.png",))
        self.assertEqual((new, existing, prior), before)
        new.append("assets/old.png")
        before = copy.deepcopy((new, existing, prior))
        self.refuse(new, existing, prior)
        self.assertEqual((new, existing, prior), before)

    def test_planning_and_refusal_perform_no_filesystem_io(self):
        # A future filesystem probe or write would fail before a result can be returned.
        targets = ("builtins.open", "os.open", "os.stat", "os.lstat", "os.listdir", "os.scandir",
                   "os.mkdir", "os.remove", "os.unlink", "os.rename", "os.replace",
                   "pathlib.Path.open", "pathlib.Path.stat", "pathlib.Path.lstat", "pathlib.Path.exists",
                   "pathlib.Path.iterdir", "pathlib.Path.mkdir", "pathlib.Path.read_bytes",
                   "pathlib.Path.read_text", "pathlib.Path.write_bytes", "pathlib.Path.write_text")
        with ExitStack() as stack:
            for target in targets:
                stack.enter_context(patch(target, side_effect=AssertionError("unexpected filesystem IO")))
            self.assertEqual(self.plan(("assets/new.png",)), FIXED + ("assets/new.png",))
            self.refuse(("assets/old.png",), (), ("assets/old.png",))


if __name__ == "__main__":
    unittest.main()
