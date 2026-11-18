#!/usr/bin/env python3

import io
import os
import sys
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

# Dynamically load rpatool-v2.py or rpatool.py from the same directory as this test file
_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = ('rpatool.py', 'rpatool-v2.py', 'rpatool-v7.py')
_TARGET_SCRIPT = None
for _name in _CANDIDATES:
    _path = os.path.join(_HERE, _name)
    if os.path.exists(_path):
        _TARGET_SCRIPT = _path
        break

if _TARGET_SCRIPT is None:
    raise RuntimeError('Could not find rpatool.py or rpatool-v2.py next to test_rpatool.py')

import importlib.util
_spec = importlib.util.spec_from_file_location('rpatool_mod', _TARGET_SCRIPT)
rpatool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rpatool)


def run_cli(args):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    exit_code = 0
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        try:
            rpatool.main(args)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    return exit_code, out_buf.getvalue(), err_buf.getvalue()


class TestRpaToolSuite(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='rpatool_test_')
        self.src_dir = os.path.join(self.tmpdir, 'src')
        os.makedirs(os.path.join(self.src_dir, 'images'))
        os.makedirs(os.path.join(self.src_dir, 'audio'))

        self.sample_files = {
            'script.rpy': b'label start:\n    "Hello RenPy"\n    return\n',
            'images/bg.png': b'\x89PNG\r\n\x1a\n' + (b'PNGDATA' * 120),
            'images/sprite.webp': b'RIFF' + (b'WEBPDATA' * 80),
            'audio/bgm.ogg': b'OggS' + (b'OGGDATA' * 150),
            'notes.txt': b'readme notes\n',
        }
        for rel_path, content in self.sample_files.items():
            full_path = os.path.join(self.src_dir, *rel_path.split('/'))
            with open(full_path, 'wb') as f:
                f.write(content)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _pack_sample(self, archive_path, extra_args=None):
        mappings = [
            '{0}={1}'.format(rel, os.path.join(self.src_dir, *rel.split('/')))
            for rel in self.sample_files
        ]
        cmd = ['-c', archive_path] + (extra_args or []) + mappings
        code, out, err = run_cli(cmd)
        self.assertEqual(code, 0, msg='Pack failed: {0}'.format(err))

    def test_all_versions_roundtrip(self):
        version_cases = [
            (1, ['-1'], 'game_v1.rpi'),
            (2, ['-2'], 'game_v2.rpa'),
            (3, ['-3'], 'game_v3.rpa'),
            (3.2, ['-32'], 'game_v32.rpa'),
            (4, ['-4'], 'game_v4.rpa'),
        ]
        for expected_ver, flags, fname in version_cases:
            with self.subTest(version=expected_ver):
                arc_path = os.path.join(self.tmpdir, fname)
                self._pack_sample(arc_path, extra_args=flags + ['-p', '6'])

                out_dir = os.path.join(self.tmpdir, 'out_v{0}'.format(str(expected_ver).replace('.', '_')))
                os.makedirs(out_dir)
                old_cwd = os.getcwd()
                try:
                    os.chdir(out_dir)
                    code, out, err = run_cli(['-x', arc_path])
                    self.assertEqual(code, 0, msg=err)
                finally:
                    os.chdir(old_cwd)

                for rel_path, expected_bytes in self.sample_files.items():
                    extracted = os.path.join(out_dir, *rel_path.split('/'))
                    self.assertTrue(os.path.exists(extracted), msg='Missing {0}'.format(rel_path))
                    with open(extracted, 'rb') as f:
                        self.assertEqual(f.read(), expected_bytes)

    def test_modded_and_custom_magic(self):
        for magic in ('ALT-1.0 ', 'ZiX-12A ', 'EX2-1.0 ', 'RPA-5.0 '):
            with self.subTest(magic=magic):
                arc_path = os.path.join(self.tmpdir, '{0}.rpa'.format(magic.strip()))
                self._pack_sample(arc_path, extra_args=['--magic', magic, '-k', 'CAFEBABE'])

                # Load without --magic to confirm built-in detection
                arc = rpatool.RenPyArchive(arc_path)
                self.assertEqual(arc.detected_magic, magic)
                self.assertEqual(arc.key, 0xCAFEBABE)
                arc._close_handle()

                # Append a file without --magic and verify header is preserved
                extra_txt = os.path.join(self.tmpdir, 'extra.txt')
                with open(extra_txt, 'wb') as f:
                    f.write(b'appended payload')
                code, _, err = run_cli(['-a', arc_path, 'extra.txt={0}'.format(extra_txt)])
                self.assertEqual(code, 0, msg=err)

                with open(arc_path, 'rb') as f:
                    first_line = f.readline().decode('utf-8')
                self.assertTrue(first_line.startswith(magic))

        # Custom unknown magic header
        custom_arc = os.path.join(self.tmpdir, 'custom.rpa')
        self._pack_sample(custom_arc, extra_args=['--magic', 'MYGAME-9.9', '-k', '1234ABCD'])
        code, out, err = run_cli(['-l', custom_arc, '--magic', 'MYGAME-9.9'])
        self.assertEqual(code, 0, msg=err)
        self.assertIn('images/bg.png', out)

    def test_only_and_exclude_filtering(self):
        arc_path = os.path.join(self.tmpdir, 'filter.rpa')
        self._pack_sample(arc_path)

        code, out, err = run_cli(['-l', arc_path, '--only', 'images/*,.ogg', '--exclude', '*.webp'])
        self.assertEqual(code, 0, msg=err)
        listed = [line.strip() for line in out.strip().splitlines() if line.strip()]
        self.assertEqual(listed, ['audio/bgm.ogg', 'images/bg.png'])

    def test_path_traversal_sanitization(self):
        arc_path = os.path.join(self.tmpdir, 'traversal.rpa')
        arc = rpatool.RenPyArchive(version=3)
        arc.add('../../../etc/passwd', b'escaped_attempt')
        arc.add('/images/../../secret.txt', b'root_escape_attempt')
        arc.save(arc_path)
        arc._close_handle()

        out_dir = os.path.join(self.tmpdir, 'safe_out')
        code, _, err = run_cli(['-x', arc_path, '-o', out_dir])
        self.assertEqual(code, 0, msg=err)

        self.assertTrue(os.path.exists(os.path.join(out_dir, 'etc', 'passwd')))
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'secret.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.tmpdir, 'secret.txt')))

    def test_archive_info_command(self):
        arc_path = os.path.join(self.tmpdir, 'info_v4.rpa')
        self._pack_sample(arc_path, extra_args=['-4', '-k', '1337BEEF'])

        code, out, err = run_cli(['-i', arc_path])
        self.assertEqual(code, 0, msg=err)
        self.assertIn('RPAv4', out)
        self.assertIn("'RPA-4.0 '", out)
        self.assertIn('0x1337beef', out.lower())
        self.assertIn('5', out)

        # Filtered info summary
        code_f, out_f, err_f = run_cli(['--info', arc_path, '--only', '.png,.webp', '--exclude', '*.webp'])
        self.assertEqual(code_f, 0, msg=err_f)
        self.assertIn('1 / 5', out_f)

    def test_parallel_extraction_jobs(self):
        arc_path = os.path.join(self.tmpdir, 'parallel.rpa')
        mappings = []
        expected_many = {}
        for i in range(40):
            rel = 'pack/dir_{0}/file_{1:02d}.bin'.format(i % 4, i)
            payload = ('ITEM_{0:02d}_'.format(i).encode('ascii')) * 256
            expected_many[rel] = payload
            real = os.path.join(self.src_dir, 'gen_{0}.bin'.format(i))
            with open(real, 'wb') as f:
                f.write(payload)
            mappings.append('{0}={1}'.format(rel, real))

        code, _, err = run_cli(['-c', arc_path] + mappings)
        self.assertEqual(code, 0, msg=err)

        out_dir = os.path.join(self.tmpdir, 'parallel_out')
        code_x, _, err_x = run_cli(['-x', arc_path, '-j', '4', '-o', out_dir])
        self.assertEqual(code_x, 0, msg=err_x)

        for rel, payload in expected_many.items():
            target = os.path.join(out_dir, *rel.split('/'))
            self.assertTrue(os.path.exists(target), msg='Missing parallel file {0}'.format(rel))
            with open(target, 'rb') as f:
                self.assertEqual(f.read(), payload)

    def test_compact_progress_output(self):
        arc_path = os.path.join(self.tmpdir, 'progress.rpa')
        mappings = [
            '{0}={1}'.format(rel, os.path.join(self.src_dir, *rel.split('/')))
            for rel in self.sample_files
        ]

        code_c, _, err_c = run_cli(['-c', arc_path, '--progress'] + mappings)
        self.assertEqual(code_c, 0, msg=err_c)
        self.assertIn('Pack [', err_c)
        for line in err_c.strip().splitlines():
            self.assertLessEqual(len(line), 60, msg='Progress line exceeded 60 cols: {0!r}'.format(line))

        out_dir = os.path.join(self.tmpdir, 'prog_out')
        code_x, _, err_x = run_cli(['-x', arc_path, '-j', '2', '--progress', '-o', out_dir])
        self.assertEqual(code_x, 0, msg=err_x)
        self.assertIn('Ext  [', err_x)
        for line in err_x.strip().splitlines():
            self.assertLessEqual(len(line), 60, msg='Extract progress line exceeded 60 cols: {0!r}'.format(line))


if __name__ == '__main__':
    unittest.main(verbosity=2)
