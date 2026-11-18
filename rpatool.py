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
    """Sanitize archive member paths so extraction cannot escape base_dir."""
    normalized = archive_path.replace('\\', '/')
    _, normalized = os.path.splitdrive(normalized)
    normalized = normalized.lstrip('/')

    parts = []
    for part in normalized.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if parts:
                parts.pop()
            continue
        parts.append(part)

    if not parts:
        raise ValueError('invalid or unsafe archive entry path: {0}'.format(archive_path))

    rel_path = os.path.join(*parts)
    abs_base = os.path.abspath(base_dir)
    abs_target = os.path.abspath(os.path.join(abs_base, rel_path))

    if os.path.commonpath([abs_base, abs_target]) != abs_base:
        raise ValueError('path traversal blocked for entry: {0}'.format(archive_path))

    return rel_path


class RenPyArchive:
    RPA2_MAGIC = 'RPA-2.0 '
    RPA3_MAGIC = 'RPA-3.0 '
    RPA3_2_MAGIC = 'RPA-3.2 '
    RPA4_MAGIC = 'RPA-4.0 '

    KNOWN_MAGICS = {
        RPA4_MAGIC: 4,
        RPA3_2_MAGIC: 3.2,
        RPA3_MAGIC: 3,
        RPA2_MAGIC: 2,
    }

    PICKLE_PROTOCOL = 2

    def __init__(self, file=None, version=3, padlength=0, key=0xDEADBEEF,
                 verbose=False, force_version=None, forced_offset=None,
                 forced_key=None, custom_magic=None):
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
        self.forced_offset = forced_offset
        self.forced_key = forced_key
        self.custom_magic = self._normalize_magic(custom_magic)
        self.detected_magic = None

        if file is not None:
            self.load(file)

    def __del__(self):
        self.close()

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    @staticmethod
    def _normalize_magic(magic):
        if magic is None:
            return None
        if not magic.endswith(' '):
            magic = magic + ' '
        return magic

    def _resolve_rpa1_paths(self, filename):
        base, ext = os.path.splitext(filename)
        ext_lower = ext.lower()
        if ext_lower == '.rpi':
            return filename, base + '.rpa'
        elif ext_lower == '.rpa':
            return base + '.rpi', filename
        return filename + '.rpi', filename

    def get_magic_for_version(self, version):
        if self.custom_magic is not None:
            return self.custom_magic
        if version == 4:
            return self.RPA4_MAGIC
        elif version == 3.2:
            return self.RPA3_2_MAGIC
        elif version == 3:
            return self.RPA3_MAGIC
        elif version == 2:
            return self.RPA2_MAGIC
        return ''

    def get_version(self):
        if self.force_version is not None:
            if self.custom_magic is not None:
                self.detected_magic = self.custom_magic
            return self.force_version

        self.handle.seek(0)
        raw_header = self.handle.readline()
        try:
            magic_line = raw_header.decode('utf-8')
        except UnicodeDecodeError:
            magic_line = raw_header.decode('latin1')

        # Check custom magic first if supplied
        if self.custom_magic is not None and magic_line.startswith(self.custom_magic):
            self.detected_magic = self.custom_magic
            tokens = magic_line[len(self.custom_magic):].strip().split()
            if len(tokens) >= 3:
                return 3.2
            elif len(tokens) == 2:
                return 3
            elif len(tokens) == 1:
                return 2
            raise ValueError('could not infer archive layout from custom magic header')

        # Check standard and known variant magic headers
        for magic_prefix, ver in self.KNOWN_MAGICS.items():
            if magic_line.startswith(magic_prefix):
                self.detected_magic = magic_prefix
                return ver

        # Check RPAv1 (.rpi index file or companion .rpi alongside .rpa)
        if self.file:
            rpi_path, _ = self._resolve_rpa1_paths(self.file)
            if self.file.lower().endswith('.rpi') or os.path.exists(rpi_path):
                return 1

        raise ValueError(
            'the given file is not a valid Ren\'Py archive, or uses an unknown magic header '
            '(try --magic or --force-version)'
        )

    def extract_indexes(self):
        self.handle.seek(0)

        if self.version in (2, 3, 3.2, 4):
            metadata = self.handle.readline()
            try:
                meta_str = metadata.decode('utf-8')
            except UnicodeDecodeError:
                meta_str = metadata.decode('latin1')

            magic = self.detected_magic or self.get_magic_for_version(self.version)
            if magic and meta_str.startswith(magic):
                remainder = meta_str[len(magic):].strip()
                vals = remainder.split()
            else:
                parts = meta_str.strip().split()
                vals = parts[1:] if len(parts) > 1 else []

            if self.forced_offset is not None:
                offset = self.forced_offset
            else:
                if len(vals) < 1:
                    raise ValueError('archive header is missing index offset')
                offset = int(vals[0], 16)

            if self.forced_key is not None:
                self.key = self.forced_key
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
                            entries.append((entry[0] ^ self.key, entry[1] ^ self.key))
                        else:
                            entries.append((entry[0] ^ self.key, entry[1] ^ self.key, entry[2]))
                    indexes[i] = entries
        elif self.version == 1:
            rpi_path, rpa_path = self._resolve_rpa1_paths(self.file)
            if not os.path.exists(rpi_path):
                raise IOError(errno.ENOENT, 'missing RPAv1 index file: {0}'.format(rpi_path))
            if not os.path.exists(rpa_path):
                raise IOError(errno.ENOENT, 'missing RPAv1 data file: {0}'.format(rpa_path))

            with open(rpi_path, 'rb') as rpi_handle:
                indexes = _unpickle(codecs.decode(rpi_handle.read(), 'zlib'))

            if self.file != rpa_path:
                self.close()
                self.data_file = rpa_path
                self.handle = open(rpa_path, 'rb')
        else:
            raise ValueError('unsupported Ren\'Py archive version: {0}'.format(self.version))

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

        chunks = []
        source_name = self.data_file or self.file
        for entry in self.indexes[filename]:
            if len(entry) == 3:
                (offset, length, prefix) = entry
            else:
                (offset, length) = entry
                prefix = b''

            self.verbose_print('Reading file {0} from data file {1}... (offset = {2}, length = {3} bytes)'.format(
                _printable(filename), source_name, offset, length))
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
        self.close()
        self.file = filename
        self.data_file = filename
        self.files = {}
        self.handle = open(self.file, 'rb')
        self.version = self.get_version()
        self.indexes = self.extract_indexes()

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

        self.close()

        if self.version == 1:
            rpi_path, rpa_path = self._resolve_rpa1_paths(filename)
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
            with open(rpi_path, 'wb') as index_archive:
                index_archive.write(codecs.encode(pickle.dumps(indexes, self.PICKLE_PROTOCOL), 'zlib'))

            self.load(rpi_path)
            return

        magic = self.get_magic_for_version(self.version)
        if self.version in (3, 4):
            header_template = magic + '{:016x} {:08x}\n'
        elif self.version == 3.2:
            header_template = magic + '{:016x} 00000000 {:08x}\n'
        else:
            header_template = magic + '{:016x}\n'

        offset = len(header_template.format(0, self.key).encode('utf-8'))

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
                else:
                    indexes[file] = [(offset, len(content))]
                offset += len(content)

            self.verbose_print('Writing archive index to archive file...')
            archive.write(codecs.encode(pickle.dumps(indexes, self.PICKLE_PROTOCOL), 'zlib'))

            self.verbose_print('Writing header to archive file... (version = RPAv{0}, magic = {1!r})'.format(
                self.version, magic))
            archive.seek(0)
            header_bytes = header_template.format(offset, self.key).encode('utf-8')
            archive.write(header_bytes)

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

    parser.add_argument('-1', '--one', action='store_true', help='Use the RPAv1 (.rpi + .rpa) format for creating/saving archives.')
    parser.add_argument('-2', '--two', action='store_true', help='Use the RPAv2 format for creating/saving archives.')
    parser.add_argument('-3', '--three', action='store_true', help='Use the RPAv3 format for creating/saving archives (default).')
    parser.add_argument('-32', '--three-two', action='store_true', help='Use the RPAv3.2 format for creating/saving archives.')
    parser.add_argument('-4', '--four', action='store_true', help='Use the RPAv4 format for creating/saving archives.')

    parser.add_argument('--magic', metavar='STR', default=None,
                        help='Custom magic header prefix for reading/writing modded archives (e.g., "ALT-1.0 ").')
    parser.add_argument('--force-version', type=float, choices=[1, 2, 3, 3.2, 4], default=None,
                        help='Force treating ARCHIVE as a specific RPA version when reading.')
    parser.add_argument('--offset', metavar='HEX', default=None,
                        help='Override the index offset in hexadecimal when reading modded headers.')
    parser.add_argument('-k', '--key', metavar='KEY', default=None,
                        help='The obfuscation key used for creating or overriding RPAv3/3.2/4 archives, in hexadecimal (default: 0xDEADBEEF).')
    parser.add_argument('-p', '--padding', metavar='COUNT', default=None,
                        help='The maximum number of bytes of padding to add between files (default: 0).')
    parser.add_argument('-o', '--outfile', default=None,
                        help='An alternative output archive file when appending to or deleting from archives, or output directory when extracting.')

    parser.add_argument('-h', '--help', action='help', help='Print this help and exit.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Be a bit more verbose while performing operations.')
    parser.add_argument('-V', '--version', action='version', version='rpatool v1.1.0', help='Show version information.')
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
    forced_key = int(arguments.key, 16) if arguments.key is not None else None
    key = forced_key if forced_key is not None else 0xDEADBEEF
    forced_offset = int(arguments.offset, 16) if arguments.offset is not None else None
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
            verbose=arguments.verbose,
            version=version,
            force_version=arguments.force_version,
            forced_offset=forced_offset,
            forced_key=forced_key if not arguments.create else None,
            custom_magic=arguments.magic
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

        if explicit_version is not None:
            archive.version = explicit_version
        if forced_key is not None:
            archive.key = forced_key
        if arguments.magic is not None:
            archive.custom_magic = RenPyArchive._normalize_magic(arguments.magic)

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
            archive.version = explicit_version
        if forced_key is not None:
            archive.key = forced_key
        if arguments.magic is not None:
            archive.custom_magic = RenPyArchive._normalize_magic(arguments.magic)

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
