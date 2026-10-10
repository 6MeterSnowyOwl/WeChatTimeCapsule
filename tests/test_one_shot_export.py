"""Catches lost archive bodies, source incompleteness, nonlocal media and unsafe writes."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from PIL import Image
from tests.one_shot_fixtures import cli_args, file_hashes, make_fixture
from wechat_exporter.history_contracts import ArchiveScope
from wechat_exporter.integrity import verify_current_capsule
from wechat_exporter.v3_state import decode_state

PROJECT = Path(__file__).resolve().parents[1]
CLI = PROJECT / 'scripts/export_full_history_once.py'


class OneShotTests(unittest.TestCase):
    def run_cli(self, args):
        self.assertTrue(CLI.is_file(), 'one-shot CLI must produce a verified full-history capsule')
        return subprocess.run([sys.executable, str(CLI), *args], cwd=PROJECT,
            capture_output=True, text=True, encoding='utf8', timeout=90)

    def test_subprocess_complete_union_media_readback_and_logical_repeat(self):
        with TemporaryDirectory() as directory:
            fixture = make_fixture(Path(directory) / 'input')
            before = file_hashes(fixture.root)
            logical = []
            for name in ('first', 'second'):
                output = Path(directory) / name
                result = self.run_cli(cli_args(fixture, output))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                facts = json.loads(result.stdout)
                self.assertEqual(facts['status'], 'SYNTHETIC_READY')
                check = verify_current_capsule(output)
                self.assertTrue(check.ok and check.manifest_present, check.issues)
                schema = json.loads((output / 'state/schema.json').read_bytes())
                rows = [json.loads(line) for line in (output / 'state/messages.jsonl').read_text(encoding='utf8').splitlines()]
                bundle = decode_state(json.dumps({'schema': schema, 'records': rows}).encode(), ArchiveScope('synthetic-account', 'synthetic-conversation'))
                records = bundle.to_core_records()
                self.assertEqual(len(records), 6)
                self.assertEqual([r.message.create_time for r in records], [1700000010,1700000020,1700000025,1700000030,1700000040,1700000050])
                self.assertEqual([r.identity for r in records[:5]], ['server:101','server:102','server:105','server:103','server:104'])
                self.assertEqual(records[2].message.content, 'legacy body retained')
                self.assertEqual(records[5].message.content, 'archive only retained')
                self.assertTrue(records[5].identity.startswith('archive:'))
                metadata = json.loads((output / 'capsule_meta.json').read_bytes())
                self.assertEqual(metadata['counts']['raw_source_count'], 6)
                self.assertEqual(metadata['counts']['canonical_duplicate_count'], 1)
                self.assertEqual(metadata['counts']['legacy_matched_count'], 2)
                assets = {p.name: p.read_bytes() for p in (output / 'assets').rglob('*') if p.is_file()}
                self.assertEqual(sorted(assets.values()), sorted([fixture.old_bytes,fixture.new_bytes,fixture.video_bytes]))
                for path in (output / 'assets/images').iterdir():
                    with Image.open(path) as image:
                        image.verify()
                markdown = (output / 'chat.md').read_text(encoding='utf8')
                self.assertIn('legacy body retained', markdown)
                self.assertNotIn('source body differs', markdown)
                self.assertIn('assets/videos/', markdown)
                logical.append([(r.identity if r.identity.startswith('server:') else 'archive:opaque', r.message.to_json_dict(), r.media_asset) for r in records])
            self.assertEqual(logical[0], logical[1])
            self.assertEqual(file_hashes(fixture.root), before)

    def test_incomplete_shards_and_existing_output_fail_without_mutation(self):
        with TemporaryDirectory() as directory:
            fixture = make_fixture(Path(directory) / 'input')
            raw = json.loads(fixture.manifest.read_text())
            raw['shards'].pop()
            fixture.manifest.write_text(json.dumps(raw))
            before = file_hashes(fixture.root)
            output = Path(directory) / 'output'
            result = self.run_cli(cli_args(fixture, output))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('INCOMPLETE_SOURCE_SET', result.stdout)
            self.assertFalse(output.exists())
            self.assertEqual(file_hashes(fixture.root), before)
            output.mkdir()
            (output / 'sentinel').write_bytes(b'retain')
            result = self.run_cli(cli_args(fixture, output))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((output / 'sentinel').read_bytes(), b'retain')

    def test_real_mode_rejected_before_input_io(self):
        result = self.run_cli(['--mode','real','--account','unused','--conversation','unused',
            '--archive','missing','--source-manifest','missing','--media-root','missing','--output','missing'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('REAL_PROVIDER_UNAVAILABLE', result.stdout)

    def test_broken_output_symlink_cannot_redirect_publication(self):
        with TemporaryDirectory() as directory:
            fixture = make_fixture(Path(directory) / 'input')
            output, redirected = Path(directory) / 'output', Path(directory) / 'redirected'
            os.symlink(redirected, output, target_is_directory=True)
            before = file_hashes(fixture.root)
            result = self.run_cli(cli_args(fixture, output))
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertFalse(redirected.exists())
            self.assertTrue(output.is_symlink())
            self.assertEqual(file_hashes(fixture.root), before)

    def test_identity_conflict_fails_before_publication(self):
        with TemporaryDirectory() as directory:
            fixture = make_fixture(Path(directory) / 'input')
            with closing(sqlite3.connect(fixture.source / 'message/message_1.db')) as connection:
                connection.execute('UPDATE Msg_synthetic SET server_id=101 WHERE local_id=1')
                connection.commit()
            before = file_hashes(fixture.root)
            output = Path(directory) / 'output'
            result = self.run_cli(cli_args(fixture, output))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('IDENTITY_INVARIANT_CONFLICT', result.stdout)
            self.assertFalse(output.exists())
            self.assertEqual(file_hashes(fixture.root), before)

    def test_output_inside_source_is_rejected(self):
        with TemporaryDirectory() as directory:
            fixture = make_fixture(Path(directory) / 'input')
            before = file_hashes(fixture.root)
            output = fixture.source / 'new-output'
            result = self.run_cli(cli_args(fixture, output))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('OVERLAPPING_ROOTS', result.stdout)
            self.assertFalse(output.exists())
            self.assertEqual(file_hashes(fixture.root), before)

    def test_hardlinked_input_is_rejected(self):
        with TemporaryDirectory() as directory:
            fixture = make_fixture(Path(directory) / 'input')
            os.link(fixture.archive / 'assets/images/old.png', Path(directory) / 'shared-image')
            before = file_hashes(fixture.root)
            output = Path(directory) / 'output'
            result = self.run_cli(cli_args(fixture, output))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('SHARED_FILE_IDENTITY', result.stdout)
            self.assertFalse(output.exists())
            self.assertEqual(file_hashes(fixture.root), before)

    def test_later_old_asset_cannot_overwrite_new_canonical_image(self):
        with TemporaryDirectory() as directory:
            fixture = make_fixture(Path(directory) / 'input', colliding_archive_asset=True)
            before = file_hashes(fixture.root)
            output = Path(directory) / 'output'
            result = self.run_cli(cli_args(fixture, output))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            schema = json.loads((output / 'state/schema.json').read_bytes())
            rows = [json.loads(line) for line in (output / 'state/messages.jsonl').read_text(encoding='utf8').splitlines()]
            records = decode_state(json.dumps({'schema': schema, 'records': rows}).encode(),
                ArchiveScope('synthetic-account', 'synthetic-conversation')).to_core_records()
            new = next(record for record in records if record.identity == 'server:103')
            old = next(record for record in records if record.binding_status == 'archive_only')
            new_reference = 'assets/images/' + new.media_asset
            old_reference = 'assets/images/' + old.media_asset
            self.assertEqual((output / new_reference).read_bytes(), fixture.new_bytes)
            self.assertEqual((output / old_reference).read_bytes(), fixture.old_bytes)
            self.assertNotEqual(new_reference, old_reference)
            markdown = (output / 'chat.md').read_text(encoding='utf8')
            self.assertIn('![图片](' + new_reference + ')', markdown)
            self.assertIn('![图片](' + old_reference + ')', markdown)
            self.assertEqual(file_hashes(fixture.root), before)

    def test_dotdot_output_rejected_before_any_protected_write(self):
        with TemporaryDirectory() as directory:
            fixture = make_fixture(Path(directory) / 'input')
            alias = Path(directory) / 'alias'
            alias.mkdir()
            output = alias / '..' / 'input' / 'archive-v2' / 'out'
            before = file_hashes(fixture.root)
            # The audit hook observes actual mkdir/open attempts in the CLI
            # process; final tree hashes alone would miss cleaned-up staging.
            wrapper = '''import os, pathlib, runpy, sys
protected = pathlib.Path(sys.argv[1]).resolve()
def audit(event, args):
    writing = event == "os.mkdir"
    if event == "open":
        writing = bool(args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
    if writing and isinstance(args[0], (str, bytes, os.PathLike)):
        path = pathlib.Path(os.fsdecode(args[0])).resolve()
        if path == protected or path.is_relative_to(protected):
            print("PROTECTED_WRITE_ATTEMPT:" + event, flush=True)
sys.addaudithook(audit)
cli = sys.argv[2]
sys.argv = [cli, *sys.argv[3:]]
runpy.run_path(cli, run_name="__main__")
'''
            result = subprocess.run([sys.executable, '-c', wrapper, str(fixture.archive), str(CLI),
                *cli_args(fixture, output)], cwd=PROJECT, capture_output=True,
                text=True, encoding='utf8', timeout=90)
            self.assertNotIn('PROTECTED_WRITE_ATTEMPT:', result.stdout, result.stdout + result.stderr)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('OVERLAPPING_ROOTS', result.stdout)
            self.assertFalse(output.exists())
            self.assertEqual(file_hashes(fixture.root), before)


if __name__ == '__main__':
    unittest.main()
