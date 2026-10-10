"""Read-only semantic inspection; requires caller-owned approved root proof.

No physical isolation, source authentication, migration, recovery, lock, or
write authority is established here. Callers must independently approve/prove
the root before inspection and revalidate it before recovery or any write.
"""
from pathlib import Path

from .history_contracts import ArchiveScope, HistoryError
from .track2_contracts import StateBundle, decode_dto
from .v3_state import _decode_document, _load_json, _require


def inspect_v3_schema(root: Path) -> StateBundle:
    """Read joint schema/store state; parsed target metadata grants no permit."""
    try:
        _require(isinstance(root, Path) and root.is_absolute())
        schema = _load_json((root / "state/schema.json").read_bytes())
        _require(type(schema) is dict)
        scope = decode_dto(ArchiveScope, schema["scope"])
        data = (root / "state/messages.jsonl").read_bytes()
        lines = data.splitlines()
        _require(not data or data.endswith(b"\n"))
        _require(all(line.strip() for line in lines))
        records = [_load_json(line) for line in lines]
        bundle = _decode_document({"schema": schema, "records": records}, scope)
        target_path = root / "target.json"
        if target_path.exists():
            target = _load_json(target_path.read_bytes())
            _require(type(target) is dict)
            # Task5 may add display/render metadata here. All codec-owned
            # identity/version/generation fields must be explicit and agree.
            for key in ("schema_version", "identity_contract_version", "archive_id", "scope", "data_generation"):
                _require(key in target and type(target[key]) is type(schema[key]) and target[key] == schema[key])
        return bundle
    except (OSError, ValueError, TypeError, HistoryError, KeyError, AttributeError, RecursionError):
        raise ValueError("INVALID_V3_STATE") from None
