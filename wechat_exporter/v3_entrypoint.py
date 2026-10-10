"""Dedicated nonwriting T9 CLI, independent of legacy bootstrap and settings.

No profile/service is constructed, even for valid selections. Execution needs
genuine reviewed producers unavailable in this slice. No JSON approval loader,
implicit roots, arbitrary save-root option or synthetic write bypass exists.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
from typing import Sequence


class _InvalidArguments(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual error text echoes rejected private arguments.
        raise _InvalidArguments("INVALID_SELECTION")


def _parser():
    parser = _Parser(prog="wechat_exporter.v3_entrypoint", allow_abbrev=False,
        description="Explicit Track2 requests; no authority is granted and execution is unavailable.")
    parser.add_argument("action", choices=("noop", "build", "verify", "activate", "save", "recover"))
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--object-id", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--purpose", required=True, choices=("SYNTHETIC", "REHEARSAL", "FORMAL"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Return 0 only for help/no-op; 2 invalid selection; 3 unavailable route."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("EXPLICIT_SELECTION_REQUIRED", file=sys.stderr)
        return 2
    try:
        if any(type(value) is not str for value in arguments):
            raise _InvalidArguments("INVALID_SELECTION")
        for value in arguments:
            value.encode("utf-8", errors="strict")
        parsed = _parser().parse_args(arguments)
        # Lazy pure-policy import; never open_v3_profile, service or registry.
        from .v3_ui import Selection, _selection_valid
        selection = Selection(Path(parsed.namespace), parsed.object_id, parsed.candidate_id, parsed.purpose)
        if not _selection_valid(selection):
            raise _InvalidArguments("INVALID_SELECTION")
    except SystemExit as caught:
        return 0 if caught.code == 0 else 2
    except (ValueError, TypeError):
        print("INVALID_SELECTION", file=sys.stderr)
        return 2
    if parsed.action == "noop":
        print("NOOP_NO_AUTHORITY")
        return 0
    print("ACTIVATION_APPROVAL_REQUIRED" if parsed.action == "activate"
          else "OPERATION_UNAVAILABLE", file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
