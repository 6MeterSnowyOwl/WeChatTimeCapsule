"""Thin selection of an already authenticated runtime; no credential discovery."""
from dataclasses import dataclass, replace
import builtins
import hashlib
import os
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory
from types import FunctionType, SimpleNamespace

from .history_contracts import HistoryError
from .history_provider import (AuthenticatedHistoryDB, PrivateCapability, PrivateProvider,
    capability_descriptor, require_plain_path)


def overlaps(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('INVALID_EXPLICIT_ROOT')
    return path.absolute()


@dataclass(frozen=True, repr=False)
class RealDescriptor:
    capability: PrivateCapability
    expected_shards: tuple[str, ...]
    version: str = '1.2.1'
    private: bool = True

    @property
    def storage_root(self): return self.capability.storage_root

    @property
    def shared_cache_root(self): return self.capability.shared_cache_root

    @property
    def account_id(self): return self.capability.account_id

    def build(self, workdir, *, verification=False):
        return CompletePrivateProvider(self, workdir)


class CompletePrivateProvider(PrivateProvider):
    """Keep the pinned decoder; reject coverage gaps before calling them complete."""
    def __init__(self, descriptor, workdir):
        super().__init__(descriptor.capability, workdir)
        self.expected_shards = descriptor.expected_shards

    def list_sources(self):
        sources = super().list_sources()
        if tuple(item.relative_db for item in sources) != self.expected_shards:
            raise HistoryError('INCOMPLETE_SOURCE_SET')
        return sources

    def decode_row(self, raw):
        # 42 is intentionally parsed by the existing models/message_types path.
        label = self.api._msg_type_name(raw['local_type'])
        if type(label) is int and label != 42:
            raise HistoryError('UNSUPPORTED_SOURCE_SCHEMA', fields=('message_type',))
        if label in ('图片', '视频') and raw['local_type'] != {'图片': 3, '视频': 43}[label]:
            # Existing media restoration supports exact raw 3/43 only. Do not
            # turn wrapped media coverage gaps into fully supported rows.
            raise HistoryError('UNSUPPORTED_SOURCE_SCHEMA', fields=('raw_local_type', 'message_type'))
        message = super().decode_row(raw)
        # Upstream returns a friendly placeholder for undecodable binary data.
        # Media/voice/sticker have explicit placeholder coverage; opaque textual
        # bodies cannot count as fully decoded source rows.
        if isinstance(raw['message_content'], bytes) and label in ('文本', '系统消息'):
            if message.content == '[' + str(label) + ']':
                raise HistoryError('SOURCE_READ_FAILED', fields=('content',))
        if message.payload and message.payload.get('parse_status') == 'malformed':
            raise HistoryError('SOURCE_READ_FAILED', fields=('content', 'payload'))
        return message


def select_real_provider(*, authenticated_db, account, conversation, source_root,
                         media_root, expected_shards, metadata_paths, image_domain,
                         video_domain, origin_confirmed, real_read_confirmed):
    """All memory binding/consent checks precede selected-root filesystem access.

    origin_confirmed is migration identity evidence. It never grants a read;
    the caller must independently supply operation-level real_read_confirmed.
    """
    if authenticated_db is None:
        raise ValueError('REAL_PROVIDER_UNAVAILABLE')
    if real_read_confirmed is not True:
        raise ValueError('REAL_READ_CONFIRMATION_REQUIRED')
    if origin_confirmed is not True:
        raise ValueError('ORIGIN_CONFIRMATION_REQUIRED')
    if not isinstance(authenticated_db, AuthenticatedHistoryDB) or authenticated_db.account_id != account:
        raise HistoryError('IDENTITY_SCOPE_MISMATCH')
    if not account or not conversation:
        raise HistoryError('IDENTITY_SCOPE_MISMATCH')
    capability = capability_descriptor(authenticated_db)
    source, media = absolute(source_root), absolute(media_root)
    images, videos = absolute(image_domain), absolute(video_domain)
    if source != capability.storage_root.absolute() or media != source.parent:
        raise HistoryError('IDENTITY_SCOPE_MISMATCH')
    expected_images = media / 'msg/attach' / hashlib.md5(conversation.encode()).hexdigest()
    if images != expected_images or videos != media / 'msg/video':
        raise HistoryError('IDENTITY_SCOPE_MISMATCH')
    if (type(expected_shards) not in (tuple, list) or not expected_shards or
            tuple(expected_shards) != tuple(sorted(set(expected_shards))) or
            any(type(x) is not str or not re.fullmatch(r'message/message_[0-9]+\.db', x) for x in expected_shards)):
        raise HistoryError('INCOMPLETE_SOURCE_SET')
    if (type(metadata_paths) not in (tuple, list) or len(set(metadata_paths)) != len(metadata_paths) or
            any(type(x) is not str or x not in capability.metadata for x in metadata_paths)):
        raise HistoryError('SOURCE_SNAPSHOT_UNSAFE', diagnostic={
            'phase': 'provider_selection', 'branch': 'metadata_selection_invalid'})
    selected = (*expected_shards, *metadata_paths)
    if any(name not in capability.keys for name in selected):
        raise HistoryError('SOURCE_SNAPSHOT_UNSAFE', diagnostic={
            'phase': 'provider_selection', 'branch': 'selection_key_missing'})
    capability = replace(capability, keys={name: capability.keys[name] for name in selected},
        metadata=tuple(metadata_paths))
    # vars() copied values only: no cfg/wxid/account properties on the real db.
    state = vars(authenticated_db.base_db)
    if state.get('account') != account:
        raise HistoryError('IDENTITY_SCOPE_MISMATCH')
    # Reproduce the pinned wxid property's pure string projection using its
    # already-copied account value; never invoke a runtime property/getter.
    wxid = re.sub(r'_\w{4}$', '', state['account'])
    media_db = SimpleNamespace(account_dir=str(media), account=account, wxid=wxid,
        cfg_dword=state.get('cfg_dword'), image_key=state.get('_one_shot_image_key'),
        xor_key=state.get('_one_shot_xor_key'))
    return RealDescriptor(capability, tuple(expected_shards)), media_db, (images, videos)


def validate_real_roots(archive, source, media, domains, work, output, descriptor):
    protected = (archive, source, *domains)
    for path in (*protected, media, work, output.parent):
        require_plain_path(path)
    if not all(path.is_dir() for path in (*protected, work)) or not output.parent.is_dir():
        raise ValueError('INVALID_EXPLICIT_ROOT')
    if output.exists() or output.is_symlink():
        raise ValueError('OUTPUT_ALREADY_EXISTS')
    # The media account parent may naturally own db_storage. Only the selected
    # read domains are mutually disjoint; both write roots are independent.
    if any(overlaps(a, b) for i, a in enumerate(protected) for b in protected[i+1:]):
        raise ValueError('OVERLAPPING_ROOTS')
    if any(overlaps(write, root) for write in (work, output) for root in (archive, media, source)):
        raise ValueError('OVERLAPPING_ROOTS')
    if overlaps(output, work):
        raise ValueError('OVERLAPPING_ROOTS')
    shared = descriptor.shared_cache_root
    if shared is not None and any(overlaps(write, Path(shared).absolute()) for write in (work, output)):
        raise ValueError('OVERLAPPING_ROOTS')
    if work.stat().st_dev != output.parent.stat().st_dev:
        raise ValueError('CROSS_VOLUME_PUBLICATION_UNSUPPORTED')


def selected_source_facts(descriptor):
    """Hash exact selected DB/metadata and sidecars, never db_storage as a tree."""
    from .integrity import sha256_file
    facts = {}
    for name in (*descriptor.expected_shards, *descriptor.capability.metadata):
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = descriptor.storage_root / (name + suffix)
            if not path.exists():
                if not suffix: raise HistoryError('SOURCE_READ_FAILED')
                continue
            require_plain_path(path)
            info = path.stat()
            if not path.is_file() or info.st_nlink != 1:
                raise ValueError('SHARED_FILE_IDENTITY')
            facts[name+suffix] = (info.st_dev, info.st_ino, info.st_size, sha256_file(path))
    return facts


def bounded_downloader_factory(domains, work_root, media_db, selected_facts):
    """Reuse pinned download/decrypt code with local key and path constraints."""
    from wechatauto.media import MediaDownloader
    from .integrity import sha256_file
    from .media import DISPLAYABLE_EXTENSIONS

    def remember(path):
        path = Path(path).absolute()
        if not any(path.is_relative_to(domain) for domain in domains):
            raise HistoryError('SOURCE_SNAPSHOT_UNSAFE', diagnostic={
                'phase': 'media_read', 'branch': 'media_file_outside_domain'})
        role = 'image_source' if path.is_relative_to(domains[0]) else 'video_source'
        try:
            require_plain_path(path)
            info = path.stat()
        except FileNotFoundError:
            # No stat result exists. This later query is a separate observation;
            # a reappearing file is not proof of a stable missing source.
            observed_is_file = path.is_file()
            condition = ('MEDIA_IDENTITY_OBSERVATION_CHANGED' if observed_is_file
                else 'MEDIA_SOURCE_MISSING')
            error = HistoryError('SOURCE_READ_FAILED', diagnostic={
                'phase': 'media_read', 'branch': 'media_file_identity',
                'exception_class': 'FileNotFoundError', 'condition': condition,
                'observed_is_file': observed_is_file})
            error.private_media_observation = dict(path=str(path), role=role,
                st_mode=None, is_regular=None, st_dev=None, st_ino=None, st_nlink=None,
                st_size=None, st_mtime_ns=None, st_file_attributes=None,
                observed_is_file=observed_is_file, condition=condition,
                stat_observation='unavailable',
                is_file_observation='after_missing_path_error')
            raise error from None
        observation = dict(path=str(path),
            role=role, stat_observation='before_path_is_file',
            is_file_observation='subsequent_independent_query',
            st_mode=info.st_mode, is_regular=stat.S_ISREG(info.st_mode),
            st_dev=info.st_dev, st_ino=info.st_ino, st_nlink=info.st_nlink,
            st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
            st_file_attributes=getattr(info, 'st_file_attributes', 0))
        # These are the original two independent observations, not an atomic read.
        observed_is_file = path.is_file()
        # This is a bound, read-only source, not an archive/output identity.
        # Multiple hardlinks do not make reading unsafe. Writable output copies
        # still have their separate one-link/independent-identity admission.
        if not observed_is_file or not observation['is_regular'] or info.st_nlink < 1:
            condition = ('MEDIA_IDENTITY_OBSERVATION_CHANGED'
                if observation['is_regular'] != observed_is_file else
                'MEDIA_NOT_REGULAR' if not observed_is_file else 'MEDIA_LINK_COUNT_UNEXPECTED')
            error = HistoryError('SOURCE_SNAPSHOT_UNSAFE', diagnostic={
                'phase': 'media_read', 'branch': 'media_file_identity',
                'condition': condition, 'observed_is_file': observed_is_file,
                'st_nlink': info.st_nlink})
            observation['observed_is_file'] = observed_is_file
            observation['condition'] = condition
            error.private_media_observation = observation
            raise error
        fact = (info.st_dev, info.st_ino, info.st_size, sha256_file(path))
        if path in selected_facts and selected_facts[path] != fact:
            error = HistoryError('SOURCE_READ_FAILED', diagnostic={
                'phase': 'media_read', 'branch': 'media_file_identity',
                'condition': 'MEDIA_IDENTITY_OBSERVATION_CHANGED',
                'observed_is_file': observed_is_file, 'st_nlink': info.st_nlink})
            observation.update(observed_is_file=observed_is_file,
                condition='MEDIA_IDENTITY_OBSERVATION_CHANGED',
                prior_file_fact=list(selected_facts[path]))
            error.private_media_observation = observation
            raise error
        selected_facts[path] = fact

    def bounded_walk(root):
        root = Path(root).absolute()
        if root not in domains:
            raise HistoryError('SOURCE_SNAPSHOT_UNSAFE', diagnostic={
                'phase': 'media_read', 'branch': 'media_walk_outside_domain'})
        def failed(error): raise HistoryError('SOURCE_READ_FAILED') from None
        for directory, directories, files in os.walk(root, followlinks=False, onerror=failed):
            require_plain_path(Path(directory))
            for name in directories:
                require_plain_path(Path(directory) / name)
            yield directory, directories, files

    def bounded_open(file, mode='r', *args, **kwargs):
        if 'r' in mode and isinstance(file, (str, bytes, os.PathLike)):
            remember(file)
        return builtins.open(file, mode, *args, **kwargs)

    def call_pinned(method, self, *args, bounded_reads=False, **kwargs):
        globals_copy = dict(method.__globals__)
        globals_copy['os'] = SimpleNamespace(**{**vars(os), 'walk': bounded_walk})
        if bounded_reads:
            globals_copy['__builtins__'] = {**vars(builtins), 'open': bounded_open}
        return FunctionType(method.__code__, globals_copy, argdefs=method.__defaults__)(self, *args, **kwargs)

    class BoundedDownloader(MediaDownloader):
        def __init__(self, db, save_dir=None):
            super().__init__(db, save_dir=save_dir, image_key=media_db.image_key)
            self._explicit_aes, self._explicit_xor = media_db.image_key, media_db.xor_key
            if self._explicit_aes is None and type(media_db.cfg_dword) is int and media_db.wxid:
                self._explicit_aes, self._explicit_xor = self.derive_image_keys(media_db.cfg_dword, media_db.wxid)

        def _resolve_aes_key(self):
            return self._explicit_aes  # No process scan, cache read or cfg discovery.

        def _find_dat(self, user, md5, create_time, thumbnail=False):
            return call_pinned(MediaDownloader._find_dat, self, user, md5, create_time, thumbnail)

        def _derive_xor_key(self, dat_path):
            return call_pinned(MediaDownloader._derive_xor_key, self, dat_path, bounded_reads=True)

        def download_image(self, *args, **kwargs):
            try:
                output = super().download_image(*args, **kwargs)
                if output and Path(output).suffix.lower() not in DISPLAYABLE_EXTENSIONS:
                    # Pinned .img/.wxgf fallbacks mean an existing selected
                    # asset could not be rendered; absence is only None.
                    raise HistoryError('SOURCE_READ_FAILED', fields=('content',))
                return output
            except HistoryError:
                raise
            except Exception:
                raise HistoryError('SOURCE_READ_FAILED') from None

        def download_video(self, *args, **kwargs):
            try:
                return call_pinned(MediaDownloader.download_video, self, *args, bounded_reads=True, **kwargs)
            except HistoryError:
                raise
            except Exception:
                raise HistoryError('SOURCE_READ_FAILED') from None

        def _probe_ct(self, dat_path=None):
            if dat_path is None: return b''
            path = Path(dat_path).absolute()
            if not path.is_relative_to(domains[0]):
                raise HistoryError('SOURCE_SNAPSHOT_UNSAFE', diagnostic={
                    'phase': 'media_read', 'branch': 'image_probe_outside_domain'})
            remember(path)
            return super()._probe_ct(str(path))

        def decrypt_image(self, dat_path, aes_key=None, xor_key=None):
            path = Path(dat_path).absolute()
            if not path.is_relative_to(domains[0]):
                raise HistoryError('SOURCE_SNAPSHOT_UNSAFE', diagnostic={
                    'phase': 'media_read', 'branch': 'image_decrypt_outside_domain'})
            remember(path)
            try:
                return super().decrypt_image(str(path), aes_key=self._explicit_aes, xor_key=self._explicit_xor)
            except HistoryError:
                raise
            except Exception:
                raise HistoryError('SOURCE_READ_FAILED') from None

        def _wxgf_to_jpg(self, data):
            # Reuse the upstream function unchanged. Its local import receives a
            # narrow tempfile facade; no global tempdir/env mutation is needed.
            proxy = SimpleNamespace(TemporaryDirectory=lambda *a, **kw:
                TemporaryDirectory(*a, **{**kw, 'dir': str(work_root)}))
            def bounded_import(name, *args, **kwargs):
                return proxy if name == 'tempfile' else builtins.__import__(name, *args, **kwargs)
            globals_copy = dict(MediaDownloader._wxgf_to_jpg.__globals__)
            globals_copy['__builtins__'] = {**vars(builtins), '__import__': bounded_import}
            method = FunctionType(MediaDownloader._wxgf_to_jpg.__code__, globals_copy,
                argdefs=MediaDownloader._wxgf_to_jpg.__defaults__)
            return method(self, data)

    return BoundedDownloader
