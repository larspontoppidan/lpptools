"""Auto-tag FLAC and MP3 music files based on folder/filename conventions.

Folder name format:  "<Artist> - <Album>"
File name format:    "<TrackNumber> - <TrackTitle>.<ext>"
                or:  "<TrackNumber> <TrackTitle>.<ext>"

Embeds folder.jpg (if present) as cover art into every file.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from pathlib import Path

from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3,
    ID3NoHeaderError,
    APIC,
    TIT2,
    TPE1,
    TPE2,
    TALB,
    TRCK,
    TDRC,
    TCON,
    TPOS,
    COMM,
    TCOM,
    TPE3,
    TIT1,
)
from mutagen.mp3 import MP3


def _version_str() -> str:
    try:
        return importlib.metadata.version("autotag")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


__version__ = _version_str()


COVER_FILENAME = "folder.jpg"
FILENAME_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*)?(.+?)\s*$")
AUDIO_EXTS = {".flac", ".mp3"}


def parse_folder(folder: Path) -> tuple[str, str]:
    name = folder.name
    if " - " not in name:
        raise ValueError(f"Folder name {name!r} does not contain ' - '")
    artist, album = name.split(" - ", 1)
    return artist.strip(), album.strip()


def parse_filename(stem: str) -> tuple[str, str]:
    m = FILENAME_RE.match(stem)
    if not m:
        raise ValueError(f"Filename stem {stem!r} does not match '<num> [-] <title>'")
    return m.group(1).lstrip("0") or "0", m.group(2).strip()


def load_cover_bytes(folder: Path) -> bytes | None:
    cover_path = folder / COVER_FILENAME
    if not cover_path.is_file():
        return None
    return cover_path.read_bytes()


def make_flac_picture(data: bytes) -> Picture:
    pic = Picture()
    pic.data = data
    pic.type = 3  # front cover
    pic.mime = "image/jpeg"
    pic.desc = "Cover"
    return pic


def write_flac(path: Path, fields: dict, cover: bytes | None,
               clean: bool = False) -> None:
    audio = FLAC(path)
    if clean:
        audio.delete()
        audio.clear_pictures()
    mapping = {
        "artist": "artist",
        "albumartist": "albumartist",
        "album": "album",
        "title": "title",
        "tracknumber": "tracknumber",
        "discnumber": "discnumber",
        "date": "date",
        "genre": "genre",
        "comment": "comment",
        "composer": "composer",
        "conductor": "conductor",
        "work": "work",
    }
    for key, tag in mapping.items():
        val = fields.get(key)
        if val is not None:
            audio[tag] = val
    if cover is not None:
        audio.clear_pictures()
        audio.add_picture(make_flac_picture(cover))
    audio.save()


def write_mp3(path: Path, fields: dict, cover: bytes | None,
              clean: bool = False) -> None:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    if clean:
        tags.delete()
        tags = ID3()

    setters = {
        "title": lambda v: TIT2(encoding=3, text=v),
        "artist": lambda v: TPE1(encoding=3, text=v),
        "albumartist": lambda v: TPE2(encoding=3, text=v),
        "album": lambda v: TALB(encoding=3, text=v),
        "tracknumber": lambda v: TRCK(encoding=3, text=v),
        "discnumber": lambda v: TPOS(encoding=3, text=v),
        "date": lambda v: TDRC(encoding=3, text=v),
        "genre": lambda v: TCON(encoding=3, text=v),
        "comment": lambda v: COMM(encoding=3, lang="eng", desc="", text=v),
        "composer": lambda v: TCOM(encoding=3, text=v),
        "conductor": lambda v: TPE3(encoding=3, text=v),
        "work": lambda v: TIT1(encoding=3, text=v),
    }
    for key, make in setters.items():
        val = fields.get(key)
        if val is not None:
            tags.add(make(val))

    if cover is not None:
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))

    tags.save(path, v2_version=3)


def show_tags(path: Path) -> None:
    print(f"\n=== {path.name} ===")
    ext = path.suffix.lower()
    try:
        if ext == ".flac":
            audio = FLAC(path)
            for k, v in audio.tags or []:
                print(f"  {k}: {v}")
            print(f"  [pictures: {len(audio.pictures)}]")
        elif ext == ".mp3":
            audio = MP3(path)
            if audio.tags is None:
                print("  (no ID3 tags)")
                return
            for frame in audio.tags.values():
                print(f"  {frame.FrameID}: {frame}")
        else:
            print("  (unsupported)")
    except MutagenError as e:
        print(f"  error: {e}", file=sys.stderr)


def collect_audio(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def tag_folder(folder: Path, args: argparse.Namespace) -> int:
    artist, album = parse_folder(folder)
    if args.artist:
        artist = args.artist
    if args.album:
        album = args.album

    cover = None if args.no_cover else load_cover_bytes(folder)
    if cover is None and not args.no_cover:
        print(f"warning: no {COVER_FILENAME} found in {folder}", file=sys.stderr)

    files = collect_audio(folder)
    if not files:
        print(f"warning: no audio files in {folder}", file=sys.stderr)
        return 0

    total_tracks = len(files)
    tagged = 0
    for path in files:
        try:
            track_num, track_title = parse_filename(path.stem)
        except ValueError as e:
            print(f"skip: {path.name}: {e}", file=sys.stderr)
            continue

        track_value = (f"{track_num}/{total_tracks}"
                       if args.total_tracks else track_num)

        fields = {
            "artist": artist,
            "albumartist": args.album_artist or artist,
            "album": album,
            "title": track_title,
            "tracknumber": track_value,
            "date": args.year,
            "genre": args.genre,
            "discnumber": args.disc,
            "comment": args.comment,
            "composer": args.composer,
            "conductor": args.conductor,
            "work": args.work,
        }

        print(f"tagging: {path.name}")
        for k, v in fields.items():
            if v is not None:
                print(f"  {k}={v!r}")
        if cover is not None:
            print(f"  cover={len(cover)} bytes")

        if args.dry_run:
            tagged += 1
            continue

        ext = path.suffix.lower()
        if ext == ".flac":
            write_flac(path, fields, cover, clean=args.clean)
        elif ext == ".mp3":
            write_mp3(path, fields, cover, clean=args.clean)
        else:
            continue
        tagged += 1

    return tagged


def main() -> int:
    parser = argparse.ArgumentParser(prog="autotag", description=__doc__)
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("folders", nargs="+", type=Path,
                        help="One or more album folders to process.")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Show what would be done without writing tags.")
    parser.add_argument("-s", "--show", action="store_true",
                        help="Print existing tags of files in the folder; do not modify.")
    parser.add_argument("-y", "--year", help="Year/date of release (e.g. 2024).")
    parser.add_argument("-g", "--genre", help="Genre.")
    parser.add_argument("-d", "--disc", help="Disc number (e.g. '1' or '1/2').")
    parser.add_argument("-c", "--comment", help="Comment.")
    parser.add_argument("--composer", help="Composer (e.g. 'J. S. Bach').")
    parser.add_argument("--conductor", help="Conductor.")
    parser.add_argument("--work",
                        help="Work / grouping (e.g. 'Symphony No. 5 in C minor').")
    parser.add_argument("--artist", help="Override artist (default: from folder).")
    parser.add_argument("--album", help="Override album (default: from folder).")
    parser.add_argument("--album-artist",
                        help="Album artist (default: same as artist).")
    parser.add_argument("--total-tracks", action="store_true",
                        help="Write track numbers as 'N/total'.")
    parser.add_argument("--no-cover", action="store_true",
                        help="Do not embed folder.jpg.")
    parser.add_argument("--clean", action="store_true",
                        help="Erase ALL existing tags (and pictures) before writing.")
    args = parser.parse_args()

    if args.show:
        for folder in args.folders:
            if not folder.is_dir():
                print(f"error: {folder} is not a directory", file=sys.stderr)
                continue
            files = collect_audio(folder)
            if not files:
                print(f"(no audio files in {folder})")
                continue
            print(f"\n## {folder}")
            for p in files:
                show_tags(p)
        return 0

    total = 0
    for folder in args.folders:
        if not folder.is_dir():
            print(f"error: {folder} is not a directory", file=sys.stderr)
            continue
        try:
            total += tag_folder(folder, args)
        except (ValueError, MutagenError) as e:
            print(f"error: {folder}: {e}", file=sys.stderr)

    print(f"done: {total} file(s) {'would be ' if args.dry_run else ''}tagged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
