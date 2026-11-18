#!/usr/bin/env python3

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr


def load_rpatool_module():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(script_dir, 'rpatool.py')
    if os.path.exists(path):
        spec = importlib.util.spec_from_file_location('rpatool', path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, path
    raise FileNotFoundError('Could not find rpatool.py in test directory')


rpatool, RPATOOL_PATH = load_rpatool_module()


class TestRpaTool(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='rpatool_test_')
        self.src_dir = os.path.join(self.test_dir, 'src')
        os.makedirs(os.path.join(self.src_dir, 'images'), exist_ok=True)
        os.makedirs(os.path.join(self.src_dir, 'audio'), exist_ok=True)

        self.sample_files = {
            'script.rpy': b'label start:\n    "Hello, Ren\'Py!"\n    return\n',
            'images/bg.png': b'\x89PNG\r\n\x1a\n' + b'A' * 180,
            'images/sprite.webp': b'RIFF' + b'B' * 124,
        }
        self.total_payload_bytes = sum(len(v) for v in self.sample_files.values())

        for rel_path, data in self.sample_files.items():
            full_path = os.path.join(self.src_dir, *rel_path.split('/'))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(data)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def run_cli(self, args, cwd=None):
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        old_cwd = os.getcwd()
        exit_code = 0
        try:
            if cwd:
                os.chdir(cwd)
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                try:
                    rpatool.main(args)
                except SystemExit as e:
                    exit_code = e.code if isinstance(e.code, int) else 1
        finally:
            os.chdir(old_cwd)
        return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()

    def test_all_archive_versions_roundtrip_and_dot_extract(self):
        version_cases = [
            ('-1', 'test_v1.rpi', 1),
            ('-2', 'test_v2.rpa', 2),
            ('-3', 'test_v3.rpa', 3),
            ('-32', 'test_v32.rpa', 3.2),
            ('-4', 'test_v4.rpa', 4),
        ]

        for flag, filename, expected_ver in version_cases:
            with self.subTest(version=expected_ver):
                arc_path = os.path.join(self.test_dir, filename)
                mappings = [
                    '{0}={1}'.format(rel, os.path.join(self.src_dir, *rel.split('/')))
                    for rel in self.sample_files
                ]
                code, out, err = self.run_cli([flag, '-p', '8', '-c', arc_path] + mappings)
                self.assertEqual(code, 0, msg=err)

                arc = rpatool.RenPyArchive(arc_path)
                self.assertEqual(arc.version, expected_ver)
                self.assertEqual(sorted(arc.list()), sorted(self.sample_files.keys()))

                extract_dir = os.path.join(self.test_dir, 'out_{0}'.format(str(expected_ver).replace('.', '_')))
                os.makedirs(extract_dir, exist_ok=True)
                code, out, err = self.run_cli(['-x', arc_path], cwd=extract_dir)
                self.assertEqual(code, 0, msg=err)

                for rel_path, expected_bytes in self.sample_files.items():
                    extracted_file = os.path.join(extract_dir, *rel_path.split('/'))
                    self.assertTrue(os.path.exists(extracted_file), msg='Missing {0}'.format(rel_path))
                    with open(extracted_file, 'rb') as f:
                        self.assertEqual(f.read(), expected_bytes)

    def test_archive_info_flag(self):
        arc_path = os.path.join(self.test_dir, 'info_test.rpa')
        mappings = [
            '{0}={1}'.format(rel, os.path.join(self.src_dir, *rel.split('/')))
            for rel in self.sample_files
        ]
        code, _, err = self.run_cli(['-4', '-k', '1234ABCD', '-c', arc_path] + mappings)
        self.assertEqual(code, 0, msg=err)

        code, out, err = self.run_cli(['-i', arc_path])
        self.assertEqual(code, 0, msg=err)
        self.assertIn('Format       : RPAv4', out)
        self.assertIn("Magic Header : 'RPA-4.0'", out)
        self.assertIn('XOR Key      : 0x1234ABCD', out)
        self.assertIn('Total Files  : 3', out)
        self.assertIn('Payload Size : {0} B'.format(self.total_payload_bytes), out)

        code, out_filtered, err = self.run_cli(['-i', '--only', '.png', arc_path])
        self.assertEqual(code, 0, msg=err)
        png_size = len(self.sample_files['images/bg.png'])
        self.assertIn('Matched Files: 1 ({0} B)'.format(png_size), out_filtered)

    def test_built_in_modded_magic_and_preserve_on_append(self):
        for magic in ('ALT-1.0 ', 'ZiX-12A ', 'EX2-1.0 ', 'RPA-5.0 '):
            with self.subTest(magic=magic):
                arc_path = os.path.join(self.test_dir, 'mod_{0}.rpa'.format(magic.strip().replace('.', '_')))
                arc = rpatool.RenPyArchive(version=3, custom_magic=magic)
                arc.add('script.rpy', self.sample_files['script.rpy'])
                arc.save(arc_path)

                reloaded = rpatool.RenPyArchive(arc_path)
                self.assertEqual(reloaded.detected_magic, magic)
                self.assertEqual(reloaded.read('script.rpy'), self.sample_files['script.rpy'])

                extra_file = os.path.join(self.src_dir, 'images', 'bg.png')
                code, _, err = self.run_cli(['-a', arc_path, 'images/bg.png={0}'.format(extra_file)])
                self.assertEqual(code, 0, msg=err)

                after_append = rpatool.RenPyArchive(arc_path)
                self.assertEqual(after_append.detected_magic, magic)
                self.assertEqual(sorted(after_append.list()), ['images/bg.png', 'script.rpy'])

    def test_custom_unknown_magic_and_key_override(self):
        arc_path = os.path.join(self.test_dir, 'custom_magic.rpa')
        src_script = os.path.join(self.src_dir, 'script.rpy')
        code, _, err = self.run_cli([
            '--magic', 'MYGAME-9.9',
            '-k', 'CAFEBABE',
            '-c', arc_path,
            'script.rpy={0}'.format(src_script)
        ])
        self.assertEqual(code, 0, msg=err)

        with self.assertRaises(ValueError):
            rpatool.RenPyArchive(arc_path)

        code, out, err = self.run_cli(['--magic', 'MYGAME-9.9', '-l', arc_path])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(out.strip(), 'script.rpy')

    def test_only_and_exclude_filtering(self):
        arc_path = os.path.join(self.test_dir, 'filter_test.rpa')
        mappings = [
            '{0}={1}'.format(rel, os.path.join(self.src_dir, *rel.split('/')))
            for rel in self.sample_files
        ]
        self.run_cli(['-c', arc_path] + mappings)

        code, out, err = self.run_cli(['-l', '--only', 'images/*', '--exclude', '*.webp', arc_path])
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(out.strip().splitlines(), ['images/bg.png'])

        out_dir = os.path.join(self.test_dir, 'filtered_out')
        code, _, err = self.run_cli(['-x', '-o', out_dir, '--only', '.png,.webp', '--exclude', '*.webp', arc_path])
        self.assertEqual(code, 0, msg=err)
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'images', 'bg.png')))
        self.assertFalse(os.path.exists(os.path.join(out_dir, 'images', 'sprite.webp')))

    def test_parallel_extraction(self):
        arc_path = os.path.join(self.test_dir, 'parallel_test.rpa')
        arc = rpatool.RenPyArchive(version=3)
        expected = {}
        for i in range(40):
            name = 'batch/sub_{0}/file_{1:02d}.bin'.format(i % 4, i)
            payload = ('ITEM-{0}-'.format(i).encode('utf-8')) * 64
            arc.add(name, payload)
            expected[name] = payload
        arc.save(arc_path)

        out_dir = os.path.join(self.test_dir, 'parallel_out')
        code, _, err = self.run_cli(['-x', '-j', '4', '-o', out_dir, arc_path])
        self.assertEqual(code, 0, msg=err)

        for rel_path, expected_bytes in expected.items():
            full_path = os.path.join(out_dir, *rel_path.split('/'))
            self.assertTrue(os.path.exists(full_path))
            with open(full_path, 'rb') as f:
                self.assertEqual(f.read(), expected_bytes)

    def test_progress_output_compact_60_cols(self):
        arc_path = os.path.join(self.test_dir, 'progress_test.rpa')
        mappings = [
            '{0}={1}'.format(rel, os.path.join(self.src_dir, *rel.split('/')))
            for rel in self.sample_files
        ]
        code, _, err = self.run_cli(['--progress', '-c', arc_path] + mappings)
        self.assertEqual(code, 0, msg=err)
        self.assertTrue(len(err.strip()) > 0)
        for line in err.splitlines():
            self.assertLessEqual(len(line), 60, msg='Line exceeded 60 cols: {0!r}'.format(line))

        out_dir = os.path.join(self.test_dir, 'progress_out')
        code, _, err = self.run_cli(['--progress', '-j', '2', '-x', '-o', out_dir, arc_path])
        self.assertEqual(code, 0, msg=err)
        self.assertTrue(len(err.strip()) > 0)
        for line in err.splitlines():
            self.assertLessEqual(len(line), 60, msg='Line exceeded 60 cols: {0!r}'.format(line))

    def test_dry_run_mode_all_operations(self):
        never_created = os.path.join(self.test_dir, 'never_created.rpa')
        mappings = [
            '{0}={1}'.format(rel, os.path.join(self.src_dir, *rel.split('/')))
            for rel in self.sample_files
        ]

        code, out, err = self.run_cli(['--dry-run', '-4', '-c', never_created] + mappings)
        self.assertEqual(code, 0, msg=err)
        self.assertFalse(os.path.exists(never_created))
        self.assertIn('[dry-run] Would pack', out)
        self.assertIn('[dry-run] Summary: would create', out)

        real_arc = os.path.join(self.test_dir, 'real.rpa')
        self.run_cli(['-c', real_arc] + mappings)

        dry_extract_dir = os.path.join(self.test_dir, 'dry_extract')
        code, out, err = self.run_cli(['--dry-run', '-x', '-o', dry_extract_dir, '--only', '.png', real_arc])
        self.assertEqual(code, 0, msg=err)
        self.assertFalse(os.path.exists(dry_extract_dir))
        self.assertIn('images/bg.png', out)

        code, out, err = self.run_cli(['--dry-run', '-d', real_arc, 'script.rpy', 'nonexistent.rpy'])
        self.assertEqual(code, 0)
        self.assertIn('[dry-run] Would fail deleting missing file nonexistent.rpy', err)
        self.assertIn('script.rpy', rpatool.RenPyArchive(real_arc).list())

    def test_sanitize_extract_path(self):
        base = os.path.join(self.test_dir, 'extract_sandbox')
        os.makedirs(base, exist_ok=True)

        safe1 = rpatool.sanitize_extract_path(base, '../../../etc/passwd')
        self.assertEqual(safe1, os.path.abspath(os.path.join(base, 'etc', 'passwd')))

        safe2 = rpatool.sanitize_extract_path(base, '/images/../../secret.txt')
        self.assertEqual(safe2, os.path.abspath(os.path.join(base, 'secret.txt')))

        with self.assertRaises(ValueError):
            rpatool.sanitize_extract_path(base, '../../..')

    def test_inplace_delete_and_equals_in_filename(self):
        weird_name = 'file=with=equals.txt'
        weird_path = os.path.join(self.test_dir, weird_name)
        with open(weird_path, 'wb') as f:
            f.write(b'equals-content')

        arc_path = os.path.join(self.test_dir, 'equals.rpa')
        code, _, err = self.run_cli(['-c', arc_path, weird_name, 'script.rpy={0}'.format(os.path.join(self.src_dir, 'script.rpy'))], cwd=self.test_dir)
        self.assertEqual(code, 0, msg=err)

        code, _, err = self.run_cli(['-d', arc_path, 'script.rpy'])
        self.assertEqual(code, 0, msg=err)

        arc = rpatool.RenPyArchive(arc_path)
        self.assertEqual(arc.list(), [weird_name])

        out_dir = os.path.join(self.test_dir, 'equals_out')
        code, _, err = self.run_cli(['-x', '-o', out_dir, arc_path])
        self.assertEqual(code, 0, msg=err)
        with open(os.path.join(out_dir, weird_name), 'rb') as f:
            self.assertEqual(f.read(), b'equals-content')


if __name__ == '__main__':
    unittest.main()
