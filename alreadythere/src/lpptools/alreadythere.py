#!/usr/bin/env python3

import hashlib
import importlib.metadata
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import List, Tuple


def _version_str() -> str:
    try:
        return importlib.metadata.version("alreadythere")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


__version__ = _version_str()

# ----


def welcome():
    print("alreadythere v%s" % __version__)


def usage():
    print("""\nUSAGE:
  alreadythere [OPTION] [OPTION] ... [INPUT-FOLDER] [INPUT-FOLDER] ... SEARCH-FOLDER

OPTIONS:
  -h                  Show usage (this help)
  --version           Print version and exit
  -v -V               Be verbose, more Verbose
  -j THREADS          Multithreaded hashing (this may not be an advantage if IO limited)
  --i-rec-off         Don't traverse input folder(s) recursively
  --s-rec-off         Don't traverse search folder(s) recursively
  --fullhash          Calculate MD5 hash of entire file. Otherwise a fast hash method is
                      used that only hashes the first ~5 MB, but includes file size.
  --trust-cache       Don't traverse SEARCH-FOLDER, expect and trust the cache file there.
  --delete            Careful! Deletes files in input folder(s) that exist in search folder.

Checks if files in INPUT-FOLDER(s) are present in SEARCH_FOLDER. The folders are traversed
recursively if not instructed otherwise.

Identical files are identified in the process. Specify -V option to see these files.

If no INPUT-FOLDER is specified, the script will just search for identical files
in the SEARCH-FOLDER.
""")


# --- Verbose print levels

Verbosity = 0
Failed = False


def eprint(s: str):  # error print
    print(s, file=sys.stderr)
    global Failed
    Failed = True


def iprint(s: str):  # info print
    if Verbosity >= 0:
        print(s)


def vprint(s: str):
    if Verbosity >= 1:
        print(s)


def vvprint(s: str):
    if Verbosity >= 2:
        print(s)


# --- Utility functions

def getFilesInDir(dir: str, recursive: bool) -> List[str]:
    files = []
    for root, dirnames, filenames in os.walk(dir):
        for filename in filenames:
            if not filename.startswith("_already_there_"):
                files.append(os.path.abspath(os.path.join(root, filename)))
        if not recursive:
            break
    return files


def calcFileMd5Hash(filename, blocks_limit: int = -1):
    hash_obj = hashlib.md5()
    with open(filename, "rb") as f:
        while blocks_limit != 0:
            chunk = f.read(524288)
            if not chunk:
                break
            hash_obj.update(chunk)
            blocks_limit -= 1
    return hash_obj.hexdigest()

# ----

def hashFilesMt(dir, recursive, fast_hash, hash_cache, threads=4) -> Tuple[dict, int, dict]:
    hash_lock = Lock()
    hash_dict = {}
    hash_dups = 0
    new_cache = {}

    def calc(filename):
        if filename in hash_cache:
            hash = hash_cache[filename]
        else:
            if fast_hash:
                file_size = os.path.getsize(filename)
                hash = calcFileMd5Hash(filename, 10) + ":%d" % file_size
            else:
                hash = calcFileMd5Hash(filename)
        with hash_lock:
            new_cache[filename] = hash
            if hash in hash_dict:
                hash_dict[hash].append(filename)
                hash_dups += 1
            else:
                hash_dict[hash] = [filename]

    with ThreadPoolExecutor(threads) as executor:
        executor.map(calc, getFilesInDir(dir, recursive))

    return (hash_dict, hash_dups, new_cache)


def hashFiles(dir, recursive, fast_hash, hash_cache) -> Tuple[dict, int, dict]:
    hash_dict = {}
    hash_dups = 0
    new_cache = {}

    for filename in getFilesInDir(dir, recursive):
        if filename in hash_cache:
            hash = hash_cache[filename]
        else:
            if fast_hash:
                file_size = os.path.getsize(filename)
                hash = calcFileMd5Hash(filename, 10) + ":%d" % file_size
            else:
                hash = calcFileMd5Hash(filename)

        new_cache[filename] = hash

        if hash in hash_dict:
            hash_dict[hash].append(filename)
            hash_dups += 1
        else:
            hash_dict[hash] = [filename]

    return (hash_dict, hash_dups, new_cache)


def justTrustCache(hash_cache: dict[str, str]) -> Tuple[dict, int]:
    hash_dict = {}
    hash_dups = 0

    for filename, hash in hash_cache.items():
        if hash in hash_dict:
            hash_dict[hash].append(filename)
            hash_dups += 1
        else:
            hash_dict[hash] = [filename]

    return (hash_dict, hash_dups)


def formatFilenames(filenames, start_path: str = None) -> str:
    if start_path is not None:
        filenames = [os.path.relpath(x, start_path) for x in filenames]
    if len(filenames) > 1:
        return "\n  " + "\n  ".join(filenames)
    else:
        return filenames[0]


def loadCache(folder: str, fast_hash: bool) -> dict:
    file = os.path.join(
        folder,
        "_already_there_fast_cache.txt" if fast_hash else "_already_there_cache.txt",
    )
    ret = {}
    if os.path.exists(file):
        with open(file) as f:
            for l in f.readlines():
                s = l.strip("\n").split(" ", 1)
                if len(s) == 2:
                    filename = os.path.abspath(os.path.join(folder, s[1]))
                    ret[filename] = s[0]
        vprint("Loaded hash cache: %s with %d entries" % (file, len(ret)))
    return ret


def saveCache(folder: str, fast_hash: bool, cache: dict):
    file = os.path.join(
        folder,
        "_already_there_fast_cache.txt" if fast_hash else "_already_there_cache.txt",
    )
    with open(file, "w") as f:
        for filename, hash in cache.items():
            f.write("%s %s\n" % (hash, os.path.relpath(filename, folder)))
    vprint("Saved hash cache: %s with %d entries" % (file, len(cache)))


def alreadyThere(
    i_rec, s_rec, i_folders, s_folder, fast_hash, delete, trust_cache, thread_count
):
    if not os.path.isdir(s_folder):
        eprint("%s is not a valid path, aborting" % s_folder)
        return

    for folder in i_folders:
        if not os.path.isdir(folder):
            eprint("%s is not a valid path, aborting" % folder)
            return

    hash_type = "fast" if fast_hash else "full"
    vprint("Processing search folder (%s hash): %s" % (hash_type, s_folder))

    hash_cache = loadCache(s_folder, fast_hash)

    if trust_cache:
        if len(hash_cache) == 0:
            eprint("No cache file found and expected to trust the cache, aborting")
            return
        (search_hashes, search_dups) = justTrustCache(hash_cache)
    else:
        if thread_count > 1:
            (search_hashes, search_dups, new_cache) = hashFilesMt(
                s_folder, s_rec, fast_hash, hash_cache, thread_count
            )
        else:
            (search_hashes, search_dups, new_cache) = hashFiles(
                s_folder, s_rec, fast_hash, hash_cache
            )

        saveCache(s_folder, fast_hash, new_cache)

    for hash, filenames in search_hashes.items():
        if len(filenames) > 1:
            vvprint("Identical files: " + formatFilenames(filenames))

    iprint(
        "Search folder: %s has %d files and %d duplicate files"
        % (s_folder, len(search_hashes), search_dups)
    )

    for folder in i_folders:
        vvprint("\nProcessing input folder (%s hash): %s" % (hash_type, folder))
        if thread_count > 1:
            (input_hashes, dups, new_cache) = hashFilesMt(
                folder, i_rec, fast_hash, {}, thread_count
            )
        else:
            (input_hashes, dups, new_cache) = hashFiles(folder, i_rec, fast_hash, {})
        parent_folder = os.path.dirname(folder.rstrip(os.path.sep))
        no_found = 0
        deleted = 0
        for hash, filenames in sorted(input_hashes.items(), key=lambda x: x[1]):
            match = search_hashes.get(hash)
            if match is None:
                vprint("No match found: " + formatFilenames(filenames, parent_folder))
                no_found += 1
            else:
                if len(filenames) > 1:
                    vvprint(
                        "Identical files: "
                        + formatFilenames(filenames, parent_folder)
                        + "\nmatches: "
                        + formatFilenames(match)
                    )
                else:
                    vvprint(
                        "File: "
                        + formatFilenames(filenames, parent_folder)
                        + "\nmatches: "
                        + formatFilenames(match)
                    )
                if delete:
                    for filename in filenames:
                        iprint("File has duplicate(s), deleting: %s" % filename)
                        os.unlink(filename)
                        deleted += 1

        concl = "Input folder: %s has %d files, %d duplicate files" % (
            folder,
            len(input_hashes),
            dups,
        )

        if len(input_hashes) == 0:
            concl += ". No files in input folder"
        elif no_found == 0:
            concl += ". ALL FILES FOUND in search folder"
        elif no_found == len(input_hashes):
            concl += ". NONE of the files were found in search folder"
        else:
            concl += ". SOME FILES found in search folder. Not found: %d" % no_found

        if deleted > 0:
            concl += ". Files deleted: %d" % deleted

        iprint(concl)


# ----

class ShowHelpException(Exception):
    def __init__(self, param: str):
        self.param = param


class Params:
    NonOptionsMin = 1

    def __init__(self):
        self.Flags = {
            "-v": False,
            "-V": False,
            "--i-rec-off": False,
            "--s-rec-off": False,
            "--fullhash": False,
            "--delete": False,
            "--trust-cache": False,
        }
        self.Ints = {"-j": 1}
        self.NonOptions = []

    @staticmethod
    def parse(cmds: list):
        p = Params()
        while len(cmds) > 0 and cmds[0].startswith("-"):
            if cmds[0] == "-h":
                raise ShowHelpException(cmds[0])
            elif cmds[0] in p.Flags:
                p.Flags[cmds[0]] = True
            elif cmds[0] in p.Ints:
                p.Ints[cmds[0]] = int(cmds[1])
                cmds.pop(0)
            else:
                raise ValueError("Unknown option: " + cmds[0])
            cmds.pop(0)
        if len(cmds) < Params.NonOptionsMin:
            raise ValueError("Wrong number of arguments")
        p.NonOptions = list(cmds)
        return p


def processParams(p: Params):
    global Verbosity
    if p.Flags["-v"]:
        Verbosity = 1
    if p.Flags["-V"]:
        Verbosity = 2

    alreadyThere(
        not p.Flags["--i-rec-off"],
        not p.Flags["--s-rec-off"],
        p.NonOptions[0:-1],
        p.NonOptions[-1],
        not p.Flags["--fullhash"],
        p.Flags["--delete"],
        p.Flags["--trust-cache"],
        p.Ints["-j"],
    )


def main() -> int:
    welcome()
    if "--version" in sys.argv:
        return 0
    try:
        params = Params.parse(sys.argv[1:])
    except ShowHelpException as e:
        usage()
        return 0
    except Exception as e:
        print(str(e))
        usage()
        return 1
    else:
        processParams(params)
        return 0 if not Failed else 1


if __name__ == "__main__":
    sys.exit(main())
