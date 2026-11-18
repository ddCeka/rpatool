rpatool
=======

This is a simple tool allowing you to inspect, create, modify, and extract Ren'Py Archive (`.rpa`/`.rpi`) files.
Supports full reading and writing across **RPAv1**, **RPAv2**, **RPAv3**, **RPAv3.2**, and **RPAv4**, built-in recognition for common modded headers (`ALT-1.0 `, `ZiX-12A `, `EX2-1.0 `, etc.), wildcard/exclude pattern filtering, dry-run simulation, compact 60-column progress reporting, and multi-threaded extraction.

Usage
-----
    rpatool [-l|-i|-x|-c|-d|-a] [-o OUTFILE] [--only PATTERNS] [--exclude PATTERNS]
            [-1|-2|-3|-32|-4] [--magic HEADER] [--force-version VER] [--offset HEX]
            [-k KEY] [-p COUNT] [-j JOBS] [--progress] [--dry-run] [-h] [-v] [-V]
            ARCHIVE [FILE [FILE ...]]

    positional arguments:
      ARCHIVE               The Ren'Py archive file to operate on
      FILE                  Zero or more files to operate on

    actions:
      -l, --list            List files in archive ARCHIVE
      -i, --info            Display archive metadata (version, magic header, XOR key, file count, size)
      -x, --extract         Extract FILEs from ARCHIVE
      -c, --create          Create ARCHIVE from FILEs
      -d, --delete          Delete FILEs from ARCHIVE
      -a, --append          Append FILEs to ARCHIVE

    optional arguments:
      -o OUTFILE, --outfile OUTFILE
                            An alternative output archive file when appending to or
                            deleting from archives, or output directory when extracting
      --only PATTERNS       Include only matching extensions or wildcards, comma-separated
                            (e.g., .png,.webp or images/*)
      --exclude PATTERNS    Exclude matching extensions or wildcards, comma-separated
                            (e.g., .rpyc,.txt or cache/*)
      -1, --one             Use the RPAv1 (.rpi + .rpa) format for creating/saving archives
      -2, --two             Use the RPAv2 format for creating/saving archives
      -3, --three           Use the RPAv3 format for creating/saving archives (default)
      -32, --three-two      Use the RPAv3.2 format for creating/saving archives
      -4, --four            Use the RPAv4 format for creating/saving archives
      --magic HEADER        Custom magic header prefix for modded archives (e.g., "ALT-1.0 ")
      --force-version VER   Override detected archive version (1, 2, 3, 3.2, or 4) for custom headers
      --offset HEX          Override index offset in hexadecimal for non-standard headers
      -k KEY, --key KEY     The obfuscation key used for RPAv3/v3.2/v4 archives, in hexadecimal
                            (default: 0xDEADBEEF)
      -p COUNT, --padding COUNT
                            The maximum number of bytes of padding to add between files (default: 0)
      -j JOBS, --jobs JOBS  Number of parallel worker threads for extraction (default: 1, 0 = auto)
      --progress            Display a compact 60-column progress bar during operations
      --dry-run             Simulate extract, create, append, or delete without modifying disk
      -h, --help            Print this help and exit
      -v, --verbose         Be a bit more verbose while performing operations
      -V, --version         Show version information

    The FILE argument can optionally be in ARCHIVE=REAL format, mapping a file in
    the archive file system to a file on your real file system. An example of
    this is: rpatool -x test.rpa script.rpyc=/home/foo/test.rpyc

Examples
--------
    rpatool -i foo.rpa
Inspects `foo.rpa` and prints its format version, magic header, XOR key, index offset, file count, total size, and top file types.

    rpatool -x foo.rpa
Extracts every file from `foo.rpa` into the current directory (`.`), sanitizing internal paths and creating subdirectories as needed.

    rpatool -j 4 --progress -o output -x foo.rpa
Extracts all files from `foo.rpa` into `output` using 4 parallel worker threads with a compact 60-column progress bar.

    rpatool --dry-run -o output -x foo.rpa --only .png,.webp --exclude "cache/*"
Simulates extracting `.png` and `.webp` files while skipping `cache/*`, printing all planned actions and sizes without writing anything to disk.

    rpatool -o output -x foo.rpa script.rpyc ui.png
Extracts the files `script.rpyc` and `ui.png` from `foo.rpa` into the directory `output`.

    rpatool -c bar.rpa test.jpg script.rpy sprites
Creates the RPAv3 archive `bar.rpa`, containing `test.jpg`, `script.rpy`, and the directory `sprites`.

    rpatool -4 -p 25 -k 12345 -c bar.rpa movies=C:\projects\vn\movies
Creates an RPAv4 archive `bar.rpa` with the obfuscation key `0x12345` and maximum padding of `25`, mapping `C:\projects\vn\movies` into `movies` inside the archive.

    rpatool -1 -c legacy.rpi script.rpy images
Creates an RPAv1 archive pair (`legacy.rpi` index + `legacy.rpa` data file).

    rpatool --magic "MYMOD-1.0 " -o out_dir -x modded.rpa
Unpacks a modded archive that uses a non-standard header prefix (`MYMOD-1.0 `). Built-in headers such as `ALT-1.0 `, `ZiX-12A `, `ZiX-12B `, `EX2-1.0 `, and `RPA-5.0 ` are detected automatically without `--magic`.

    rpatool -l baz.rpa --only "images/*" --exclude "*.bak"
Lists matching files in `baz.rpa`.

    rpatool -v -a foo.rpa sprites=sprites_new
Adds all files from `sprites_new` into `sprites` in `foo.rpa`, preserving the existing archive format version and magic header.

    rpatool -o bar_new.rpa -d bar.rpa foo.jpg
Removes `foo.jpg` from `bar.rpa`, storing the resulting archive in `bar_new.rpa`.

Running Tests
-------------
    python -m unittest -v test_rpatool.py

API
---
`rpatool` can also be included in any other project (following the license conditions, of course) to provide the `RenPyArchive` class.
A small overview:

    RenPyArchive([file = None], [version = 3], [padlength = 0], [key = 0xDEADBEEF], [verbose = False],
                 [force_version = None], [force_offset = None], [force_key = None], [custom_magic = None])
The constructor, which will optionally load an archive file.

`file`: the archive file to open. If `None`, no archive will be attempted to open.

`version`: the archive format version (`1`, `2`, `3`, `3.2`, or `4`) used when `RenPyArchive.save([file])` is called. Default: `3`

`padlength`: the maximum number of bytes of padding to put between files when saving. Default: `0`

`key`: the obfuscation key used when saving RPAv3, RPAv3.2, or RPAv4 archives. Default: `0xDEADBEEF`

`verbose`: print info on what we are doing to the command line. Default: `False`

`force_version`: override detected version (`1`, `2`, `3`, `3.2`, or `4`) when loading custom headers. Default: `None`

`force_offset`: override index offset in bytes when loading non-standard headers. Default: `None`

`force_key`: override XOR obfuscation key when loading custom headers. Default: `None`

`custom_magic`: custom magic header prefix (e.g., `"ALT-1.0 "`) for reading or writing modded archives. Default: `None`

    RenPyArchive.load(filename)
Loads an archive file from `filename`. Will raise an `IOError` if the file can't be accessed, or a `ValueError` if the file is not detected as a Ren'Py archive.

    RenPyArchive.save([filename])
Saves the archive to `filename` (or overwrites the loaded archive if `filename` is omitted). Supports RPAv1 (`.rpi` + `.rpa`), RPAv2, RPAv3, RPAv3.2, and RPAv4.

    RenPyArchive.get_info([files = None])
Returns a dictionary containing archive metadata (`version`, `magic`, `key`, `offset`, `archive_size`, `total_files`, `total_uncompressed_size`, `matched_files`, `matched_uncompressed_size`, and `extensions`).

    RenPyArchive.list()
Gives a list of all filenames currently in the archive.

    RenPyArchive.has_file(filename)
Returns `True` if `filename` is found in the archive, `False` otherwise.

    RenPyArchive.add(filename, content)
Adds a file to the archive with path `filename` and bytes `content`. Will raise a `ValueError` if the filename already exists in the archive.

    RenPyArchive.change(filename, content)
Changes the contents of an existing file in the archive. Will raise an `IOError` if the file isn't known in the archive.

    RenPyArchive.remove(filename)
Removes `filename` from the archive. Will raise an `IOError` if the filename isn't known in the archive.

    RenPyArchive.read(filename, [handle = None])
Reads and returns the bytes content of `filename` in the archive. Accepts an optional independent file `handle` for thread-safe parallel reads.

License
-------
rpatool is licensed under the WTFPL. See the LICENSE file for more details.

Disclaimer
----------
This tool is intended for use with files on which the authors allowed modification of and/or extraction from ONLY and the unpermitted use on files where such consent was not given is highly discouraged, and most likely a license violation as well.
Support requests for help with dealing with such files will not be answered.

Credits
-------
Credits for the creation of the Ren'Py archive format and the reference code in Ren'Py go to renpytom.
