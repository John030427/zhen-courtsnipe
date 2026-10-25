import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_release as br  # noqa: E402

SAFE_TEMPLATE = '[token]\ncapture_mode = "page"\n[safety]\ndry_run = false\n[order]\ntoken = ""\n'


class ScanTests(unittest.TestCase):
    def package(self, files: dict[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in {'app/config.example.toml': SAFE_TEMPLATE, **files}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        return root

    def test_clean_package_passes(self):
        pkg = self.package({
            'app/core/x.py': 'token = ""\nuser_id = "YOUR_USER_ID"\nTOKEN = "fixture-token"\nport = 8766\n',
            'runtime/Lib/site-packages/lib/test_vectors.py': 'password = "hunter2hunter2"\n13800138000\n',
        })
        self.assertEqual(br.scan(pkg), [])

    def test_secrets_and_personal_data_fail(self):
        pkg = self.package({
            'app/a.py': 'TOKEN = "c3921ef0aa11bb"\n',
            'app/b.py': 'x = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"\n',
            'app/c.md': '联系 13912345678\n',
            'app/d.txt': '-----BEGIN PRIVATE KEY-----\n',
            'app/e.txt': '44030119900101123X\n',
        })
        problems = '\n'.join(br.scan(pkg))
        for expected in ('credential value：app/a.py:1', 'jwt：app/b.py:1', 'phone：app/c.md:1',
                         'private key：app/d.txt:1', 'id card：app/e.txt:1'):
            self.assertIn(expected, problems)

    def test_personal_files_fail_by_name(self):
        pkg = self.package({'app/config.toml': '', 'app/watch-orders.json': '{}', 'app/scripts.py': '',
                            'HANDOFF-2026-10-13.md': '', 'app/.local-capture/auth-rush.jsonl': '',
                            'logs/server.log': '', 'app/mitmproxy-ca.pem': ''})
        problems = '\n'.join(br.scan(pkg))
        for name in ('app/config.toml', 'app/watch-orders.json', 'app/scripts.py', 'HANDOFF-2026-10-13.md',
                     'app/.local-capture', 'logs/server.log', 'app/mitmproxy-ca.pem'):
            self.assertIn(f'禁止打包的文件：{name}', problems)

    def test_template_must_keep_defaults(self):
        pkg = self.package({'app/config.example.toml': SAFE_TEMPLATE.replace('dry_run = false', 'dry_run = true')})
        self.assertIn('配置模板缺少默认值：dry_run = false', br.scan(pkg))


class WhitelistTests(unittest.TestCase):
    def test_windowless_entries_use_bundled_runtime_and_local_launcher(self):
        for name, body in br.WINDOWLESS_ENTRY_FILES.items():
            body.encode('ascii')
            self.assertTrue(name.endswith('.vbs'))
            self.assertTrue(body.endswith('\r\n'))
            self.assertIn('runtime\\pythonw.exe', body)
            self.assertIn('app\\launcher.py', body)
            self.assertIn('0, False', body)
        self.assertNotIn('--stop', br.WINDOWLESS_ENTRY_FILES['start-workbench.vbs'])
        self.assertIn('--stop', br.WINDOWLESS_ENTRY_FILES['stop-workbench.vbs'])

    def test_every_whitelisted_file_is_tracked_and_missing_ones_stop_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            copied = br.copy_app('HEAD', Path(tmp))
            self.assertIn('launcher.py', copied)
            self.assertNotIn('scripts.py', copied)
            self.assertFalse(any(p.startswith('tests/') for p in copied))
            with patch.object(br, 'APP_FILES', br.APP_FILES + ['no-such-file.py']), self.assertRaises(SystemExit):
                br.copy_app('HEAD', Path(tmp) / 'again')

    def test_entry_files_are_ascii_crlf_and_point_inside_the_package(self):
        for name, body in br.ENTRY_FILES.items():
            body.encode('ascii')
            self.assertTrue(body.endswith('\r\n'), name)
            self.assertNotIn('\n', body.replace('\r\n', ''), name)
            self.assertIn('%~dp0runtime\\', body)


if __name__ == '__main__':
    unittest.main()
