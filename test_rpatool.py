#!/usr/bin/env python3

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

# Import rpatool from the same directory (supports rpatool.py or rpatool-v7.py)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    import rpatool
except ImportError:
    import importlib.util
    candidate = os.path.join(SCRIPT_DIR, 'rpatool-v7.py')
    if not os.path.exists(candidate):
        raise ImportError('Could not find rpatool.py or rpatool-v7.py next to test_rpatool.py')
    spec = importlib.util.spec_from_file_location('rpatool', candidate)
    rpatool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rpatool)


class TestRpaTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rpatool.main(args)
        return out.getvalue(), err.getvalue()

    def test_all_versions_roundtrip(self):
        """Verify create, inspect, and extract into '.' across RPAv1, v2, v3, v3.2, and v4."""
        sample_files = {
            'script.rpy': b'label start:\n    "Hello world"\n',
            'images/bg.png': b'\x89PNG\r\n\x1a\n' + b'A' * 120,
            'images/icon.webp': b'RIFF' + b'B' * 80,
            'audio/bgm.ogg': b'OggS' + b'C' * 200,
        }
        src_dir = os.path.join(self.base, 'src')
        for rel, data in sample_files.items():
            full = os.path.join(src_dir, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as f:
                f.write(data)

        version_flags = {
            1: ['-1'],
            2: ['-2'],
            3: ['-3'],
            3.2: ['-32'],
            4: ['-4'],
        }

        for ver, flags in version_flags.items():
            ext = '.rpi' if ver == 1 else '.rpa'
            arc_path = os.path.join(self.base, f'test_v{str(ver).replace(".", "_")}{ext}')
            mappings = [f'{rel}={os.path.join(src_dir, rel)}' for rel in sample_files]

            _, err = self.run_cli(['-c', arc_path] + flags + ['-p', '8'] + mappings)
            self.assertEqual(err, '', f'Error creating v{ver}: {err}')

            arc = rpatool.RenPyArchive(arc_path)
            self.assertEqual(arc.version, ver)
            self.assertEqual(sorted(arc.list()), sorted(sample_files.keys()))

            extract_dir = os.path.join(self.base, f'out_v{str(ver).replace(".", "_")}')
            os.makedirs(extract_dir, exist_ok=True)
            old_cwd = os.getcwd()
            try:
                os.chdir(extract_dir)
                _, err = self.run_cli(['-x', arc_path])
                self.assertEqual(err, '', f'Error extracting v{ver}: {err}')
            finally:
                os.chdir(old_cwd)

            for rel, expected in sample_files.items():
                extracted_file = os.path.join(extract_dir, rel)
                self.assertTrue(os.path.exists(extracted_file), f'Missing {rel} for v{ver}')
                with open(extracted_file, 'rb') as f:
                    self.assertEqual(f.read(), expected)

    def test_builtin_modded_magics(self):
        """Verify built-in recognition and preservation of modded magic headers."""
        src_file = os.path.join(self.base, 'hello.txt')
        extra_file = os.path.join(self.base, 'extra.txt')
        with open(src_file, 'wb') as f:
            f.write(b'modded archive test')
        with open(extra_file, 'wb') as f:
            f.write(b'appended file')

        for magic in ['ALT-1.0 ', 'ZiX-12A ', 'EX2-1.0 ', 'RPA-5.0 ']:
            arc_path = os.path.join(self.base, f'{magic.strip()}.rpa')
            _, err = self.run_cli(['-c', arc_path, '--magic', magic, f'hello.txt={src_file}'])
            self.assertEqual(err, '')

            # Load without --magic to verify automatic detection
            arc = rpatool.RenPyArchive(arc_path)
            self.assertEqual(arc.magic, magic)
            self.assertEqual(arc.read('hello.txt'), b'modded archive test')

            # Append without --magic and verify modded magic header is preserved
            _, err = self.run_cli(['-a', arc_path, f'extra.txt={extra_file}'])
            self.assertEqual(err, '')
            arc2 = rpatool.RenPyArchive(arc_path)
            self.assertEqual(arc2.magic, magic)
            self.assertEqual(arc2.read('extra.txt'), b'appended file')

    def test_custom_unknown_magic_and_overrides(self):
        """Verify arbitrary --magic strings and custom -k keys."""
        src_file = os.path.join(self.base, 'data.bin')
        with open(src_file, 'wb') as f:
            f.write(b'custom magic payload')

        arc_path = os.path.join(self.base, 'custom.rpa')
        _, err = self.run_cli(['-c', arc_path, '--magic', 'MYGAME-9.9', '-k', 'CAFEBABE', f'data.bin={src_file}'])
        self.assertEqual(err, '')

        with self.assertRaises(ValueError):
            rpatool.RenPyArchive(arc_path)

        arc = rpatool.RenPyArchive(arc_path, custom_magic='MYGAME-9.9')
        self.assertEqual(arc.key, 0xCAFEBABE)
        self.assertEqual(arc.read('data.bin'), b'custom magic payload')

    def test_only_and_exclude_filters(self):
        """Verify multi-extension and wildcard filtering with --only and --exclude."""
        arc = rpatool.RenPyArchive(version=3)
        arc.add('images/a.png', b'png1')
        arc.add('images/b.webp', b'webp1')
        arc.add('images/cache/thumb.png', b'thumb')
        arc.add('scripts/main.rpyc', b'code')
        arc.add('readme.txt', b'text')
        arc_path = os.path.join(self.base, 'filter.rpa')
        arc.save(arc_path)

        # List with combined --only and --exclude
        out, err = self.run_cli(['-l', arc_path, '--only', '.png,.webp', '--exclude', 'images/cache/*'])
        self.assertEqual(err, '')
        self.assertEqual(out.strip().splitlines(), ['images/a.png', 'images/b.webp'])

        # Extract with wildcard --only and extension --exclude
        out_dir = os.path.join(self.base, 'filtered_out')
        _, err = self.run_cli(['-x', arc_path, '-o', out_dir, '--only', 'images/*', '--exclude', '*.webp'])
        self.assertEqual(err, '')
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'images', 'a.png')))
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'images', 'cache', 'thumb.png')))
        self.assertFalse(os.path.exists(os.path.join(out_dir, 'images', 'b.webp')))
        self.assertFalse(os.path.exists(os.path.join(out_dir, 'scripts', 'main.rpyc')))

    def test_path_traversal_sanitization(self):
        """Verify unsafe archive paths cannot escape the target extraction directory."""
        self.assertEqual(rpatool.sanitize_extract_path('../../../etc/passwd'), 'etc/passwd')
        self.assertEqual(rpatool.sanitize_extract_path('/images/../../secret.txt'), 'secret.txt')
        self.assertEqual(rpatool.sanitize_extract_path('C:\\Windows\\System32\\evil.dll'), 'Windows/System32/evil.dll')
        self.assertIsNone(rpatool.sanitize_extract_path('../../..'))
        self.assertIsNone(rpatool.sanitize_extract_path('/'))

        arc = rpatool.RenPyArchive(version=3)
        arc.indexes['../../escaped.txt'] = [(0, 4)]
        arc.files['../../escaped.txt'] = b'safe'
        arc_path = os.path.join(self.base, 'traversal.rpa')
        arc.save(arc_path)

        out_dir = os.path.join(self.base, 'safe_out')
        _, err = self.run_cli(['-x', arc_path, '-o', out_dir])
        self.assertEqual(err, '')
        self.assertTrue(os.path.exists(os.path.join(out_dir, 'escaped.txt')))
        self.assertFalse(os.path.exists(os.path.join(self.base, 'escaped.txt')))

    def test_inplace_delete_and_equals_in_filename(self):
        """Verify in-place archive deletion and filenames containing '=' characters."""
        src_eq = os.path.join(self.base, 'file=with=equals.txt')
        with open(src_eq, 'wb') as f:
            f.write(b'equals content')

        arc_path = os.path.join(self.base, 'edit.rpa')
        _, err = self.run_cli(['-c', arc_path, src_eq])
        self.assertEqual(err, '')

        arc = rpatool.RenPyArchive(arc_path)
        arc.add('temp.txt', b'delete me')
        arc.save(arc_path)

        # In-place delete without -o
        _, err = self.run_cli(['-d', arc_path, 'temp.txt'])
        self.assertEqual(err, '')

        arc2 = rpatool.RenPyArchive(arc_path)
        self.assertEqual(arc2.list(), [arc2.convert_filename(src_eq)])

        # Extract file containing '=' in its name
        out_dir = os.path.join(self.base, 'eq_out')
        _, err = self.run_cli(['-x', arc_path, '-o', out_dir, arc2.convert_filename(src_eq)])
        self.assertEqual(err, '')
        extracted = os.path.join(out_dir, arc2.convert_filename(src_eq))
        self.assertTrue(os.path.exists(extracted))
        with open(extracted, 'rb') as f:
            self.assertEqual(f.read(), b'equals content')


if __name__ == '__main__':
    unittest.main(verbosity=2)
