from __future__ import annotations

import multiprocessing
import os
import re
from multiprocessing.connection import Connection

from .account import create_database_for_account
from .compatibility import CapabilityProbeResult, ProbeErrorCode, RuntimeCapability
from .contacts import CONTACT_REQUIRED_COLUMNS
from .messages import MESSAGE_REQUIRED_COLUMNS


DEFAULT_PROBE_TIMEOUT_SECONDS = 15.0
_MESSAGE_TABLE_PATTERN = re.compile(r"^Msg_[0-9a-f]{32}$", re.IGNORECASE)


def _failure(code: ProbeErrorCode) -> CapabilityProbeResult:
    return CapabilityProbeResult(RuntimeCapability.PROBE_FAILED, code)


def _account_display_name(info: dict) -> str:
    """Resolve an ephemeral self label for the GUI; never persist or log it."""
    for key in ("remark", "nick_name", "nickname", "username"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return ""


def _table_columns(connection, table_name: str) -> set[str]:
    if table_name != "contact" and not _MESSAGE_TABLE_PATTERN.fullmatch(table_name):
        raise ValueError("unexpected schema table name")
    escaped = table_name.replace('"', '""')
    rows = connection.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    return {str(row[1]) for row in rows}


def probe_account_database(db) -> CapabilityProbeResult:
    """Probe only identity/schema metadata through upstream read-only handles."""
    try:
        info = db.get_self_info()
        if not isinstance(info, dict):
            return _failure(ProbeErrorCode.CONTACT_QUERY_FAILED)
        account_display_name = _account_display_name(info)
    except Exception:
        return _failure(ProbeErrorCode.CONTACT_QUERY_FAILED)

    open_db = getattr(db, "_open", None)
    db_files = getattr(db, "_db_files", None)
    message_dbs = getattr(db, "_message_dbs", None)
    if not callable(open_db) or not isinstance(db_files, (list, tuple)) or not callable(message_dbs):
        return _failure(ProbeErrorCode.DATABASE_DISCOVERY_FAILED)

    contact_relative = next(
        (
            relative
            for relative, source, *_ in db_files
            if os.path.basename(str(source)).casefold() == "contact.db"
        ),
        None,
    )
    if not contact_relative:
        return _failure(ProbeErrorCode.DATABASE_DISCOVERY_FAILED)

    try:
        connection = open_db(contact_relative)
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='contact' LIMIT 1"
            ).fetchone()
            columns = _table_columns(connection, "contact") if exists else set()
            if exists:
                connection.execute("SELECT 1 FROM contact LIMIT 1").fetchone()
        finally:
            connection.close()
    except Exception:
        return _failure(ProbeErrorCode.DATABASE_OPEN_FAILED)
    if not set(CONTACT_REQUIRED_COLUMNS).issubset(columns):
        return _failure(ProbeErrorCode.CONTACT_SCHEMA_UNSUPPORTED)

    try:
        paths = tuple(message_dbs())
    except Exception:
        return _failure(ProbeErrorCode.DATABASE_DISCOVERY_FAILED)
    if not paths:
        return _failure(ProbeErrorCode.DATABASE_DISCOVERY_FAILED)

    saw_message_table = False
    for relative_path in paths:
        try:
            connection = open_db(relative_path)
            try:
                row = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name GLOB 'Msg_*' LIMIT 1"
                ).fetchone()
                if not row:
                    continue
                table_name = str(row[0])
                if not _MESSAGE_TABLE_PATTERN.fullmatch(table_name):
                    continue
                saw_message_table = True
                columns = _table_columns(connection, table_name)
                escaped = table_name.replace('"', '""')
                connection.execute(f'SELECT 1 FROM "{escaped}" LIMIT 1').fetchone()
            finally:
                connection.close()
        except Exception:
            return _failure(ProbeErrorCode.DATABASE_OPEN_FAILED)
        if not set(MESSAGE_REQUIRED_COLUMNS).issubset(columns):
            return _failure(ProbeErrorCode.MESSAGE_SCHEMA_UNSUPPORTED)
        return CapabilityProbeResult(
            RuntimeCapability.READY,
            account_display_name=account_display_name,
        )

    if not saw_message_table:
        return _failure(ProbeErrorCode.MESSAGE_SCHEMA_UNSUPPORTED)
    return CapabilityProbeResult(
        RuntimeCapability.READY,
        account_display_name=account_display_name,
    )


def probe_account(stable_id: str) -> CapabilityProbeResult:
    try:
        db, _backend = create_database_for_account(stable_id)
    except Exception:
        return _failure(ProbeErrorCode.DATABASE_OPEN_FAILED)
    return probe_account_database(db)


def _probe_child(send_connection: Connection, stable_id: str) -> None:
    try:
        result = probe_account(stable_id)
        send_connection.send(result.to_payload())
    except BaseException:
        try:
            send_connection.send(
                _failure(ProbeErrorCode.INTERNAL_PROBE_ERROR).to_payload()
            )
        except BaseException:
            pass
    finally:
        send_connection.close()


def _stop_process(process, *, terminate_first: bool) -> None:
    if not terminate_first and process.is_alive():
        process.join(2.0)
    if process.is_alive():
        process.terminate()
        process.join(2.0)
    if process.is_alive():
        process.kill()
        process.join()
    elif process.exitcode is None:
        process.join()


def probe_account_with_timeout(
    stable_id: str,
    *,
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    child_target=None,
) -> CapabilityProbeResult:
    """Run the probe in a spawned process and always reap it before returning."""
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    process = context.Process(
        target=child_target or _probe_child,
        args=(send_connection, stable_id),
        name="wechat-capability-probe",
    )
    timed_out = False
    started = False
    try:
        process.start()
        started = True
        send_connection.close()
        if not receive_connection.poll(max(0.0, timeout_seconds)):
            timed_out = True
            return _failure(ProbeErrorCode.TIMEOUT)
        try:
            return CapabilityProbeResult.from_payload(receive_connection.recv())
        except (EOFError, OSError, ValueError, KeyError):
            return _failure(ProbeErrorCode.INTERNAL_PROBE_ERROR)
    except (OSError, RuntimeError):
        return _failure(ProbeErrorCode.INTERNAL_PROBE_ERROR)
    finally:
        receive_connection.close()
        send_connection.close()
        if started:
            _stop_process(process, terminate_first=timed_out)
        try:
            process.close()
        except (ValueError, OSError):
            pass
