"""Behavior proof for authenticated one-shot selection and shared Core output."""
from contextlib import closing, redirect_stdout, redirect_stderr
import importlib
import inspect
import io
import json
import struct
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.one_shot_fixtures import file_hashes
from tests.one_shot_provider_fixtures import fixture, kwargs, descriptor_type
from wechat_exporter.history_contracts import ArchiveScope, HistoryError
from wechat_exporter.integrity import verify_current_capsule
from wechat_exporter.one_shot_export import export_full_history_once
from wechat_exporter.v3_state import decode_state


class ProviderBridgeTests(unittest.TestCase):
    def bridge(self):
        self.assertIn('authenticated_db', inspect.signature(export_full_history_once).parameters,
            'real authenticated runtime must reach the existing one-shot Core pipeline')
        return importlib.import_module('wechat_exporter.one_shot_provider')

    def run_export(self, f, output, **changes):
        module = self.bridge()
        options = kwargs(f, output)
        options.update(changes)
        with patch.object(module, 'RealDescriptor', descriptor_type()):
            return export_full_history_once(**options)

    def test_real_shape_two_shards_archive_union_media_and_readonly_inputs(self):
        self.bridge()
        with TemporaryDirectory() as tmp:
            f = fixture(Path(tmp) / 'input')
            before = file_hashes(f.root)
            output = Path(tmp) / 'capsule'
            result = self.run_export(f, output)
            self.assertEqual(result['status'], 'REAL_BRIDGE_READY')
            self.assertEqual(result['message_count'], 6)
            self.assertTrue(verify_current_capsule(output).ok)
            rows = [json.loads(x) for x in (output / 'state/messages.jsonl').read_text('utf8').splitlines()]
            self.assertEqual(len(rows), 6)
            text = (output / 'chat.md').read_text('utf8')
            self.assertIn('legacy body retained', text)
            assets = [p.read_bytes() for p in (output / 'assets').rglob('*') if p.is_file()]
            self.assertEqual(sorted(assets), sorted([f.old_bytes, f.new_bytes, f.video_bytes]))
            self.assertEqual(before, file_hashes(f.root))
            self.assertEqual(list(f.work.iterdir()), [])
            self.assertFalse(Path(f.runtime.base_db.__dict__['workdir']).exists())
            meta = json.loads((output / 'capsule_meta.json').read_bytes())
            self.assertEqual(meta['counts']['earliest_timestamp'], 1700000010)
            self.assertEqual(meta['counts']['latest_timestamp'], 1700000050)

    def test_all_existing_labels_and_card_subtypes_survive_decoder_and_render(self):
        self.bridge()
        cases = [(1,'plain text'),(34,'voice'),(47,'sticker'),(48,'<location poiname="Park" label="Place" x="1" y="2"/>'),
            (42,'<msg nickname="Card Person" alias="alias"/>'),(10000,'system text')]
        for subtype in ('4','5','6','33','57','62','999'):
            cases.append((49, '<msg><appmsg><type>' + subtype + '</type><title>Title ' + subtype + '</title><url>https://example.org</url><refermsg><type>1</type><content>quoted text</content></refermsg></appmsg></msg>'))
        with TemporaryDirectory() as tmp:
            f = fixture(Path(tmp) / 'input')
            with closing(sqlite3.connect(f.source / 'message/message_1.db')) as c:
                for i, (kind, content) in enumerate(cases, 20):
                    c.execute('INSERT INTO "' + f.table + '" VALUES (?,?,?,?,?,?,?,?,?,?)',
                        (i,kind,1000+i,2,1700000100+i,content,b'',b'',None,i))
                c.commit()
            output = Path(tmp) / 'capsule'
            result = self.run_export(f, output)
            self.assertEqual(result['message_count'], 19)
            meta = json.loads((output / 'capsule_meta.json').read_bytes())
            self.assertEqual(meta['coverage'], {'fully_supported':11,'partial':4,'placeholder':4})
            rows = [json.loads(x) for x in (output/'state/messages.jsonl').read_text('utf8').splitlines()]
            schema = json.loads((output/'state/schema.json').read_bytes())
            records = decode_state(json.dumps({'schema':schema,'records':rows}).encode(),
                ArchiveScope('synthetic-account','synthetic-conversation')).to_core_records()
            self.assertEqual({r.message.message_type for r in records}, {'文本','图片','视频','语音',
                '动画表情','位置','联系人名片','系统消息','链接卡片','文件','小程序卡片','引用消息','拍一拍','文件/链接/卡片'})
            self.assertEqual({r.message.subtype for r in records if r.message.subtype}, {'4','5','6','33','57','62','999'})
            text = (output / 'chat.md').read_text('utf8')
            for content in ('plain text','system text','Park','Card Person','Title 4','Title 5','Title 6','Title 33','Title 57'):
                self.assertIn(content, text)
            for label in ('语音消息','动画表情','拍一拍消息','卡片消息'): self.assertIn(label, text)

    def test_unknown_type_opaque_text_bad_schema_and_read_error_abort(self):
        self.bridge()
        for mutation, expected in [('type','UNSUPPORTED_SOURCE_SCHEMA'),('opaque','SOURCE_READ_FAILED'),
                ('schema','UNSUPPORTED_SOURCE_SCHEMA'),('read','SOURCE_READ_FAILED')]:
            with self.subTest(mutation=mutation), TemporaryDirectory() as tmp:
                f = fixture(Path(tmp) / 'input')
                path = f.source / 'message/message_1.db'
                if mutation == 'read':
                    path.write_bytes(b'not sqlite')
                else:
                    with closing(sqlite3.connect(path)) as c:
                        if mutation == 'type': c.execute('UPDATE "'+f.table+'" SET local_type=9876 WHERE local_id=1')
                        if mutation == 'opaque': c.execute('UPDATE "'+f.table+'" SET message_content=? WHERE local_id=1',(b'\xff\xfe',))
                        if mutation == 'schema': c.execute('ALTER TABLE "'+f.table+'" DROP COLUMN source')
                        c.commit()
                before = file_hashes(f.root)
                output = Path(tmp) / 'capsule'
                with self.assertRaises(HistoryError) as error: self.run_export(f, output)
                self.assertEqual(error.exception.code, expected)
                self.assertFalse(output.exists())
                self.assertEqual(before, file_hashes(f.root))

    def test_complete_shard_and_key_inventory_required(self):
        self.bridge()
        for mutation in ('shard','key'):
            with self.subTest(mutation=mutation), TemporaryDirectory() as tmp:
                f = fixture(Path(tmp) / 'input')
                changes = {'expected_shards': ('message/message_0.db',)} if mutation=='shard' else {}
                if mutation=='key': f.runtime.base_db._keys.pop('message/message_1.db')
                with self.assertRaises(HistoryError): self.run_export(f, Path(tmp)/'capsule', **changes)
                self.assertEqual(list(f.work.iterdir()), [])

    def test_binding_and_confirmation_refuse_before_selected_io(self):
        self.bridge()
        for changes in ({'account':'wrong'}, {'conversation':'wrong'}, {'source_root':Path('missing')},
                {'real_read_confirmed':False}, {'origin_confirmed':False}):
            with self.subTest(changes=changes), TemporaryDirectory() as tmp:
                f = fixture(Path(tmp) / 'input')
                with patch('pathlib.Path.lstat', side_effect=AssertionError('SELECTED_IO')):
                    with self.assertRaises((HistoryError, ValueError)):
                        self.run_export(f, Path(tmp)/'capsule', **changes)

    def test_work_output_and_media_domains_cannot_overlap_protected_data(self):
        self.bridge()
        for change in ('work','output','image'):
            with self.subTest(change=change), TemporaryDirectory() as tmp:
                f = fixture(Path(tmp) / 'input')
                before = file_hashes(f.root)
                extra = {'work_root':f.archive} if change=='work' else {'image_domain':f.source} if change=='image' else {}
                output = f.source/'out' if change=='output' else Path(tmp)/'capsule'
                with self.assertRaises((HistoryError, ValueError)): self.run_export(f, output, **extra)
                self.assertEqual(before, file_hashes(f.root))

    def test_programmatic_cli_runtime_and_shell_refusal(self):
        self.bridge()
        cli = importlib.import_module('scripts.export_full_history_once')
        with patch('pathlib.Path.lstat', side_effect=AssertionError('SELECTED_IO')), redirect_stdout(io.StringIO()) as stream:
            status = cli.main(['--mode','real','--account','a','--conversation','c','--archive','missing',
                '--source-manifest','missing','--media-root','missing','--output','missing'])
        self.assertEqual(status, 2)
        self.assertIn('REAL_PROVIDER_UNAVAILABLE', stream.getvalue())
        with self.assertRaises(SystemExit) as error, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()): cli.main([])
        self.assertEqual(error.exception.code, 2)
        with TemporaryDirectory() as tmp:
            f = fixture(Path(tmp) / 'input')
            options = kwargs(f, Path(tmp)/'capsule')
            arguments = []
            for key in ('mode','account','conversation','archive','source_manifest','media_root','output'):
                value = options.pop(key)
                arguments.extend(('--'+key.replace('_','-'), str(value)))
            module = self.bridge()
            with patch.object(module, 'RealDescriptor', descriptor_type()), redirect_stdout(io.StringIO()) as stream:
                status = cli.main(arguments, **options)
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stream.getvalue())['message_count'], 6)
            self.assertTrue(verify_current_capsule(Path(tmp)/'capsule').ok)

    def test_present_corrupt_media_aborts_instead_of_publishing_missing(self):
        self.bridge()
        with TemporaryDirectory() as tmp:
            f = fixture(Path(tmp) / 'input')
            image = next((f.media / 'msg/attach').rglob('*.dat'))
            image.write_bytes(b'corrupt selected image')
            output = Path(tmp) / 'capsule'
            with self.assertRaises(HistoryError): self.run_export(f, output)
            self.assertFalse(output.exists())

    def test_wrapped_image_and_video_types_abort_with_complete_duplicate_observations(self):
        self.bridge()
        for original, wrapped in ((3, 65539), (43, 65579)):
            with self.subTest(raw_type=wrapped), TemporaryDirectory() as tmp:
                f = fixture(Path(tmp) / 'input')
                # Change both shards, preserving the duplicated image's type
                # invariant so this reaches adapter coverage rejection.
                for index in (0, 1):
                    with closing(sqlite3.connect(f.source/f'message/message_{index}.db')) as c:
                        c.execute('UPDATE "'+f.table+'" SET local_type=? WHERE local_type=?', (wrapped,original))
                        c.commit()
                before = file_hashes(f.root)
                output = Path(tmp)/'capsule'
                with self.assertRaises(HistoryError) as error: self.run_export(f, output)
                self.assertEqual(error.exception.code, 'UNSUPPORTED_SOURCE_SCHEMA')
                self.assertFalse(output.exists())
                self.assertEqual(file_hashes(f.root), before)
                self.assertEqual(list(f.work.iterdir()), [])

    def test_present_v1_undisplayable_and_wxgf_fallbacks_abort_before_publication(self):
        self.bridge()
        from wechatauto.media import V1_MAGIC
        for body in (b'not-an-image', b'wxgf-no-hevc-payload'):
            with self.subTest(body=body), TemporaryDirectory() as tmp:
                f = fixture(Path(tmp) / 'input')
                image = next((f.media/'msg/attach').rglob('*.dat'))
                image.write_bytes(V1_MAGIC + b'x'*16 + bytes(x^0x88 for x in body))
                before = file_hashes(f.root)
                output = Path(tmp)/'capsule'
                with self.assertRaises(HistoryError) as error: self.run_export(f, output)
                self.assertEqual(error.exception.code, 'SOURCE_READ_FAILED')
                self.assertFalse(output.exists())
                self.assertEqual(file_hashes(f.root), before)
                self.assertEqual(list(f.work.iterdir()), [])

    def test_successful_v1_image_and_absent_asset_keep_existing_semantics(self):
        self.bridge()
        from wechatauto.media import V1_MAGIC
        for available in (True, False):
            with self.subTest(available=available), TemporaryDirectory() as tmp:
                f = fixture(Path(tmp)/'input')
                image = next((f.media/'msg/attach').rglob('*.dat'))
                if available:
                    image.write_bytes(V1_MAGIC + b'x'*16 + bytes(x^0x88 for x in f.new_bytes))
                else:
                    image.unlink()
                before = file_hashes(f.root)
                output = Path(tmp)/'capsule'
                result = self.run_export(f, output)
                self.assertEqual(result['counts']['media_missing_count'], 0 if available else 1)
                self.assertTrue(verify_current_capsule(output).ok)
                if available:
                    self.assertIn(f.new_bytes, [p.read_bytes() for p in (output/'assets').rglob('*') if p.is_file()])
                self.assertEqual(file_hashes(f.root), before)
                self.assertEqual(list(f.work.iterdir()), [])

    def test_v2_image_inert_cfg_derivation_and_missing_key_stop(self):
        self.bridge()
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from wechatauto.media import MediaDownloader, V2_MAGIC
        for available in (True, False):
            with self.subTest(available=available), TemporaryDirectory() as tmp:
                f = fixture(Path(tmp) / 'input')
                aes, xor = MediaDownloader.derive_image_keys(1234, 'synthetic-account')
                pad = 16 - len(f.new_bytes) % 16
                encrypted = Cipher(algorithms.AES(aes.encode()), modes.ECB()).encryptor()
                payload = encrypted.update(f.new_bytes + bytes([pad]) * pad) + encrypted.finalize()
                image = next((f.media / 'msg/attach').rglob('*.dat'))
                image.write_bytes(V2_MAGIC + struct.pack('<LL', len(f.new_bytes), 0) + b'\x00' + payload)
                if available: f.runtime.base_db.__dict__['cfg_dword'] = 1234
                output = Path(tmp) / 'capsule'
                with patch.object(MediaDownloader, '_scan_aes_key', side_effect=AssertionError('PROCESS_SCAN')), \
                        patch.object(MediaDownloader, '_load_persisted_key', side_effect=AssertionError('SHARED_CACHE')):
                    if available:
                        try: self.run_export(f, output)
                        except HistoryError as error: self.fail('inert cfg must decode selected V2 image: ' + error.code)
                        self.assertIn(f.new_bytes, [p.read_bytes() for p in (output/'assets').rglob('*') if p.is_file()])
                    else:
                        with self.assertRaises(HistoryError): self.run_export(f, output)
                        self.assertFalse(output.exists())

    def test_shared_video_and_unselected_database_contents_are_not_read(self):
        self.bridge()
        with TemporaryDirectory() as tmp:
            f = fixture(Path(tmp) / 'input')
            blocked = [f.source/'unselected.db', f.media/'msg/video/unrelated.mp4']
            for path in blocked: path.write_bytes(b'unselected private synthetic content')
            real_open = Path.open
            def scoped_open(path, *args, **kw):
                if path in blocked: raise AssertionError('UNSELECTED_CONTENT_READ')
                return real_open(path, *args, **kw)
            with patch.object(Path, 'open', scoped_open): self.run_export(f, Path(tmp)/'capsule')

    def test_conversion_temporary_files_stay_in_operation_work_without_global_mutation(self):
        module = self.bridge()
        from types import SimpleNamespace
        import tempfile
        original = tempfile.TemporaryDirectory
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            work, images, videos = root/'work', root/'images', root/'videos'
            for path in (work, images, videos): path.mkdir()
            factory = module.bounded_downloader_factory((images,videos), work,
                SimpleNamespace(image_key=None, xor_key=None, cfg_dword=None, wxid=''), {})
            downloader = factory(SimpleNamespace(workdir=str(work)), str(work))
            downloader._ffmpeg_exe = lambda: 'synthetic-ffmpeg'
            downloader._extract_hevc = lambda value: b'fixture-hevc'
            observed = []
            def convert(args, **options):
                source, destination = Path(args[5]), Path(args[-1])
                observed.extend((source, destination))
                self.assertEqual(source.read_bytes(), b'fixture-hevc')
                destination.write_bytes(b'\xff\xd8\xffsynthetic-jpeg')
                return SimpleNamespace(returncode=0)
            with patch('subprocess.run', side_effect=convert):
                self.assertEqual(downloader._wxgf_to_jpg(b'fixture'), b'\xff\xd8\xffsynthetic-jpeg')
            self.assertEqual(len(observed), 2)
            self.assertTrue(all(path.is_relative_to(work) for path in observed))
            self.assertEqual(list(work.iterdir()), [])
            self.assertIs(tempfile.TemporaryDirectory, original)


if __name__ == '__main__': unittest.main()
