#!/usr/bin/env python3

from __future__ import print_function

import sys
import os
import codecs
import errno
import random

if sys.version_info < (3, 8):
    try:
        import pickle5 as pickle
    except ImportError:
        import pickle
        print('warning: pickle5 module could not be loaded and Python version is < 3.8,', file=sys.stderr)
        print('         newer Ren\'Py games may fail to unpack!', file=sys.stderr)
        if sys.version_info >= (3, 5):
            print('         if this occurs, fix it by installing pickle5:', file=sys.stderr)
            print('             {} -m pip install pickle5'.format(sys.executable), file=sys.stderr)
        else:
            print('         if this occurs, please upgrade to a newer Python (>= 3.5).', file=sys.stderr)
        print(file=sys.stderr)
else:
    import pickle

if sys.version_info[0] >= 3:
    def _unicode(text):
        return text

    def _printable(text):
        return text

    def _unmangle(data):
        if isinstance(data, bytes):
            return data
        return data.encode('latin1')

    def _unpickle(data):
        return pickle.loads(data, encoding='latin1')
elif sys.version_info[0] == 2:
    def _unicode(text):
        if isinstance(text, unicode):
            return text
        return text.decode('utf-8')

    def _printable(text):
        return text.encode('utf-8')

    def _unmangle(data):
        return data

    def _unpickle(data):
        return pickle.loads(data)


def sanitize_extract_path(base_dir, archive_path):
    """
    Sanitize an archive-internal path so it cannot escape base_dir
    via leading slashes, drive letters, or '..' traversal components.
    """
    clean = archive_path.replace('\\', '/')
    _, clean = os.path.splitdrive(clean)
    clean = clean.lstrip('/')

    safe_parts = []
    for part in clean.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if safe_parts:
                safe_parts.pop()
            continue
        safe_parts.append(part)

    if not safe_parts:
        raise ValueError('unsafe or empty archive path: {0}'.format(archive_path))

    rel_path = os.path.join(*safe_parts)
    abs_base = os.path.abspath(base_dir)
    abs_target = os.path.abspath(os.path.join(abs_base, rel_path))

    if os.path.commonpath([abs_base, abs_target]) != abs_base:
        raise ValueError('path traversal blocked for archive entry: {0}'.format(archive_path))

    return rel_path


class RenPyArchive:
    RPA2_MAGIC = 'RPA-2.0 '
    RPA3_MAGIC = 'RPA-3.0 '
    RPA3_2_MAGIC = 'RPA-3.2 '
    RPA4_MAGIC = 'RPA-4.0 '

    # Built-in known modded magic headers and their default layout versions.
    # Entries with None infer version from header token count (2 -> v3, 3+ -> v3.2, 1 -> v2).
    KNOWN_MODDED_MAGICS = {
        'ALT-1.0 ': 3,
        'ZiX-12A ': 3,
        'ZiX-12B ': 3,
        'EX2-1.0 ': 3,
        'RPA-2.1 ': 2,
        'RPA-3.1 ': 3,
        'RPA-4.1 ': 4,
        'RPA-5.0 ': 3,
    }

    PICKLE_PROTOCOL = 2

    def __init__(self, file=None, version=3, padlength=0, key=0xDEADBEEF, verbose=False,
                 force_version=None, override_offset=None, override_key=None, custom_magic=None):
        self.file = None
        self.data_file = None
        self.handle = None
        self.files = {}
        self.indexes = {}
        self.version = version
        self.padlength = padlength
        self.key = key
        self.verbose = verbose
        self.force_version = force_version
        self.override_offset = override_offset
        self.override_key = override_key
        self.custom_magic = self._normalize_magic(custom_magic)
        self.detected_magic = None

        if file is not None:
            self.load(file)

    @staticmethod
    def _normalize_magic(magic):
        if magic is None:
            return None
        magic = _unicode(magic)
        if not magic.endswith(' '):
            magic += ' '
        return magic

    def __del__(self):
        if self.handle is not None:
            self.handle.close()

    def _infer_version_from_header(self, magic_line, magic_prefix):
        rest = magic_line[len(magic_prefix):].strip()
        vals = rest.split()
        if len(vals) >= 3:
            return 3.2
        elif len(vals) == 2:
            return 3
        elif len(vals) == 1:
            return 2
        raise ValueError('could not infer archive layout from header: {0}'.format(magic_line.strip()))

    def get_version(self):
        self.handle.seek(0)
        raw_line = self.handle.readline()
        magic = raw_line.decode('utf-8', errors='replace')

        if self.force_version is not None:
            if self.custom_magic is not None and magic.startswith(self.custom_magic):
                self.detected_magic = self.custom_magic
            else:
                for known_magic in self.KNOWN_MODDED_MAGICS:
                    if magic.startswith(known_magic):
                        self.detected_magic = known_magic
                        break
            return self.force_version

        if self.custom_magic is not None:
            if magic.startswith(self.custom_magic):
                self.detected_magic = self.custom_magic
                return self._infer_version_from_header(magic, self.custom_magic)
            raise ValueError('archive header does not start with expected custom magic {0!r}'.format(self.custom_magic))

        if magic.startswith(self.RPA4_MAGIC):
            self.detected_magic = self.RPA4_MAGIC
            return 4
        elif magic.startswith(self.RPA3_2_MAGIC):
            self.detected_magic = self.RPA3_2_MAGIC
            return 3.2
        elif magic.startswith(self.RPA3_MAGIC):
            self.detected_magic = self.RPA3_MAGIC
            return 3
        elif magic.startswith(self.RPA2_MAGIC):
            self.detected_magic = self.RPA2_MAGIC
            return 2

        for known_magic, default_ver in self.KNOWN_MODDED_MAGICS.items():
            if magic.startswith(known_magic):
                self.detected_magic = known_magic
                self.verbose_print('Detected known modded magic header: {0!r}'.format(known_magic))
                if default_ver is not None:
                    return default_ver
                return self._infer_version_from_header(magic, known_magic)

        if self.file and (self.file.lower().endswith('.rpi') or os.path.exists(os.path.splitext(self.file)[0] + '.rpi')):
            self.detected_magic = None
            return 1

        raise ValueError('the given file is not a valid Ren\'Py archive, or an unsupported version')

    def _parse_header_tokens(self, metadata):
        if self.detected_magic is not None and metadata.startswith(self.detected_magic):
            rest = metadata[len(self.detected_magic):].strip()
            return rest.split()
        vals = metadata.strip().split()
        return vals[1:] if len(vals) > 1 else []

    def extract_indexes(self):
        self.handle.seek(0)
        indexes = None

        if self.version in (2, 3, 3.2, 4):
            metadata = self.handle.readline().decode('utf-8', errors='replace')
            vals = self._parse_header_tokens(metadata)

            if self.override_offset is not None:
                offset = self.override_offset
            else:
                if len(vals) < 1:
                    raise ValueError('missing index offset in archive header')
                offset = int(vals[0], 16)

            if self.version in (3, 3.2, 4):
                if self.override_key is not None:
                    self.key = self.override_key
                elif self.version in (3, 4):
                    self.key = 0
                    for subkey in vals[1:]:
                        self.key ^= int(subkey, 16)
                elif self.version == 3.2:
                    self.key = 0
                    for subkey in vals[2:]:
                        self.key ^= int(subkey, 16)

            self.handle.seek(offset)
            contents = codecs.decode(self.handle.read(), 'zlib')
            indexes = _unpickle(contents)

            if self.version in (3, 3.2, 4):
                obfuscated_indexes = indexes
                indexes = {}
                for i in obfuscated_indexes.keys():
                    entries = []
                    for entry in obfuscated_indexes[i]:
                        if len(entry) == 2:
                            ent_offset, ent_length = entry
                            entries.append((ent_offset ^ self.key, ent_length ^ self.key))
                        else:
                            ent_offset, ent_length, prefix = entry
                            entries.append((ent_offset ^ self.key, ent_length ^ self.key, prefix))
                    indexes[i] = entries
        elif self.version == 1:
            rpi_path = self.file if self.file.lower().endswith('.rpi') else os.path.splitext(self.file)[0] + '.rpi'
            with open(rpi_path, 'rb') as rpi_handle:
                indexes = _unpickle(codecs.decode(rpi_handle.read(), 'zlib'))
        else:
            raise ValueError('unsupported archive version: {0}'.format(self.version))

        return indexes

    def generate_padding(self):
        length = random.randint(1, self.padlength)
        return os.urandom(length)

    def convert_filename(self, filename):
        (_, filename) = os.path.splitdrive(os.path.normpath(filename).replace(os.sep, '/'))
        return filename

    def verbose_print(self, message):
        if self.verbose:
            print(message)

    def list(self):
        return list(self.indexes.keys()) + list(self.files.keys())

    def has_file(self, filename):
        filename = self.convert_filename(_unicode(filename))
        return filename in self.indexes or filename in self.files

    def read(self, filename):
        filename = self.convert_filename(_unicode(filename))

        if filename not in self.files and filename not in self.indexes:
            raise IOError(errno.ENOENT, 'the requested file {0} does not exist in the given Ren\'Py archive'.format(
                _printable(filename)))

        if filename not in self.files and filename in self.indexes and self.handle is None:
            raise IOError(errno.ENOENT, 'the requested file {0} does not exist in the given Ren\'Py archive'.format(
                _printable(filename)))

        if filename in self.files:
            self.verbose_print('Reading file {0} from internal storage...'.format(_printable(filename)))
            return self.files[filename]
        else:
            chunks = []
            for entry in self.indexes[filename]:
                if len(entry) == 3:
                    (offset, length, prefix) = entry
                else:
                    (offset, length) = entry
                    prefix = b''

                self.verbose_print('Reading file {0} from data file {1}... (offset = {2}, length = {3} bytes)'.format(
                    _printable(filename), self.data_file or self.file, offset, length))
                self.handle.seek(offset)
                unmangled_prefix = _unmangle(prefix)
                chunks.append(unmangled_prefix + self.handle.read(length - len(unmangled_prefix)))
            return b''.join(chunks)

    def change(self, filename, contents):
        filename = _unicode(filename)
        self.remove(filename)
        self.add(filename, contents)

    def add(self, filename, contents):
        filename = self.convert_filename(_unicode(filename))
        if filename in self.files or filename in self.indexes:
            raise ValueError('file {0} already exists in archive'.format(_printable(filename)))

        self.verbose_print('Adding file {0} to archive... (length = {1} bytes)'.format(
            _printable(filename), len(contents)))
        self.files[filename] = contents

    def remove(self, filename):
        filename = self.convert_filename(_unicode(filename))
        if filename in self.files:
            self.verbose_print('Removing file {0} from internal storage...'.format(_printable(filename)))
            del self.files[filename]
        elif filename in self.indexes:
            self.verbose_print('Removing file {0} from archive indexes...'.format(_printable(filename)))
            del self.indexes[filename]
        else:
            raise IOError(errno.ENOENT, 'the requested file {0} does not exist in this archive'.format(_printable(filename)))

    def load(self, filename):
        filename = _unicode(filename)

        if self.handle is not None:
            self.handle.close()
            self.handle = None

        self.file = filename
        self.files = {}
        self.detected_magic = None
        self.handle = open(self.file, 'rb')
        self.version = self.get_version()
        self.indexes = self.extract_indexes()

        if self.version == 1:
            base, _ = os.path.splitext(self.file)
            rpa_path = base + '.rpa'
            if not os.path.exists(rpa_path):
                self.handle.close()
                self.handle = None
                raise IOError(errno.ENOENT, 'companion RPAv1 data file {0} not found'.format(rpa_path))
            self.handle.close()
            self.data_file = rpa_path
            self.handle = open(self.data_file, 'rb')
        else:
            self.data_file = self.file

    def _get_write_magic(self):
        if self.custom_magic is not None:
            return self.custom_magic
        if self.detected_magic is not None:
            return self.detected_magic
        if self.version == 4:
            return self.RPA4_MAGIC
        elif self.version == 3.2:
            return self.RPA3_2_MAGIC
        elif self.version == 3:
            return self.RPA3_MAGIC
        elif self.version == 2:
            return self.RPA2_MAGIC
        return None

    def save(self, filename=None):
        if filename is None:
            filename = self.file
        if filename is None:
            raise ValueError('no target file found for saving archive')
        filename = _unicode(filename)

        if self.version not in (1, 2, 3, 3.2, 4):
            raise ValueError('saving is only supported for version 1, 2, 3, 3.2, and 4 archives')

        self.verbose_print('Rebuilding archive index...')
        files = self.files
        for file in list(self.indexes.keys()):
            content = self.read(file)
            del self.indexes[file]
            files[file] = content

        if self.handle is not None:
            self.handle.close()
            self.handle = None

        if self.version == 1:
            base, _ = os.path.splitext(filename)
            rpi_path = base + '.rpi'
            rpa_path = base + '.rpa'

            offset = 0
            indexes = {}
            self.verbose_print('Writing files to RPAv1 data file {0}...'.format(rpa_path))
            with open(rpa_path, 'wb') as data_archive:
                for file, content in files.items():
                    if self.padlength > 0:
                        padding = self.generate_padding()
                        data_archive.write(padding)
                        offset += len(padding)

                    data_archive.write(content)
                    indexes[file] = [(offset, len(content))]
                    offset += len(content)

            self.verbose_print('Writing RPAv1 index file {0}...'.format(rpi_path))
            with open(rpi_path, 'wb') as idx_archive:
                idx_archive.write(codecs.encode(pickle.dumps(indexes, self.PICKLE_PROTOCOL), 'zlib'))

            self.load(rpi_path)
            return

        magic_str = self._get_write_magic()
        magic_bytes = magic_str.encode('utf-8')

        if self.version in (3, 4):
            header_len = len(magic_bytes) + 16 + 1 + 8 + 1
        elif self.version == 3.2:
            header_len = len(magic_bytes) + 16 + 1 + 8 + 1 + 8 + 1
        else:
            header_len = len(magic_bytes) + 16 + 1

        offset = header_len
        with open(filename, 'wb') as archive:
            archive.seek(offset)

            indexes = {}
            self.verbose_print('Writing files to archive file...')
            for file, content in files.items():
                if self.padlength > 0:
                    padding = self.generate_padding()
                    archive.write(padding)
                    offset += len(padding)

                archive.write(content)
                if self.version in (3, 3.2, 4):
                    indexes[file] = [(offset ^ self.key, len(content) ^ self.key)]
                elif self.version == 2:
                    indexes[file] = [(offset, len(content))]
                offset += len(content)

            self.verbose_print('Writing archive index to archive file...')
            archive.write(codecs.encode(pickle.dumps(indexes, self.PICKLE_PROTOCOL), 'zlib'))

            self.verbose_print('Writing header to archive file... (version = RPAv{0}, magic = {1!r})'.format(
                self.version, magic_str))
            archive.seek(0)
            if self.version in (3, 4):
                header = '{}{:016x} {:08x}\n'.format(magic_str, offset, self.key)
            elif self.version == 3.2:
                header = '{}{:016x} {:08x} {:08x}\n'.format(magic_str, offset, 0, self.key)
            else:
                header = '{}{:016x}\n'.format(magic_str, offset)
            archive.write(header.encode('utf-8'))

        self.load(filename)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description='A tool for working with Ren\'Py archive files.',
        epilog='The FILE argument can optionally be in ARCHIVE=REAL format, mapping a file in the archive file system to a file on your real file system. An example of this: rpatool -x test.rpa script.rpyc=/home/foo/test.rpyc',
        add_help=False)

    parser.add_argument('archive', metavar='ARCHIVE', help='The Ren\'py archive file to operate on.')
    parser.add_argument('files', metavar='FILE', nargs='*', action='append', help='Zero or more files to operate on.')

    parser.add_argument('-l', '--list', action='store_true', help='List files in archive ARCHIVE.')
    parser.add_argument('-x', '--extract', action='store_true', help='Extract FILEs from ARCHIVE.')
    parser.add_argument('--only', metavar='EXT', help='Extract or list only FILEs with specific extension (e.g., .png).')
    parser.add_argument('-c', '--create', action='store_true', help='Create ARCHIVE from FILEs.')
    parser.add_argument('-d', '--delete', action='store_true', help='Delete FILEs from ARCHIVE.')
    parser.add_argument('-a', '--append', action='store_true', help='Append FILEs to ARCHIVE.')

    parser.add_argument('-1', '--one', action='store_true', help='Use the RPAv1 format (.rpi + .rpa) for creating/saving archives.')
    parser.add_argument('-2', '--two', action='store_true', help='Use the RPAv2 format for creating/saving archives.')
    parser.add_argument('-3', '--three', action='store_true', help='Use the RPAv3 format for creating/saving archives (default).')
    parser.add_argument('-32', '--three-two', action='store_true', help='Use the RPAv3.2 format for creating/saving archives.')
    parser.add_argument('-4', '--four', action='store_true', help='Use the RPAv4 format for creating/saving archives.')

    parser.add_argument('--magic', metavar='MAGIC', help='Custom magic header string for reading/writing modded archives (e.g., "ALT-1.0 ").')
    parser.add_argument('--force-version', metavar='VER', type=float, choices=[1.0, 2.0, 3.0, 3.2, 4.0],
                        help='Force archive format version (1, 2, 3, 3.2, or 4) when reading.')
    parser.add_argument('--offset', metavar='OFFSET', help='Override index offset in hexadecimal for reading modded archives.')
    parser.add_argument('-k', '--key', metavar='KEY', help='The obfuscation key used for RPAv3/v3.2/v4 archives, in hexadecimal (default: 0xDEADBEEF).')
    parser.add_argument('-p', '--padding', metavar='COUNT', help='The maximum number of bytes of padding to add between files (default: 0).')
    parser.add_argument('-o', '--outfile', help='An alternative output archive file when appending to or deleting from archives, or output directory when extracting.')

    parser.add_argument('-h', '--help', action='help', help='Print this help and exit.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Be a bit more verbose while performing operations.')
    parser.add_argument('-V', '--version', action='version', version='rpatool v1.2.0', help='Show version information.')
    arguments = parser.parse_args(argv)

    explicit_version = None
    if arguments.one:
        explicit_version = 1
    elif arguments.two:
        explicit_version = 2
    elif arguments.three:
        explicit_version = 3
    elif arguments.three_two:
        explicit_version = 3.2
    elif arguments.four:
        explicit_version = 4

    version = explicit_version if explicit_version is not None else 3
    key = int(arguments.key, 16) if arguments.key is not None else 0xDEADBEEF
    override_key = int(arguments.key, 16) if arguments.key is not None else None
    override_offset = int(arguments.offset, 16) if arguments.offset is not None else None
    force_version = int(arguments.force_version) if arguments.force_version in (1.0, 2.0, 3.0, 4.0) else arguments.force_version
    padding = int(arguments.padding) if arguments.padding is not None else 0

    if arguments.create:
        archive_path = None
        output = _unicode(arguments.archive)
    else:
        archive_path = _unicode(arguments.archive)
        if arguments.outfile is not None:
            output = _unicode(arguments.outfile)
        else:
            output = '.' if arguments.extract else _unicode(arguments.archive)

    if len(arguments.files) > 0 and isinstance(arguments.files[0], list):
        arguments.files = arguments.files[0]

    try:
        archive = RenPyArchive(
            archive_path,
            padlength=padding,
            key=key,
            version=version,
            verbose=arguments.verbose,
            force_version=force_version,
            override_offset=override_offset,
            override_key=override_key,
            custom_magic=arguments.magic,
        )
    except Exception as e:
        print('Could not open archive file {0} for reading: {1}'.format(archive_path, e), file=sys.stderr)
        sys.exit(1)

    if arguments.create or arguments.append:
        def add_file(filename):
            if '=' in filename:
                (outfile, filename) = filename.split('=', 1)
            else:
                outfile = filename

            if os.path.isdir(filename):
                for file in os.listdir(filename):
                    add_file(outfile + os.sep + file + '=' + filename + os.sep + file)
            else:
                try:
                    with open(filename, 'rb') as file:
                        archive.add(outfile, file.read())
                except Exception as e:
                    print('Could not add file {0} to archive: {1}'.format(filename, e), file=sys.stderr)

        for filename in arguments.files:
            add_file(_unicode(filename))

        if arguments.create or explicit_version is not None:
            archive.version = version
        try:
            archive.save(output)
        except Exception as e:
            print('Could not save archive file: {0}'.format(e), file=sys.stderr)

    elif arguments.delete:
        for filename in arguments.files:
            try:
                archive.remove(filename)
            except Exception as e:
                print('Could not delete file {0} from archive: {1}'.format(filename, e), file=sys.stderr)

        if explicit_version is not None:
            archive.version = version
        try:
            archive.save(output)
        except Exception as e:
            print('Could not save archive file: {0}'.format(e), file=sys.stderr)

    elif arguments.extract:
        if len(arguments.files) > 0:
            files = arguments.files
        else:
            files = archive.list()

        if arguments.only:
            files = [f for f in files if f.lower().endswith(arguments.only.lower())]

        if output and not os.path.exists(output):
            os.makedirs(output)

        for filename in files:
            if '=' in filename:
                (outfile, filename) = filename.split('=', 1)
            else:
                outfile = filename

            try:
                contents = archive.read(filename)
                safe_outfile = sanitize_extract_path(output, outfile)
                target_path = os.path.join(output, safe_outfile)
                out_dir = os.path.dirname(target_path)
                if out_dir and not os.path.exists(out_dir):
                    os.makedirs(out_dir)

                with open(target_path, 'wb') as file:
                    file.write(contents)
            except Exception as e:
                print('Could not extract file {0} from archive: {1}'.format(filename, e), file=sys.stderr)

    elif arguments.list:
        files = archive.list()
        files.sort()
        if arguments.only:
            files = [f for f in files if f.lower().endswith(arguments.only.lower())]
        for file in files:
            print(file)
    else:
        print('No operation given :(')
        print('Use {0} --help for usage details.'.format(sys.argv[0]))


if __name__ == '__main__':
    main()
