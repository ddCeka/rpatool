#!/usr/bin/env python3

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

# Load rpatool module regardless of filename (rpatool.py or rpatool-v*.py)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = ['rpatool.py'] + sorted(
    [f for f in os.listdir(SCRIPT_DIR) if f.startswith('rpatool-v') and f.endswith('.py')],
    reverse=True,
)
rpatool_path = None
for cand in CANDIDATES:
    full = os.path.join(SCRIPT_DIR, cand)
    if os.path.exists(full):
        rpatool_path = full
        break

if rpatool_path is None:
    raise RuntimeError('Could not locate rpatool.py in {0}'.format(SCRIPT_DIR))

import importlib.util
spec = importlib.util.spec_from_file_location('rpatool', rpatool_path)
rpatool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpatool)


def run_cli(args):
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    code = 0
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        try:
            rpatool.main(args)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    return code, out_buf.getvalue(), err_buf.getvalue()


class TestRpaTool(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='rpatool_test_')
        self.src_dir = os.path.join(self.tmpdir, 'src')
        os.makedirs(os.path.join(self.src_dir, 'images'))
        os.makedirs(os.path.join(self.src_dir, 'audio'))

        self.sample_files = {
            'script.rpy': b'label start:\n    "Hello RenPy!"\n    return\n',
            'images/bg.png': b'\x89PNG\r\n\x1a\nFAKEPNGDATA' * 10,
            'images/sprite.webp': b'RIFF\x00\x00\x00\x00WEBPVP8 ' * 8,
            'audio/bgm.ogg': b'OggS\x00\x02FAKEOGGSTREAM' * 12,
        }
        for rel_path, content in self.sample_files.items():
            full_path = os.path.join(self.src_dir, rel_path.replace('/', os.sep))
            with open(full_path, 'wb') as f:
                f.write(content)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_archive_versions_roundtrip_and_dot_extract(self):
        version_flags = [
            (1, ['-1']),
            (2, ['-2']),
            (3, ['-3']),
            (3.2, ['-32']),
            (4, ['-4']),
        ]
        for ver, flags in version_flags:
            with self.subTest(version=ver):
                ext_name = 'game_v{0}.rpi'.format(str(ver).replace('.', '_')) if ver == 1 else 'game_v{0}.rpa'.format(str(ver).replace('.', '_'))
                arc_path = os.path.join(self.tmpdir, ext_name)

                code, _, err = run_cli(
                    flags + ['-p', '8', '-c', arc_path] + [
                        '{0}={1}'.format(rel, os.path.join(self.src_dir, rel.replace('/', os.sep)))
                        for rel in self.sample_files
                    ]
                )
                self.assertEqual(code, 0, err)
                self.assertTrue(os.path.exists(arc_path))

                arc = rpatool.RenPyArchive(arc_path)
                self.assertEqual(arc.version, ver)
                self.assertEqual(sorted(arc.list()), sorted(self.sample_files.keys()))

                # Extract directly into '.' inside a clean subdirectory
                extract_dir = os.path.join(self.tmpdir, 'out_v{0}'.format(str(ver).replace('.', '_')))
                os.makedirs(extract_dir)
                old_cwd = os.getcwd()
                try:
                    os.chdir(extract_dir)
                    code, _, err = run_cli(['-x', arc_path])
                    self.assertEqual(code, 0, err)
                finally:
                    os.chdir(old_cwd)

                for rel_path, expected_data in self.sample_files.items():
                    out_file = os.path.join(extract_dir, rel_path.replace('/', os.sep))
                    self.assertTrue(os.path.exists(out_file), 'Missing extracted file: ' + rel_path)
                    with open(out_file, 'rb') as f:
                        self.assertEqual(f.read(), expected_data)

    def test_built_in_modded_magic_and_preserve_on_append(self):
        for magic in ('ALT-1.0 ', 'ZiX-12A ', 'EX2-1.0 ', 'RPA-5.0 '):
            with self.subTest(magic=magic):
                arc_path = os.path.join(self.tmpdir, 'mod_{0}.rpa'.format(magic.strip()))
                arc = rpatool.RenPyArchive(version=3, custom_magic=magic)
                arc.add('a.txt', b'alpha')
                arc.save(arc_path)

                # Load without --magic to verify built-in detection
                reloaded = rpatool.RenPyArchive(arc_path)
                self.assertEqual(reloaded.detected_magic, magic)
                self.assertEqual(reloaded.read('a.txt'), b'alpha')

                # Append via CLI without specifying --magic and verify magic is preserved
                extra_file = os.path.join(self.tmpdir, 'b.txt')
                with open(extra_file, 'wb') as f:
                    f.write(b'beta')
                code, _, err = run_cli(['-a', arc_path, 'b.txt=' + extra_file])
                self.assertEqual(code, 0, err)

                reloaded2 = rpatool.RenPyArchive(arc_path)
                self.assertEqual(reloaded2.detected_magic, magic)
                self.assertEqual(reloaded2.read('b.txt'), b'beta')

    def test_custom_unknown_magic_and_key_override(self):
        arc_path = os.path.join(self.tmpdir, 'custom.rpa')
        code, _, err = run_cli([
            '--magic', 'MYGAME-9.9',
            '-k', 'CAFEBABE',
            '-c', arc_path,
            'script.rpy=' + os.path.join(self.src_dir, 'script.rpy'),
        ])
        self.assertEqual(code, 0, err)

        with open(arc_path, 'rb') as f:
            header = f.readline().decode('utf-8')
        self.assertTrue(header.startswith('MYGAME-9.9 '))

        out_dir = os.path.join(self.tmpdir, 'custom_out')
        code, _, err = run_cli(['--magic', 'MYGAME-9.9', '-o', out_dir, '-x', arc_path])
        self.assertEqual(code, 0, err)
        with open(os.path.join(out_dir, 'script.rpy'), 'rb') as f:
            self.assertEqual(f.read(), self.sample_files['script.rpy'])

    def test_only_and_exclude_filtering(self):
        arc_path = os.path.join(self.tmpdir, 'filter.rpa')
        code, _, err = run_cli([
            '-c', arc_path,
            'images=' + os.path.join(self.src_dir, 'images'),
            'audio=' + os.path.join(self.src_dir, 'audio'),
            'script.rpy=' + os.path.join(self.src_dir, 'script.rpy'),
        ])
        self.assertEqual(code, 0, err)

        code, out, err = run_cli(['-l', arc_path, '--only', 'images/*', '--exclude', '*.webp'])
        self.assertEqual(code, 0, err)
        listed = [line.strip() for line in out.strip().splitlines() if line.strip()]
        self.assertEqual(listed, ['images/bg.png'])

        out_dir = os.path.join(self.tmpdir, 'filter_out')
        code, _, err = run_cli(['-o', out_dir, '-x', arc_path, '--only', '.png,.ogg', '--exclude', 'audio/*'])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'images', 'bg.png')))
        self.assertFalse(os.path.exists(os.path.join(out_dir, 'audio', 'bgm.ogg')))

    def test_sanitize_extract_path(self):
        base = os.path.join(self.tmpdir, 'safe_extract')
        p1 = rpatool.sanitize_extract_path(base, '../../../etc/passwd')
        self.assertEqual(p1, os.path.abspath(os.path.join(base, 'etc', 'passwd')))

        p2 = rpatool.sanitize_extract_path(base, '/images/../../secret.txt')
        self.assertEqual(p2, os.path.abspath(os.path.join(base, 'secret.txt')))

        with self.assertRaises(ValueError):
            rpatool.sanitize_extract_path(base, '../../..')

    def test_inplace_delete_and_equals_in_filename(self):
        arc_path = os.path.join(self.tmpdir, 'inplace.rpa')
        eq_file = os.path.join(self.tmpdir, 'file=with=equals.txt')
        with open(eq_file, 'wb') as f:
            f.write(b'equals content')

        code, _, err = run_cli([
            '-c', arc_path,
            'file=with=equals.txt=' + eq_file,
            'keep.txt=' + eq_file,
        ])
        self.assertEqual(code, 0, err)

        code, _, err = run_cli(['-d', arc_path, 'file=with=equals.txt'])
        self.assertEqual(code, 0, err)

        arc = rpatool.RenPyArchive(arc_path)
        self.assertEqual(arc.list(), ['keep.txt'])

    def test_archive_info_flag(self):
        arc_path = os.path.join(self.tmpdir, 'info_test.rpa')
        code, _, err = run_cli([
            '-4', '-k', '1234ABCD', '-c', arc_path,
            'images=' + os.path.join(self.src_dir, 'images'),
            'script.rpy=' + os.path.join(self.src_dir, 'script.rpy'),
        ])
        self.assertEqual(code, 0, err)

        code, out, err = run_cli(['-i', arc_path])
        self.assertEqual(code, 0, err)
        self.assertIn('RPAv4', out)
        self.assertIn('RPA-4.0', out)
        self.assertIn('0x1234ABCD', out)
        self.assertIn('Files:', out)

        # Filtered info
        code, out_filtered, err = run_cli(['--info', arc_path, '--only', '.png'])
        self.assertEqual(code, 0, err)
        self.assertIn('Matched files:', out_filtered)
        self.assertIn('1 / 3', out_filtered)

    def test_parallel_extraction(self):
        arc_path = os.path.join(self.tmpdir, 'parallel.rpa')
        many_dir = os.path.join(self.tmpdir, 'many_src')
        os.makedirs(many_dir)

        expected = {}
        for idx in range(40):
            sub = 'dir_{0}'.format(idx % 4)
            os.makedirs(os.path.join(many_dir, sub), exist_ok=True)
            rel = '{0}/item_{1:02d}.dat'.format(sub, idx)
            payload = ('PAYLOAD_{0}_'.format(idx) * 64).encode('utf-8')
            expected[rel] = payload
            with open(os.path.join(many_dir, rel.replace('/', os.sep)), 'wb') as f:
                f.write(payload)

        code, _, err = run_cli(['-c', arc_path, 'pack=' + many_dir])
        self.assertEqual(code, 0, err)

        out_dir = os.path.join(self.tmpdir, 'parallel_out')
        code, _, err = run_cli(['-j', '4', '-o', out_dir, '-x', arc_path])
        self.assertEqual(code, 0, err)

        for rel, payload in expected.items():
            extracted_file = os.path.join(out_dir, 'pack', rel.replace('/', os.sep))
            self.assertTrue(os.path.exists(extracted_file), 'Missing: ' + rel)
            with open(extracted_file, 'rb') as f:
                self.assertEqual(f.read(), payload)

    def test_progress_output_compact_60_cols(self):
        arc_path = os.path.join(self.tmpdir, 'progress.rpa')
        code, _, err = run_cli([
            '--progress', '-c', arc_path,
            'images=' + os.path.join(self.src_dir, 'images'),
            'audio=' + os.path.join(self.src_dir, 'audio'),
            'script.rpy=' + os.path.join(self.src_dir, 'script.rpy'),
        ])
        self.assertEqual(code, 0, err)
        progress_lines = [line for line in err.splitlines() if '[' in line and '%' in line]
        self.assertTrue(len(progress_lines) > 0, 'Expected progress output on stderr')
        for line in progress_lines:
            self.assertLessEqual(len(line), 60, 'Progress line exceeded 60 columns: {0!r}'.format(line))

        out_dir = os.path.join(self.tmpdir, 'progress_out')
        code, _, err = run_cli(['--progress', '-j', '2', '-o', out_dir, '-x', arc_path])
        self.assertEqual(code, 0, err)
        ext_lines = [line for line in err.splitlines() if '[' in line and '%' in line]
        self.assertTrue(len(ext_lines) > 0, 'Expected extraction progress output on stderr')
        for line in ext_lines:
            self.assertLessEqual(len(line), 60, 'Progress line exceeded 60 columns: {0!r}'.format(line))

    def test_dry_run_mode_all_operations(self):
        dry_arc = os.path.join(self.tmpdir, 'never_created.rpa')
        code, out, err = run_cli([
            '--dry-run', '-4', '-c', dry_arc,
            'images=' + os.path.join(self.src_dir, 'images'),
            'script.rpy=' + os.path.join(self.src_dir, 'script.rpy'),
        ])
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(dry_arc), 'Archive should not be created during --dry-run')
        self.assertIn('[dry-run] Create archive', out)
        self.assertIn('images/bg.png', out)

        real_arc = os.path.join(self.tmpdir, 'real.rpa')
        code, _, err = run_cli([
            '-c', real_arc,
            'images=' + os.path.join(self.src_dir, 'images'),
            'script.rpy=' + os.path.join(self.src_dir, 'script.rpy'),
        ])
        self.assertEqual(code, 0, err)
        orig_size = os.path.getsize(real_arc)

        dry_out_dir = os.path.join(self.tmpdir, 'never_extracted')
        code, out, err = run_cli(['--dry-run', '-o', dry_out_dir, '-x', real_arc, '--only', '.png'])
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(dry_out_dir), 'Output directory should not be created during --dry-run')
        self.assertIn('[dry-run] Extract', out)
        self.assertIn('images/bg.png', out)
        self.assertNotIn('script.rpy', out)

        code, out, err = run_cli([
            '--dry-run', '-a', real_arc,
            'audio=' + os.path.join(self.src_dir, 'audio'),
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(os.path.getsize(real_arc), orig_size)
        self.assertIn('[dry-run] Append to archive', out)
        self.assertIn('audio/bgm.ogg', out)
        self.assertNotIn('audio/bgm.ogg', rpatool.RenPyArchive(real_arc).list())

        code, out, err = run_cli(['--dry-run', '-d', real_arc, 'script.rpy', 'nonexistent.rpy'])
        self.assertEqual(code, 0, err)
        self.assertIn('[dry-run] Delete from archive', out)
        self.assertIn('script.rpy', out)
        self.assertIn('does not exist in archive', err)
        self.assertIn('script.rpy', rpatool.RenPyArchive(real_arc).list())


if __name__ == '__main__':
    unittest.main()
