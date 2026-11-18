#!/usr/bin/env python3

from __future__ import print_function

import sys
import os
import codecs
import errno
import random
import fnmatch
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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


def normalize_magic(magic):
    if magic is None:
        return None
    magic = _unicode(magic)
    if not magic.endswith(' '):
        magic += ' '
    return magic


def split_archive_mapping(spec):
    """
    Split an optional ARCHIVE=REAL mapping while safely supporting filenames that contain '='.
    If the full string already exists on disk (or in archive), treat it as a literal path;
    otherwise split on '=' where the right-hand side exists on disk (for packing) or fallback to first '='.
    """
    if '=' not in spec:
        return spec, spec

    if os.path.exists(spec):
        return spec, spec

    idx = spec.find('=')
    while idx != -1:
        left = spec[:idx]
        right = spec[idx + 1:]
        if os.path.exists(right):
            return left, right
        idx = spec.find('=', idx + 1)

    return spec.split('=', 1)


def sanitize_extract_path(base_dir, archive_path):
    """
    Sanitize an archive-internal path so it cannot escape base_dir via leading slashes,
    Windows drive letters, or '..' traversal components.
    """
    base_abs = os.path.abspath(base_dir if base_dir else '.')
    cleaned = _unicode(archive_path).replace('\\', '/')
    _, cleaned = os.path.splitdrive(cleaned)
    cleaned = cleaned.lstrip('/')

    safe_parts = []
    for part in cleaned.split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if safe_parts:
                safe_parts.pop()
            continue
        safe_parts.append(part)

    if not safe_parts:
        raise ValueError('unsafe or empty archive path: {0}'.format(_printable(archive_path)))

    target_abs = os.path.abspath(os.path.join(base_abs, *safe_parts))
    try:
        common = os.path.commonpath([base_abs, target_abs])
    except ValueError:
        raise ValueError('archive path escapes target directory: {0}'.format(_printable(archive_path)))

    if common != base_abs or target_abs == base_abs:
        raise ValueError('archive path escapes target directory: {0}'.format(_printable(archive_path)))

    return target_abs


def _compile_pattern_list(pattern_spec):
    if not pattern_spec:
        return []

    matchers = []
    for raw_token in _unicode(pattern_spec).split(','):
        token = raw_token.strip()
        if not token:
            continue
        token_norm = token.replace('\\', '/').lower()
        has_wildcard = any(ch in token_norm for ch in ('*', '?', '['))
        has_slash = '/' in token_norm

        if has_wildcard or has_slash:
            matchers.append(('glob', token_norm))
        else:
            ext = token_norm if token_norm.startswith('.') else '.' + token_norm
            matchers.append(('ext', ext))

    return matchers


def _matches_any(filepath, matchers):
    f_norm = _unicode(filepath).replace('\\', '/').lower()
    f_base = os.path.basename(f_norm)
    for kind, val in matchers:
        if kind == 'ext':
            if f_norm.endswith(val):
                return True
        elif kind == 'glob':
            if fnmatch.fnmatchcase(f_norm, val) or ('/' not in val and fnmatch.fnmatchcase(f_base, val)):
                return True
    return False


def filter_files(files, only_spec=None, exclude_spec=None):
    result = list(files)

    only_matchers = _compile_pattern_list(only_spec)
    if only_matchers:
        result = [f for f in result if _matches_any(f, only_matchers)]

    exclude_matchers = _compile_pattern_list(exclude_spec)
    if exclude_matchers:
        result = [f for f in result if not _matches_any(f, exclude_matchers)]

    return result


def format_size(num_bytes):
    size = float(num_bytes)
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if size < 1024.0 or unit == 'T':
            if unit == 'B':
                return '{0}B'.format(int(size))
            return '{0:.1f}{1}'.format(size, unit)
        size /= 1024.0
    return '{0}B'.format(int(num_bytes))


def format_size_long(num_bytes):
    size = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024.0 or unit == 'TB':
            if unit == 'B':
                return '{0} B'.format(int(size))
            return '{0:.2f} {1} ({2:,} bytes)'.format(size, unit, int(num_bytes))
        size /= 1024.0
    return '{0} B'.format(int(num_bytes))


def middle_truncate(text, max_len):
    if max_len <= 0:
        return ''
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return '.' * max_len
    left = (max_len - 3) // 2
    right = max_len - 3 - left
    return text[:left] + '...' + text[-right:]


class ProgressReporter:
    MAX_COLS = 60

    def __init__(self, total, action='Working', enabled=True, verbose=False, stream=None):
        self.total = max(0, int(total))
        self.action = action
        self.enabled = bool(enabled)
        self.verbose = bool(verbose)
        self.stream = stream if stream is not None else sys.stderr
        self.current = 0
        self.bytes_done = 0
        self.is_tty = hasattr(self.stream, 'isatty') and self.stream.isatty()
        self._last_line_len = 0
        self._lock = threading.Lock()

    def update(self, item_name='', nbytes=0, step=1):
        if not self.enabled:
            return
        with self._lock:
            self.current += step
            self.bytes_done += nbytes
            self._render(item_name)

    def _format_line(self, item_name):
        total = max(self.total, self.current, 1)
        pct = min(100, int((self.current * 100) / total))
        bar_width = 14
        filled = int((bar_width * min(self.current, total)) / total)
        bar = '#' * filled + '-' * (bar_width - filled)

        prefix = '{0} [{1}] {2}/{3} {4:3d}% {5}'.format(
            self.action,
            bar,
            self.current,
            self.total,
            pct,
            format_size(self.bytes_done),
        )
        rem = self.MAX_COLS - len(prefix)
        if item_name and rem >= 5:
            short_name = middle_truncate(os.path.basename(_printable(item_name)), rem - 1)
            line = prefix + ' ' + short_name
        else:
            line = prefix[:self.MAX_COLS]
        return line[:self.MAX_COLS]

    def _render(self, item_name=''):
        line = self._format_line(item_name)

        if self.is_tty and not self.verbose:
            pad = max(0, self._last_line_len - len(line))
            self.stream.write('\r' + line + (' ' * pad))
            self.stream.flush()
            self._last_line_len = len(line)
        else:
            step_interval = max(1, self.total // 10) if self.total > 0 else 1
            if self.current == 1 or self.current >= self.total or (self.current % step_interval) == 0:
                self.stream.write(line + '\n')
                self.stream.flush()

    def finish(self):
        if not self.enabled:
            return
        with self._lock:
            if self.is_tty and not self.verbose and self._last_line_len > 0:
                self.stream.write('\n')
                self.stream.flush()
                self._last_line_len = 0


class RenPyArchive:
    RPA2_MAGIC = 'RPA-2.0 '
    RPA3_MAGIC = 'RPA-3.0 '
    RPA3_2_MAGIC = 'RPA-3.2 '
    RPA4_MAGIC = 'RPA-4.0 '

    KNOWN_MODDED_MAGICS = (
        'ALT-1.0 ',
        'ZiX-12A ',
        'ZiX-12B ',
        'EX2-1.0 ',
        'RPA-2.1 ',
        'RPA-3.1 ',
        'RPA-4.1 ',
        'RPA-5.0 ',
    )

    PICKLE_PROTOCOL = 2

    def __init__(self, file=None, version=3, padlength=0, key=0xDEADBEEF, verbose=False,
                 force_version=None, force_offset=None, force_key=None, custom_magic=None):
        self.file = None
        self.data_file = None
        self.handle = None
        self.files = {}
        self.indexes = {}
        self.version = version
        self.padlength = padlength
        self.key = key
        self.index_offset = None
        self.verbose = verbose
        self.force_version = force_version
        self.force_offset = force_offset
        self.force_key = force_key
        self.custom_magic = normalize_magic(custom_magic)
        self.detected_magic = None

        if file is not None:
            self.load(file)

    def __del__(self):
        self._close_handle()

    def _close_handle(self):
        if self.handle is not None:
            try:
                self.handle.close()
            except Exception:
                pass
            self.handle = None

    def _resolve_v1_paths(self, filename):
        base, ext = os.path.splitext(filename)
        ext_lower = ext.lower()
        if ext_lower == '.rpi':
            return filename, base + '.rpa'
        elif ext_lower == '.rpa':
            return base + '.rpi', filename
        return filename + '.rpi', filename

    def _infer_version_from_header_line(self, header_line, prefix_len):
        rest = header_line[prefix_len:].strip()
        tokens = rest.split()
        if len(tokens) >= 3:
            return 3.2
        elif len(tokens) == 2:
            return 3
        elif len(tokens) == 1:
            return 2
        raise ValueError('could not infer archive format from custom magic header')

    def get_version(self):
        if self.force_version is not None:
            return self.force_version

        self.handle.seek(0)
        raw_line = self.handle.readline()
        magic = raw_line.decode('utf-8', errors='replace')

        if self.custom_magic is not None:
            if magic.startswith(self.custom_magic):
                self.detected_magic = self.custom_magic
                return self._infer_version_from_header_line(magic, len(self.custom_magic))
            elif self.force_offset is not None:
                self.detected_magic = self.custom_magic
                return 3 if self.force_key is not None else 2
            raise ValueError('archive header does not match expected custom magic {0!r}'.format(self.custom_magic))

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

        for known_magic in self.KNOWN_MODDED_MAGICS:
            if magic.startswith(known_magic):
                self.detected_magic = known_magic
                return self._infer_version_from_header_line(magic, len(known_magic))

        if self.file:
            base, ext = os.path.splitext(self.file)
            if ext.lower() == '.rpi':
                return 1
            if ext.lower() == '.rpa' and os.path.exists(base + '.rpi'):
                return 1

        raise ValueError('the given file is not a valid Ren\'Py archive, or an unsupported version')

    def _normalize_index_entries(self, raw_indexes, key=None):
        normalized = {}
        for path, entries in raw_indexes.items():
            norm_path = self.convert_filename(_unicode(path))
            norm_entries = []
            for entry in entries:
                if len(entry) == 2:
                    offset, length = entry
                    if key is not None:
                        offset ^= key
                        length ^= key
                    norm_entries.append((offset, length, b''))
                elif len(entry) >= 3:
                    offset, length, prefix = entry[0], entry[1], entry[2]
                    if key is not None:
                        offset ^= key
                        length ^= key
                    norm_entries.append((offset, length, _unmangle(prefix)))
            normalized[norm_path] = norm_entries
        return normalized

    def extract_indexes(self):
        self.handle.seek(0)

        if self.version in (2, 3, 3.2, 4):
            metadata = self.handle.readline().decode('utf-8', errors='replace')
            header_prefix = self.detected_magic
            if header_prefix is None:
                default_magics = {
                    2: self.RPA2_MAGIC,
                    3: self.RPA3_MAGIC,
                    3.2: self.RPA3_2_MAGIC,
                    4: self.RPA4_MAGIC,
                }
                header_prefix = default_magics.get(self.version, '')

            if header_prefix and metadata.startswith(header_prefix):
                vals = metadata[len(header_prefix):].strip().split()
            else:
                vals = metadata.split()[1:]

            if self.force_offset is not None:
                offset = self.force_offset
            else:
                if not vals:
                    raise ValueError('missing index offset in archive header')
                offset = int(vals[0], 16)

            self.index_offset = offset

            if self.version in (3, 3.2, 4):
                if self.force_key is not None:
                    self.key = self.force_key
                else:
                    self.key = 0
                    subkeys = vals[2:] if self.version == 3.2 else vals[1:]
                    for subkey in subkeys:
                        self.key ^= int(subkey, 16)

            self.handle.seek(offset)
            contents = codecs.decode(self.handle.read(), 'zlib')
            raw_indexes = _unpickle(contents)
            deobf_key = self.key if self.version in (3, 3.2, 4) else None
            return self._normalize_index_entries(raw_indexes, key=deobf_key)

        elif self.version == 1:
            rpi_path, rpa_path = self._resolve_v1_paths(self.file)
            if not os.path.exists(rpi_path):
                raise IOError(errno.ENOENT, 'missing RPAv1 index file: {0}'.format(rpi_path))
            with open(rpi_path, 'rb') as rpi_handle:
                raw_indexes = _unpickle(codecs.decode(rpi_handle.read(), 'zlib'))

            self.index_offset = None
            if os.path.abspath(self.file) != os.path.abspath(rpa_path):
                self._close_handle()
                self.data_file = rpa_path
                if os.path.exists(rpa_path):
                    self.handle = open(rpa_path, 'rb')
            else:
                self.data_file = rpa_path

            return self._normalize_index_entries(raw_indexes, key=None)

        raise ValueError('unsupported archive version: {0}'.format(self.version))

    def generate_padding(self):
        length = random.randint(1, self.padlength)
        return os.urandom(length)

    def convert_filename(self, filename):
        (_, filename) = os.path.splitdrive(os.path.normpath(filename).replace(os.sep, '/'))
        return filename.lstrip('/')

    def verbose_print(self, message):
        if self.verbose:
            print(message)

    def list(self):
        return list(self.indexes.keys()) + list(self.files.keys())

    def has_file(self, filename):
        filename = self.convert_filename(_unicode(filename))
        return filename in self.indexes or filename in self.files

    def get_file_size(self, filename):
        filename = self.convert_filename(_unicode(filename))
        if filename in self.files:
            return len(self.files[filename])
        if filename in self.indexes:
            return sum(length for _, length, _ in self.indexes[filename])
        raise IOError(errno.ENOENT, 'the requested file {0} does not exist in the given Ren\'Py archive'.format(
            _printable(filename)))

    def get_info(self, only_spec=None, exclude_spec=None):
        all_files = sorted(self.list())
        filtered_files = filter_files(all_files, only_spec=only_spec, exclude_spec=exclude_spec)

        total_uncompressed = 0
        for f in all_files:
            total_uncompressed += self.get_file_size(f)

        filtered_uncompressed = 0
        ext_counts = {}
        for f in filtered_files:
            filtered_uncompressed += self.get_file_size(f)
            _, ext = os.path.splitext(f.lower())
            ext_label = ext if ext else '(no ext)'
            ext_counts[ext_label] = ext_counts.get(ext_label, 0) + 1

        archive_size = 0
        if self.file and os.path.exists(self.file):
            archive_size += os.path.getsize(self.file)
        if self.data_file and self.file and os.path.abspath(self.data_file) != os.path.abspath(self.file):
            if os.path.exists(self.data_file):
                archive_size += os.path.getsize(self.data_file)

        magic_str = self.detected_magic
        if magic_str is None:
            magic_str = self._get_write_magic() or 'RPAv1 (.rpi/.rpa)'

        return {
            'file': self.file,
            'data_file': self.data_file,
            'version': self.version,
            'magic': magic_str.rstrip(' ') if magic_str != 'RPAv1 (.rpi/.rpa)' else magic_str,
            'raw_magic': magic_str,
            'key': self.key if self.version in (3, 3.2, 4) else None,
            'index_offset': self.index_offset,
            'archive_size': archive_size,
            'total_files': len(all_files),
            'total_uncompressed': total_uncompressed,
            'filtered_files': len(filtered_files),
            'filtered_uncompressed': filtered_uncompressed,
            'extensions': sorted(ext_counts.items(), key=lambda kv: (-kv[1], kv[0])),
        }

    def read(self, filename, handle=None):
        filename = self.convert_filename(_unicode(filename))

        if filename not in self.files and filename not in self.indexes:
            raise IOError(errno.ENOENT, 'the requested file {0} does not exist in the given Ren\'Py archive'.format(
                _printable(filename)))

        if filename in self.files:
            self.verbose_print('Reading file {0} from internal storage...'.format(_printable(filename)))
            return self.files[filename]

        active_handle = handle if handle is not None else self.handle
        if active_handle is None:
            raise IOError(errno.ENOENT, 'data archive handle is not open for reading {0}'.format(
                _printable(filename)))

        chunks = []
        for offset, length, prefix in self.indexes[filename]:
            self.verbose_print('Reading file {0} from data file {1}... (offset = {2}, length = {3} bytes)'.format(
                _printable(filename), self.data_file or self.file, offset, length))
            active_handle.seek(offset)
            read_len = max(0, length - len(prefix))
            chunks.append(prefix + active_handle.read(read_len))

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
        self._close_handle()
        self.file = filename
        self.data_file = filename
        self.files = {}
        self.detected_magic = None
        self.index_offset = None
        self.handle = open(self.file, 'rb')
        self.version = self.get_version()
        self.indexes = self.extract_indexes()

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
        return ''

    def save(self, filename=None, progress=None):
        if filename is None:
            filename = self.file
        if filename is None:
            raise ValueError('no target file found for saving archive')
        filename = _unicode(filename)

        if self.version not in (1, 2, 3, 3.2, 4):
            raise ValueError('saving is only supported for version 1, 2, 3, 3.2, and 4 archives')

        self.verbose_print('Rebuilding archive index...')
        files = self.files
        existing_keys = list(self.indexes.keys())
        read_progress = ProgressReporter(
            total=len(existing_keys),
            action='Read',
            enabled=bool(progress),
            verbose=self.verbose,
        )
        for file in existing_keys:
            content = self.read(file)
            del self.indexes[file]
            files[file] = content
            read_progress.update(file, len(content))
        read_progress.finish()

        self._close_handle()

        write_progress = ProgressReporter(
            total=len(files),
            action='Save',
            enabled=bool(progress),
            verbose=self.verbose,
        )

        if self.version == 1:
            rpi_path, rpa_path = self._resolve_v1_paths(filename)
            offset = 0
            indexes = {}
            self.verbose_print('Writing files to RPAv1 data file {0}...'.format(rpa_path))
            with open(rpa_path, 'wb') as data_out:
                for file, content in files.items():
                    if self.padlength > 0:
                        padding = self.generate_padding()
                        data_out.write(padding)
                        offset += len(padding)
                    data_out.write(content)
                    indexes[file] = [(offset, len(content))]
                    offset += len(content)
                    write_progress.update(file, len(content))

            write_progress.finish()
            self.verbose_print('Writing RPAv1 index file {0}...'.format(rpi_path))
            with open(rpi_path, 'wb') as rpi_out:
                rpi_out.write(codecs.encode(pickle.dumps(indexes, self.PICKLE_PROTOCOL), 'zlib'))

            self.load(rpi_path)
            return

        magic = self._get_write_magic()
        if self.version in (3, 4):
            header_template = '{}{:016x} {:08x}\n'
            offset = len(magic) + 26
        elif self.version == 3.2:
            header_template = '{}{:016x} 00000000 {:08x}\n'
            offset = len(magic) + 35
        else:
            header_template = '{}{:016x}\n'
            offset = len(magic) + 17

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
                write_progress.update(file, len(content))

            write_progress.finish()
            self.verbose_print('Writing archive index to archive file...')
            archive.write(codecs.encode(pickle.dumps(indexes, self.PICKLE_PROTOCOL), 'zlib'))

            self.verbose_print('Writing header to archive file... (version = RPAv{0}, magic = {1!r})'.format(
                self.version, magic))
            archive.seek(0)
            if self.version in (3, 3.2, 4):
                header = header_template.format(magic, offset, self.key)
            else:
                header = header_template.format(magic, offset)
            archive.write(codecs.encode(header, 'utf-8'))

        self.load(filename)


def parse_version_arg(val):
    val = str(val).strip().lower()
    if val in ('1', '1.0'):
        return 1
    elif val in ('2', '2.0'):
        return 2
    elif val in ('3', '3.0'):
        return 3
    elif val in ('3.2', '32'):
        return 3.2
    elif val in ('4', '4.0'):
        return 4
    raise ValueError('unsupported archive version: {0}'.format(val))


def collect_input_files(specs):
    """
    Expand input file and directory arguments into a flat list of (archive_path, real_path) pairs.
    """
    collected = []

    def _walk(filename):
        (outfile, real_filename) = split_archive_mapping(filename)
        if os.path.isdir(real_filename):
            for entry in sorted(os.listdir(real_filename)):
                _walk(outfile + os.sep + entry + '=' + real_filename + os.sep + entry)
        else:
            collected.append((outfile, real_filename))

    for spec in specs:
        _walk(_unicode(spec))
    return collected


def resolve_worker_count(jobs_arg):
    if jobs_arg is None:
        return 1
    jobs = int(jobs_arg)
    if jobs <= 0:
        cpu = os.cpu_count() or 4
        return min(8, max(2, cpu))
    return jobs


def format_archive_info(info, has_filter=False):
    lines = [
        'Archive      : {0}'.format(_printable(info['file'])),
    ]
    if info['data_file'] and info['file'] and os.path.abspath(info['data_file']) != os.path.abspath(info['file']):
        lines.append('Data File    : {0}'.format(_printable(info['data_file'])))

    lines.extend([
        'Format       : RPAv{0}'.format(info['version']),
        'Magic Header : {0!r}'.format(info['magic']),
        'XOR Key      : {0}'.format('0x{0:08X}'.format(info['key']) if info['key'] is not None else 'None (unobfuscated)'),
        'Index Offset : {0}'.format('0x{0:X} ({0:,} bytes)'.format(info['index_offset']) if info['index_offset'] is not None else 'Separate .rpi file'),
        'Archive Size : {0}'.format(format_size_long(info['archive_size'])),
        'Total Files  : {0:,}'.format(info['total_files']),
        'Payload Size : {0}'.format(format_size_long(info['total_uncompressed'])),
    ])

    if has_filter:
        lines.append('Matched Files: {0:,} ({1})'.format(
            info['filtered_files'],
            format_size_long(info['filtered_uncompressed']),
        ))

    if info['extensions']:
        top_exts = ', '.join('{0} ({1})'.format(ext, cnt) for ext, cnt in info['extensions'][:8])
        if len(info['extensions']) > 8:
            top_exts += ', ...'
        lines.append('File Types   : {0}'.format(top_exts))

    return '\n'.join(lines)


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description='A tool for working with Ren\'Py archive files (RPAv1, RPAv2, RPAv3, RPAv3.2, RPAv4, and modded headers).',
        epilog='The FILE argument can optionally be in ARCHIVE=REAL format, mapping a file in the archive file system to a file on your real file system. An example of this: rpatool -x test.rpa script.rpyc=/home/foo/test.rpyc',
        add_help=False)

    parser.add_argument('archive', metavar='ARCHIVE', help='The Ren\'Py archive file to operate on.')
    parser.add_argument('files', metavar='FILE', nargs='*', action='append', help='Zero or more files to operate on.')

    parser.add_argument('-i', '--info', action='store_true', help='Display archive metadata (version, magic header, XOR key, file count, and size).')
    parser.add_argument('-l', '--list', action='store_true', help='List files in archive ARCHIVE.')
    parser.add_argument('-x', '--extract', action='store_true', help='Extract FILEs from ARCHIVE.')
    parser.add_argument('--only', metavar='PATTERNS', help='Include only matching extensions or wildcards, comma-separated (e.g., .png,.webp or images/*).')
    parser.add_argument('--exclude', metavar='PATTERNS', help='Exclude matching extensions or wildcards, comma-separated (e.g., .rpyc,.txt or cache/*).')
    parser.add_argument('-c', '--create', action='store_true', help='Create ARCHIVE from FILEs.')
    parser.add_argument('-d', '--delete', action='store_true', help='Delete FILEs from ARCHIVE.')
    parser.add_argument('-a', '--append', action='store_true', help='Append FILEs to ARCHIVE.')

    parser.add_argument('-1', '--one', action='store_true', help='Use the RPAv1 (.rpi + .rpa) format for creating/saving archives.')
    parser.add_argument('-2', '--two', action='store_true', help='Use the RPAv2 format for creating/saving archives.')
    parser.add_argument('-3', '--three', action='store_true', help='Use the RPAv3 format for creating/saving archives (default).')
    parser.add_argument('-32', '--three-two', dest='three_two', action='store_true', help='Use the RPAv3.2 format for creating/saving archives.')
    parser.add_argument('-4', '--four', action='store_true', help='Use the RPAv4 format for creating/saving archives.')

    parser.add_argument('--magic', metavar='HEADER', help='Custom magic header prefix for modded archives (e.g., "ALT-1.0 ").')
    parser.add_argument('--force-version', metavar='VER', help='Override detected archive version (1, 2, 3, 3.2, or 4) for custom headers.')
    parser.add_argument('--offset', metavar='HEX', help='Override index offset in hexadecimal for non-standard headers.')
    parser.add_argument('-k', '--key', metavar='KEY', help='The obfuscation key used for RPAv3/v3.2/v4 archives, in hexadecimal (default: 0xDEADBEEF).')
    parser.add_argument('-p', '--padding', metavar='COUNT', help='The maximum number of bytes of padding to add between files (default: 0).')
    parser.add_argument('-j', '--jobs', metavar='N', type=int, default=1, help='Number of parallel worker threads for extraction (default: 1, 0 = auto).')
    parser.add_argument('-o', '--outfile', help='An alternative output archive file when appending to or deleting from archives, or output directory when extracting.')
    parser.add_argument('--dry-run', dest='dry_run', action='store_true', help='Simulate operations and preview planned actions without modifying any files on disk.')
    parser.add_argument('--progress', action='store_true', help='Show a compact 60-column progress indicator during archive operations.')

    parser.add_argument('-h', '--help', action='help', help='Print this help and exit.')
    parser.add_argument('-v', '--verbose', action='store_true', help='Be a bit more verbose while performing operations.')
    parser.add_argument('-V', '--version', action='version', version='rpatool v1.5.0', help='Show version information.')
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

    try:
        force_version = parse_version_arg(arguments.force_version) if arguments.force_version is not None else None
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    target_version = explicit_version if explicit_version is not None else (force_version if force_version is not None else 3)
    key = int(arguments.key, 16) if arguments.key is not None else 0xDEADBEEF
    force_key = int(arguments.key, 16) if arguments.key is not None else None
    force_offset = int(arguments.offset, 16) if arguments.offset is not None else None
    padding = int(arguments.padding) if arguments.padding is not None else 0
    workers = resolve_worker_count(arguments.jobs)
    dry_run = bool(arguments.dry_run)

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
            version=target_version,
            verbose=arguments.verbose,
            force_version=force_version,
            force_offset=force_offset,
            force_key=force_key,
            custom_magic=arguments.magic,
        )
    except Exception as e:
        print('Could not open archive file {0} for reading: {1}'.format(archive_path, e), file=sys.stderr)
        sys.exit(1)

    if arguments.info:
        has_filter = bool(arguments.only or arguments.exclude)
        info = archive.get_info(only_spec=arguments.only, exclude_spec=arguments.exclude)
        print(format_archive_info(info, has_filter=has_filter))

    elif arguments.create or arguments.append:
        file_pairs = collect_input_files(arguments.files)
        if explicit_version is not None or arguments.create:
            archive.version = target_version
        if arguments.magic is not None:
            archive.custom_magic = normalize_magic(arguments.magic)
        if force_key is not None:
            archive.key = force_key

        if dry_run:
            action_label = 'create' if arguments.create else 'append'
            total_bytes = 0
            valid_count = 0
            for outfile, real_filename in file_pairs:
                norm_out = archive.convert_filename(_unicode(outfile))
                if not os.path.exists(real_filename):
                    print('[dry-run] Would skip missing file {0}'.format(real_filename), file=sys.stderr)
                    continue
                if archive.has_file(norm_out):
                    print('[dry-run] Would fail adding duplicate {0}'.format(norm_out), file=sys.stderr)
                    continue
                size = os.path.getsize(real_filename)
                total_bytes += size
                valid_count += 1
                print('[dry-run] Would pack {0} -> {1} ({2})'.format(
                    real_filename, norm_out, format_size(size)))
            print('[dry-run] Summary: would {0} {1} (RPAv{2}, magic={3!r}) with {4} new file(s) ({5}).'.format(
                action_label,
                output,
                archive.version,
                archive._get_write_magic().rstrip(' ') or 'RPAv1',
                valid_count,
                format_size_long(total_bytes),
            ))
            return

        pack_progress = ProgressReporter(
            total=len(file_pairs),
            action='Pack',
            enabled=arguments.progress,
            verbose=arguments.verbose,
        )
        for outfile, real_filename in file_pairs:
            try:
                with open(real_filename, 'rb') as file:
                    data = file.read()
                archive.add(outfile, data)
                pack_progress.update(outfile, len(data))
            except Exception as e:
                pack_progress.update(outfile, 0)
                print('Could not add file {0} to archive: {1}'.format(real_filename, e), file=sys.stderr)
        pack_progress.finish()

        try:
            archive.save(output, progress=arguments.progress)
        except Exception as e:
            print('Could not save archive file: {0}'.format(e), file=sys.stderr)
            sys.exit(1)

    elif arguments.delete:
        if explicit_version is not None:
            archive.version = explicit_version
        if arguments.magic is not None:
            archive.custom_magic = normalize_magic(arguments.magic)
        if force_key is not None:
            archive.key = force_key

        if dry_run:
            removed_count = 0
            freed_bytes = 0
            for filename in arguments.files:
                norm_name = archive.convert_filename(_unicode(filename))
                if archive.has_file(norm_name):
                    size = archive.get_file_size(norm_name)
                    removed_count += 1
                    freed_bytes += size
                    print('[dry-run] Would delete {0} ({1})'.format(norm_name, format_size(size)))
                else:
                    print('[dry-run] Would fail deleting missing file {0}'.format(norm_name), file=sys.stderr)
            print('[dry-run] Summary: would delete {0} file(s) ({1}) and write {2} (RPAv{3}).'.format(
                removed_count,
                format_size_long(freed_bytes),
                output,
                archive.version,
            ))
            return

        del_progress = ProgressReporter(
            total=len(arguments.files),
            action='Del',
            enabled=arguments.progress,
            verbose=arguments.verbose,
        )
        for filename in arguments.files:
            try:
                archive.remove(filename)
            except Exception as e:
                print('Could not delete file {0} from archive: {1}'.format(filename, e), file=sys.stderr)
            finally:
                del_progress.update(filename, 0)
        del_progress.finish()

        try:
            archive.save(output, progress=arguments.progress)
        except Exception as e:
            print('Could not save archive file: {0}'.format(e), file=sys.stderr)
            sys.exit(1)

    elif arguments.extract:
        if len(arguments.files) > 0:
            files = arguments.files
        else:
            files = archive.list()

        files = filter_files(files, only_spec=arguments.only, exclude_spec=arguments.exclude)

        tasks = []
        target_dirs = set()
        if output:
            target_dirs.add(os.path.abspath(output))

        for filename in files:
            if '=' in filename and not archive.has_file(filename):
                (outfile, archive_filename) = filename.split('=', 1)
            else:
                outfile = filename
                archive_filename = filename

            try:
                target_path = sanitize_extract_path(output, outfile)
                out_dir = os.path.dirname(target_path)
                if out_dir:
                    target_dirs.add(out_dir)
                tasks.append((archive_filename, target_path))
            except Exception as e:
                print('Could not extract file {0} from archive: {1}'.format(archive_filename, e), file=sys.stderr)

        if dry_run:
            total_bytes = 0
            valid_count = 0
            for archive_filename, target_path in tasks:
                if archive.has_file(archive_filename):
                    size = archive.get_file_size(archive_filename)
                    total_bytes += size
                    valid_count += 1
                    print('[dry-run] Would extract {0} -> {1} ({2})'.format(
                        archive_filename, target_path, format_size(size)))
                else:
                    print('[dry-run] Would fail extracting missing file {0}'.format(archive_filename), file=sys.stderr)
            print('[dry-run] Summary: would extract {0} file(s) ({1}) into {2}.'.format(
                valid_count,
                format_size_long(total_bytes),
                os.path.abspath(output if output else '.'),
            ))
            return

        for d in sorted(target_dirs):
            if not os.path.exists(d):
                os.makedirs(d, exist_ok=True)

        ext_progress = ProgressReporter(
            total=len(tasks),
            action='Ext',
            enabled=arguments.progress,
            verbose=arguments.verbose,
        )

        if workers <= 1 or len(tasks) <= 1:
            for archive_filename, target_path in tasks:
                try:
                    contents = archive.read(archive_filename)
                    with open(target_path, 'wb') as file:
                        file.write(contents)
                    ext_progress.update(archive_filename, len(contents))
                except Exception as e:
                    ext_progress.update(archive_filename, 0)
                    print('Could not extract file {0} from archive: {1}'.format(archive_filename, e), file=sys.stderr)
        else:
            thread_local = threading.local()
            opened_handles = []
            handles_lock = threading.Lock()
            data_path = archive.data_file or archive.file

            def _get_thread_handle():
                if not hasattr(thread_local, 'handle'):
                    if data_path and os.path.exists(data_path):
                        h = open(data_path, 'rb')
                        thread_local.handle = h
                        with handles_lock:
                            opened_handles.append(h)
                    else:
                        thread_local.handle = None
                return thread_local.handle

            def _extract_worker(task):
                archive_filename, target_path = task
                h = _get_thread_handle()
                contents = archive.read(archive_filename, handle=h)
                with open(target_path, 'wb') as out_f:
                    out_f.write(contents)
                return archive_filename, len(contents)

            try:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    future_map = {executor.submit(_extract_worker, t): t for t in tasks}
                    for future in as_completed(future_map):
                        archive_filename, _ = future_map[future]
                        try:
                            _, written = future.result()
                            ext_progress.update(archive_filename, written)
                        except Exception as e:
                            ext_progress.update(archive_filename, 0)
                            print('Could not extract file {0} from archive: {1}'.format(archive_filename, e), file=sys.stderr)
            finally:
                for h in opened_handles:
                    try:
                        h.close()
                    except Exception:
                        pass

        ext_progress.finish()

    elif arguments.list:
        files = archive.list()
        files.sort()
        files = filter_files(files, only_spec=arguments.only, exclude_spec=arguments.exclude)
        for file in files:
            print(file)
    else:
        print('No operation given :(')
        print('Use {0} --help for usage details.'.format(sys.argv[0]))


if __name__ == '__main__':
    main()
