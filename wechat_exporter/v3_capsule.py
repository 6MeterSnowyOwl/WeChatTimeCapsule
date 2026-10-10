"""Pure current-capsule managed-leaf name policy; no persistence authority.

Only logical in-memory names are compared. Caller-supplied inventories never
prove actual absence, referenced closure, physical containment or link safety.
Materialization, vault installation and T7 prepare require separate reviewed
root/preimage/integrity/permit evidence; this module performs no filesystem IO.
"""
from .capsule_transaction import _names
from .history_contracts import HistoryError
from .track2_contracts import Track2Error


_FIXED_METADATA = ("state/messages.jsonl", "state/schema.json", "archive_state.json",
                   "target.json", "chat.md", "capsule_meta.json", "integrity/current.json")


def _checked_names(names, *, assets_only=False) -> tuple[str, ...]:
    if type(names) not in (tuple, list):
        raise Track2Error("TRACK2_RECOVERY_REQUIRED")
    copied = tuple(names)
    for name in copied:
        if type(name) is not str:
            raise Track2Error("TRACK2_RECOVERY_REQUIRED")
        # Core's strict Windows grammar does not validate Unicode encodability.
        name.encode("utf8", errors="strict")
        if assets_only and not name.startswith("assets/"):
            raise Track2Error("TRACK2_RECOVERY_REQUIRED")
    return _names(copied) if copied else ()


def _check_component_spellings(names: tuple[str, ...]) -> None:
    spellings = {}
    for name in names:
        parts = name.split("/")
        for depth in range(1, len(parts) + 1):
            prefix = "/".join(parts[:depth])
            key = prefix.casefold()
            if key in spellings and spellings[key] != prefix:
                raise Track2Error("TRACK2_RECOVERY_REQUIRED")
            spellings[key] = prefix


def plan_current_managed_leaves(*, new_current_assets, existing_ordinary_leaves,
                               prior_current_asset_references) -> tuple[str, ...]:
    """Return the fixed seven metadata leaves plus sorted append-only assets.

    All three inventories are required independent comparison operands, supplied
    as plain lists/tuples of strict relative UTF-8 names. Existing metadata may
    have exact preimages; existing/prior-referenced current assets never become
    replacements, including a prior reference whose leaf is currently absent.
    Historical and vault leaves in the existing inventory remain unmanaged.
    Invalid inputs produce only a stable sanitized Track2Error.
    """
    try:
        new = _checked_names(new_current_assets, assets_only=True)
        existing = _checked_names(existing_ordinary_leaves)
        prior = _checked_names(prior_current_asset_references, assets_only=True)
        # Exact overlaps between fixed metadata/existing/prior are legitimate.
        # Core owns duplicates, full-name aliases and file-parent conflicts.
        occupied = tuple(dict.fromkeys(_FIXED_METADATA + existing + prior))
        _names(occupied + new)
        # Shared directories also require one exact spelling across operands.
        _check_component_spellings(occupied + new)
        return _FIXED_METADATA + tuple(sorted(new))
    except (HistoryError, UnicodeError, TypeError, ValueError):
        raise Track2Error("TRACK2_RECOVERY_REQUIRED") from None
