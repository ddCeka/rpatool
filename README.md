rpatool
=======

This is a simple tool allowing you to inspect, create, modify, and extract Ren'Py Archive (`.rpa`/`.rpi`) files.
Supports reading and writing **RPAv1** (`.rpi` + `.rpa`), **RPAv2**, **RPAv3**, **RPAv3.2**, and **RPAv4** archives, as well as built-in detection and custom overrides for modded magic headers.

Features
--------
* **Full Archive Version Support:** Read and write RPAv1 (`-1`), RPAv2 (`-2`), RPAv3 (`-3`), RPAv3.2 (`-32`), and RPAv4 (`-4`) archives.
* **Modded Archive Support:** Built-in detection for common modded headers (`ALT-1.0 `, `ZiX-12A `, `ZiX-12B `, `EX2-1.0 `, `RPA-2.1 `, `RPA-3.1 `, `RPA-4.1 `, `RPA-5.0 `) plus `--magic`, `--force-version`, `--offset`, and `-k` overrides.
* **Archive Inspection (`-i` / `--info`):** View format version, magic header, XOR key, index offset, file counts, total uncompressed payload size, and top file extensions.
* **Flexible Filtering (`--only` / `--exclude`):** Filter listed, inspected, or extracted files using comma-separated extensions (`.png,.webp`) or glob wildcards (`images/*`).
* **Parallel Extraction (`-j` / `--jobs`):** Unpack large archives faster using multi-threaded workers alongside safe path-traversal sanitization.
* **Compact Progress Bar (`--progress`):** Optional 60-column live progress output during packing, extraction, and deletion.

Usage
-----
    rpatool [-l|-i|-x|-c|-d|-a] [-o OUTFILE] [-1|-2|-3|-32|-4]
            [--only PATTERNS] [--exclude PATTERNS] [--magic HEADER]
            [--force-version VER] [--offset HEX] [-k KEY] [-p COUNT]
            [-j N] [--progress] [-h] [-v] [-V]
            ARCHIVE [FILE [FILE ...]]

    positional arguments:
      ARCHIVE               The Ren'Py archive file to operate on
      FILE                  Zero or more files to operate on

    actions:
      -l, --list            List files in archive ARCHIVE
      -i, --info            Display archive metadata, format version, key, and size summary
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
      --force-version VER   Override detected archive version (1, 2, 3, 3.2, or 4)
      --offset HEX          Override index offset in hexadecimal for non-standard headers
      -k KEY, --key KEY     The obfuscation key used for RPAv3/v3.2/v4 archives, in hex
                            (default: 0xDEADBEEF)
      -p COUNT, --padding COUNT
                            The maximum number of bytes of padding to add between
                            files (default: 0)
      -j N, --jobs N        Number of parallel worker threads for extraction
                            (default: 1, use 0 for auto)
      --progress            Display compact 60-column progress output
      -h, --help            Print this help and exit
      -v, --verbose         Be a bit more verbose while performing operations
      -V, --version         Show version information

    The FILE argument can optionally be in ARCHIVE=REAL format, mapping a file in
    the archive file system to a file on your real file system. An example of
    this is: rpatool -x test.rpa script.rpyc=/home/foo/test.rpyc

Examples
--------
    rpatool -i foo.rpa
Will display metadata about `foo.rpa`, including its format version, magic header, XOR key, file count, and uncompressed size.

    rpatool -x foo.rpa
Will extract every file from `foo.rpa` into the current directory, making subdirectories when necessary and sanitizing paths against directory traversal.

    rpatool -j 4 --progress -o output -x foo.rpa
Will extract all files from `foo.rpa` into `output` using 4 parallel worker threads and a compact 60-column progress bar.

    rpatool -o output -x foo.rpa --only ".png,.webp" --exclude "cache/*"
Will extract only `.png` and `.webp` files from `foo.rpa` into `output` while skipping anything under `cache/`.

    rpatool -c bar.rpa test.jpg script.rpy sprites
Will create the RPAv3 archive `bar.rpa`, containing the files `test.jpg`, `script.rpy`, and the directory `sprites`.

    rpatool -4 -p 25 -k 12345 -c bar.rpa movies=C:\projects\vn\movies
Will create an RPAv4 archive `bar.rpa` with the obfuscation key `0x12345` and maximum padding of `25` bytes, placing files from `C:\projects\vn\movies` into the archive folder `movies`.

    rpatool --magic "MYGAME-1.0 " -c custom.rpa game_files
Will create an archive using a custom magic header prefix `MYGAME-1.0 `.

    rpatool -l baz.rpa
Will list all files in the archive `baz.rpa`.

    rpatool -v -a foo.rpa sprites=sprites_new
Will add all files from the directory `sprites_new` to the directory `sprites` in the archive, printing detailed progress messages.

    rpatool -o bar_new.rpa -d bar.rpa foo.jpg
Will remove the file `foo.jpg` from the archive `bar.rpa`, storing the result archive in `bar_new.rpa`.

Testing
-------
Run the built-in unit test suite with:

    python3 -m unittest test_rpatool.py

API
---
`rpatool` can also be included in any other project (following the license conditions, of course) to provide the `RenPyArchive` class.
A small overview:

    RenPyArchive([file = None], [version = 3], [padlength = 0], [key = 0xDEADBEEF], [verbose = False],
                 [force_version = None], [force_offset = None], [force_key = None], [custom_magic = None])
The constructor, which will optionally load an archive file.

`file`: the archive file to open. If None, no archive will be attempted to open.

`version`: the archive format version used to save the archive when `RenPyArchive.save([file])` is called (`1`, `2`, `3`, `3.2`, or `4`). Default: `3`

`padlength`: the maximum number of bytes of padding to put between files when saving. Default: `0`

`key`: the obfuscation key used when saving RPAv3/v3.2/v4 archives. Default: `0xDEADBEEF`

`verbose`: print info on what we are doing to the command line. Default: `False`

`force_version`: override auto-detected archive version (`1`, `2`, `3`, `3.2`, or `4`). Default: `None`

`force_offset`: override index offset in bytes when reading non-standard headers. Default: `None`

`force_key`: override XOR obfuscation key when reading custom headers. Default: `None`

`custom_magic`: custom magic header prefix for reading or writing modded archives. Default: `None`

    RenPyArchive.load(filename)
Loads an archive file from `filename`. Will raise an `IOError` if the file can't be accessed, or a `ValueError` if the file is not detected as a Ren'Py archive.

    RenPyArchive.save([filename])
Save the archive to `filename`. Will raise `ValueError` if the filename isn't given with `filename`, nor previously defined, or an `IOError` if it couldn't save the file.

    RenPyArchive.get_info([files = None])
Return a dictionary containing archive metadata (`version`, `magic`, `key`, `offset`, `archive_size`, `total_files`, `total_uncompressed`, `selected_files`, `selected_uncompressed`, and `extensions`).

    RenPyArchive.list()
Give a list of all filenames currently in the archive.

    RenPyArchive.has_file(filename)
Returns True if `filename` is found in the archive, False otherwise.

    RenPyArchive.add(filename, content)
Add a file to the archive with file `filename` and contents `content`. Will raise a `ValueError` if the filename already exists in the archive.

    RenPyArchive.change(filename, content)
Change the contents of a current file in the archive. Will raise an `IOError` if the file isn't known in the archive.

    RenPyArchive.remove(filename)
Remove `filename` from the archive. Will raise an `IOError` if the filename isn't known in the archive.

    RenPyArchive.read(filename, [handle = None])
Read and return the content of file `filename` in the archive. Optional `handle` allows thread-safe concurrent reads. Will raise an `IOError` if the filename isn't known in the archive.

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
