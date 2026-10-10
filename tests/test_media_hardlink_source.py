"""Real generated hardlinks are legal inputs, never shared writable outputs."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tests.one_shot_fixtures import file_hashes
from tests.one_shot_provider_fixtures import fixture, kwargs, PlaintextDescriptor
from wechat_exporter import one_shot_provider
from wechat_exporter.history_contracts import HistoryError
from wechat_exporter.integrity import verify_current_capsule
from wechat_exporter.one_shot_export import export_full_history_once


class HardlinkedSourceOutputTests(unittest.TestCase):
    def test_complete_pipeline_copies_hardlinked_sources_and_preserves_old_media(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            f = fixture(root / 'input')
            options = kwargs(f, root / 'output')
            source_ids = set()
            sources = []
            for domain in (options['image_domain'], options['video_domain']):
                for source in domain.rglob('*'):
                    if source.is_file():
                        alias = f.root / ('source-alias-' + str(len(sources)))
                        os.link(source, alias)
                        info = source.stat()
                        self.assertEqual(info.st_nlink, 2)
                        source_ids.add((info.st_dev, info.st_ino))
                        sources.append(source)
            self.assertGreaterEqual(len(sources), 2)
            before = file_hashes(f.root)
            failure, result = None, None
            with patch.object(one_shot_provider, 'RealDescriptor', PlaintextDescriptor):
                try:
                    result = export_full_history_once(**options)
                except HistoryError as error:
                    failure = error.code
            self.assertIsNone(failure, 'legal source hardlinks must not block complete read-only export')
            output = options['output']
            self.assertEqual(result['message_count'], 6)
            self.assertTrue(verify_current_capsule(output).ok)
            self.assertEqual(file_hashes(f.root), before)
            self.assertIn('legacy body retained', (output / 'chat.md').read_text('utf8'))
            assets = [path for path in (output / 'assets').rglob('*') if path.is_file()]
            self.assertEqual(sorted(path.read_bytes() for path in assets),
                sorted([f.old_bytes, f.new_bytes, f.video_bytes]))
            for path in assets:
                info = path.stat()
                self.assertEqual(info.st_nlink, 1)
                self.assertNotIn((info.st_dev, info.st_ino), source_ids)
            self.assertTrue(all(source.stat().st_nlink == 2 for source in sources))


if __name__ == '__main__':
    unittest.main()
