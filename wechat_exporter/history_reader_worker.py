"""Spawned private snapshot worker; no sqlite connection crosses IPC."""
from __future__ import annotations
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import pickle
import uuid
from .history_contracts import (HistoryError, Inventory, InventoryDigests, SourceCount,
    SourceCursor, SourceDescriptor, SourceLocator, SourceRow, SourceSetChanged, snapshot_diagnostic)
from .history_provider import open_frozen
from .messages import MESSAGE_REQUIRED_COLUMNS


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":"),
                                     sort_keys=True).encode()).hexdigest()


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def schema(connection, key, table):
    present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
    columns = [tuple(row) for row in connection.execute(f"PRAGMA table_info({quote(table)})")] if present else []
    # Include indexes and CHECK/foreign-key/unique constraints, not only columns.
    definitions = [tuple(row) for row in connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('table','index') ORDER BY type,name",
        (table,))] if present else []
    return SourceDescriptor(key, table, present, digest([columns,definitions])), columns


def stable_keys(provider, expected):
    try:
        keys = provider.list_sources()
    except OSError:
        raise HistoryError("SOURCE_DISCOVERY_FAILED") from None
    if tuple(item.key for item in expected) != keys:
        raise SourceSetChanged()
    return keys


def optional(provider, name, *args):
    method = getattr(provider, name, None)
    return method(*args) if method else None


def phase(lifecycle, value):
    if lifecycle is not None:
        lifecycle["phase"] = value


def snapshot_completed(lifecycle):
    if lifecycle is not None:
        lifecycle["snapshot_completed"] = True


def run_inventory(provider, root, scope, mode, cursors, page_size, lifecycle=None):
    phase(lifecycle, "source_listing")
    try:
        keys = provider.list_sources()
    except OSError:
        raise HistoryError("SOURCE_DISCOVERY_FAILED") from None
    table = getattr(provider, "target_table", "Msg_" + hashlib.md5(scope.conversation_id.encode()).hexdigest())
    sources, counts, output, next_cursors, images = [], [], [], [], []
    retained, row_hashes = {}, []
    cursor_by_key = {cursor.source_key: cursor for cursor in cursors}
    phase(lifecycle, "metadata_snapshot")
    provider.prepare_decoder()
    if not keys:
        snapshot_completed(lifecycle)
    for index, key in enumerate(keys):
        image = root / f"source-{index}.db"
        try:
            provider.freeze(key.relative_db, image)
        except FileNotFoundError:
            if provider.list_sources() != keys:
                raise SourceSetChanged() from None
            raise
        images.append(str(image))
        with closing(open_frozen(image)) as connection:
            optional(provider, "before_schema")
            description, columns = schema(connection, key, table)
            sources.append(description)
            if not description.target_present:
                if index == len(keys)-1:
                    snapshot_completed(lifecycle)
                counts.append(SourceCount(key, 0, 0, 0, 0))
                continue
            names = [row[1] for row in columns]
            if not set((*MESSAGE_REQUIRED_COLUMNS, "server_id")).issubset(names):
                raise HistoryError("UNSUPPORTED_SOURCE_SCHEMA")
            sql_table = quote(table)
            raw_total = connection.execute(f"SELECT count(*) FROM {sql_table}").fetchone()[0]
            # All requested metadata freezes returned; every shard image has
            # now passed its existing target schema/count admission. This does
            # not assert that row decoding or later stability checks succeeded.
            if index == len(keys)-1:
                snapshot_completed(lifecycle)
            cursor = cursor_by_key.get(key) if mode == "quick" else None
            predicate = "sort_seq >= ?" if cursor else "1"
            parameters = (cursor.high_watermark[0] - cursor.overlap_seq,) if cursor else ()
            invalid = " OR ".join(f"typeof({name}) != 'integer'" for name in ("local_id","create_time","sort_seq","local_type"))
            if connection.execute(f"SELECT 1 FROM {sql_table} WHERE ({predicate}) AND ({invalid}) LIMIT 1", parameters).fetchone():
                raise HistoryError("SOURCE_ROW_INVALID")
            if connection.execute(f"SELECT local_id FROM {sql_table} GROUP BY local_id HAVING count(*)>1 LIMIT 1").fetchone():
                raise HistoryError("SOURCE_ROW_INVALID")
            selected = connection.execute(f"SELECT count(*) FROM {sql_table} WHERE {predicate}", parameters).fetchone()[0]
            override = optional(provider, "selected_count", selected)
            if override is not None:
                selected = override
            enumerated = decoded = 0
            last = None
            class_names = ("server_id","local_id","create_time","sort_seq","local_type")
            projected = ",".join(quote(name) for name in names)
            observed = ",".join(f"typeof({quote(name)})" for name in names)
            while True:
                extra = " AND (sort_seq,create_time,local_id) > (?,?,?)" if last is not None else ""
                args = parameters + (last if last is not None else ()) + (page_size,)
                page = connection.execute(f"SELECT {projected},{observed} FROM {sql_table} WHERE {predicate}{extra} ORDER BY sort_seq,create_time,local_id LIMIT ?", args).fetchall()
                if not page:
                    break
                for values in page:
                    raw = dict(zip(names, tuple(values)[:len(names)]))
                    classes = dict(zip(names, tuple(values)[len(names):]))
                    enumerated += 1
                    server = raw["server_id"]
                    server_text = str(server) if server is not None else ""
                    location = SourceLocator(scope,key,table,str(raw["local_id"]),server_text,
                        raw["create_time"],raw["sort_seq"],raw["local_type"],tuple(classes[n] for n in class_names))
                    token = uuid.uuid4().hex
                    phase(lifecycle, "inventory_decode")
                    output.append(SourceRow(provider.decode_row(raw),server_text,location,token))
                    decoded += 1
                    retained[token] = (index, table, raw["local_id"])
                    lossless = [(name, classes[name],
                                 ["sha256", hashlib.sha256(value).hexdigest()] if isinstance(value,bytes) else value)
                                for name,value in raw.items()]
                    row_hashes.append((asdict(key),lossless))
                    last = (raw["sort_seq"], raw["create_time"], raw["local_id"])
                optional(provider, "after_page")
            if selected != enumerated or selected != decoded:
                raise HistoryError("SOURCE_COUNT_MISMATCH", counts={"raw_selected":selected,"enumerated":enumerated,"decoded":decoded})
            counts.append(SourceCount(key,raw_total,selected,enumerated,decoded))
            if last is not None:
                if cursor:
                    last = max(last, cursor.high_watermark)
                next_cursors.append(SourceCursor(key,last,cursor.overlap_seq if cursor else 1))
            elif cursor:
                next_cursors.append(cursor)
    phase(lifecycle, "source_stability")
    stable_keys(provider, sources)
    inventory = Inventory(scope,mode,tuple(output),tuple(sources),tuple(counts),tuple(next_cursors),
        InventoryDigests(digest([asdict(item) for item in sources]),digest([mode,row_hashes]),digest(asdict(scope))))
    return inventory, images, retained


def run_verification(provider, root, sources, lifecycle=None):
    phase(lifecycle, "source_stability")
    keys = stable_keys(provider, sources)
    for index, (key, previous) in enumerate(zip(keys, sources)):
        image = root / f"verify-{index}.db"
        try:
            provider.freeze(key.relative_db,image)
            with closing(open_frozen(image)) as connection:
                optional(provider, "before_schema")
                current, _ = schema(connection,key,previous.target_table)
            if current != previous:
                raise HistoryError("SOURCE_SCHEMA_CHANGED")
        except Exception:
            # A source may disappear or be replaced after the initial scan.
            # Only unchanged instances can retain a read/schema error; drift
            # belongs to the collection's single restart budget, not this loop.
            stable_keys(provider, sources)
            raise
    stable_keys(provider, sources)
    snapshot_completed(lifecycle)


def run_snapshot_probe(provider, root, scope, lifecycle=None):
    """Freeze and inspect exact selected-table schema/counts without row reads."""
    phase(lifecycle, "source_listing")
    try:
        keys = provider.list_sources()
    except OSError:
        raise HistoryError("SOURCE_DISCOVERY_FAILED") from None
    table = getattr(provider, "target_table", "Msg_" + hashlib.md5(scope.conversation_id.encode()).hexdigest())
    phase(lifecycle, "metadata_snapshot")
    provider.prepare_decoder()
    # The same provider opens its already frozen metadata. Only SQLite's
    # integrity result is requested; no contact or sender rows are queried.
    for relative in getattr(getattr(provider, "descriptor", None), "metadata", ()):
        with closing(provider._open(relative)) as connection:
            if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
                raise HistoryError("SOURCE_READ_FAILED")
    sources, shards = [], []
    for index, key in enumerate(keys):
        image = root / f"source-{index}.db"
        try:
            provider.freeze(key.relative_db, image)
        except FileNotFoundError:
            if provider.list_sources() != keys:
                raise SourceSetChanged() from None
            raise
        phase(lifecycle, "snapshot_probe")
        with closing(open_frozen(image)) as connection:
            if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
                raise HistoryError("SOURCE_READ_FAILED")
            description, columns = schema(connection, key, table)
            sources.append(description)
            raw_total = 0
            if description.target_present:
                if not set((*MESSAGE_REQUIRED_COLUMNS, "server_id")).issubset(row[1] for row in columns):
                    raise HistoryError("UNSUPPORTED_SOURCE_SCHEMA")
                raw_total = connection.execute(f"SELECT count(*) FROM {quote(table)}").fetchone()[0]
            shards.append({"ordinal": index, "target_present": description.target_present,
                           "raw_total": raw_total, "integrity": "verified"})
    phase(lifecycle, "source_stability")
    stable_keys(provider, sources)
    snapshot_completed(lifecycle)
    return {"shards": shards}


def child_main(pipe, descriptor, root, operation, args):
    provider = None
    lifecycle = {"phase": "worker_admission", "worker_initialized": False,
                 "snapshot_started": False, "snapshot_completed": False}
    try:
        if descriptor.version != "1.2.1":
            raise HistoryError("UNSUPPORTED_UPSTREAM_VERSION")
        if descriptor.private is not True:
            raise HistoryError("SOURCE_SNAPSHOT_UNSAFE", diagnostic={
                "phase": "worker_admission", "branch": "worker_not_private"})
        root = Path(root)
        cache = root / "cache"
        cache.mkdir()
        phase(lifecycle, "worker_initialization")
        provider = descriptor.build(cache,verification=operation == "verify")
        lifecycle["worker_initialized"] = True
        original_freeze = provider.freeze
        metadata = getattr(getattr(provider, "descriptor", None), "metadata", ())
        def observed_freeze(relative, destination):
            lifecycle["snapshot_started"] = True
            phase(lifecycle, "snapshot_verification" if operation == "verify" else
                  "metadata_snapshot" if relative.replace("\\", "/") in metadata else "shard_snapshot")
            return original_freeze(relative, destination)
        provider.freeze = observed_freeze
        if operation == "verify":
            result = run_verification(provider,root,*args,lifecycle=lifecycle)
        elif operation == "snapshot_probe":
            result = run_snapshot_probe(provider,root,*args,lifecycle=lifecycle)
            result = {**{name: lifecycle[name] for name in
                ("worker_initialized", "snapshot_started", "snapshot_completed")}, **result}
        else:
            result = run_inventory(provider,root,*args,lifecycle=lifecycle)
        payload = ("ok", result)
    except HistoryError as error:
        summary = error.to_summary()
        summary["diagnostic"] = snapshot_diagnostic({**summary.get("diagnostic", {}),
            **lifecycle, "exception_class": type(error).__name__})
        payload = ("error",summary)
    except BaseException as error:
        payload = ("error",HistoryError("SOURCE_READ_FAILED", diagnostic={
            **lifecycle, "exception_class": type(error).__name__}).to_summary())
    try:
        packed = pickle.dumps(payload,protocol=5)
        for offset in range(0,len(packed),32768):
            pipe.send_bytes(packed[offset:offset+32768])
        pipe.send_bytes(b"")
        if operation == "inventory" and payload[0] == "ok":
            pipe.recv_bytes()  # Provider lease lasts until the owning session closes.
    except (EOFError,OSError):
        pass
    finally:
        if provider is not None:
            provider.close()
        pipe.close()
