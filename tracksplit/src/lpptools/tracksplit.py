#!/usr/bin/env python3
"""Split an audio file into tracks based on quiet gaps.

The analysis computes short-term RMS levels in dBFS over fixed-size blocks
(default 0.5 s), then marks any block strictly above a threshold as "loud".
Contiguous loud blocks form segments; consecutive segments can optionally be
fused together to absorb spurious gaps. The boundaries between segments
become cut points, and ffmpeg stream-copies each resulting track.
"""

import argparse
import importlib.metadata
import math
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


def _version_str() -> str:
    try:
        return importlib.metadata.version("tracksplit")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


__version__ = _version_str()


# ---------------------------------------------------------------------------
# Tunable constants
# ---------------------------------------------------------------------------

DEFAULT_BLOCK_SECONDS = 0.5
DEFAULT_THRESHOLD_PERCENTILE = 2.0

MIN_REPORT_DB_FS = -120.0

FUSE_MIN_TRACK_SECONDS = 15.0
FUSE_MIN_TRACK_GAP_SECONDS = 60.0

FUSE_GAP_SECONDS = 2.0


# Each interior cut is placed this many seconds before the loud segment start.
LEAD_SECONDS = 1.0

_SUBTYPE_LABELS: Mapping[str, str] = {
    "PCM_S8": "8-bit PCM",
    "PCM_U8": "8-bit PCM",
    "PCM_16": "16-bit",
    "PCM_24": "24-bit",
    "PCM_32": "32-bit",
    "FLOAT": "32-bit float",
    "DOUBLE": "64-bit float",
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedAudio:
    mono: object  # numpy.ndarray, kept as object to avoid a hard import here
    samplerate_hz: float
    channels: int
    duration_s: float
    bit_depth_label: str


@dataclass(frozen=True)
class Segment:
    """Inclusive block-index range of consecutive loud blocks."""

    start_idx: int
    end_idx: int

    def length_blocks(self) -> int:
        return self.end_idx - self.start_idx + 1

    def length_seconds(self, block_s: float) -> float:
        return self.length_blocks() * block_s

    def start_seconds(self, block_s: float) -> float:
        return self.start_idx * block_s


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tracksplit",
        description=(
            "Split an audio file into individual tracks at quiet gaps using ffmpeg. Pass --doit to actually run the ffmpeg commands."
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-i",
        "--input",
        dest="input_path",
        required=True,
        help="Input audio file",
    )
    parser.add_argument(
        "--analyse",
        dest="analyse_path",
        default=None,
        help=(
            "Use a different file for the gap analysis. Useful if libsndfile can't decode input file."
        ),
    )

    thresh = parser.add_mutually_exclusive_group()
    thresh.add_argument(
        "--thresh-pct",
        dest="thresh_pct",
        type=float,
        default=None,
        metavar="PERCENTILE",
        help=(
            "Percentile of the audio file dBFS levels to use as quiet threshold "
            f"(default: {DEFAULT_THRESHOLD_PERCENTILE:g})."
        ),
    )
    thresh.add_argument(
        "--thresh-db",
        dest="thresh_db",
        type=float,
        default=None,
        metavar="DBFS",
        help="Use a fixed dBFS threshold instead of a percentile for quiet threshold.",
    )

    parser.add_argument(
        "--block",
        dest="block_seconds",
        type=float,
        default=DEFAULT_BLOCK_SECONDS,
        metavar="SECONDS",
        help=f"dBFS analysis block size in seconds (default: {DEFAULT_BLOCK_SECONDS}).",
    )
    parser.add_argument(
        "--no-fuse",
        dest="fuse",
        action="store_false",
        help=(
            "Disable fuse algorithm. The fuse algorithm first merges segments shorter than --fuse-min "
            "with closest neighbour regardless of gap. After that segments separated by less than --fuse-gap are merged."
        ),
    )
    parser.set_defaults(fuse=True)
    parser.add_argument(
        "--fuse-gap",
        dest="fuse_gap_s",
        type=float,
        default=FUSE_GAP_SECONDS,
        metavar="SECONDS",
        help=f"Fuse algorithm gap threshold in seconds (default: {FUSE_GAP_SECONDS:g}).",
    )
    parser.add_argument(
        "--fuse-min",
        dest="fuse_min_track_s",
        type=float,
        default=FUSE_MIN_TRACK_SECONDS,
        metavar="SECONDS",
        help=(
            "Fuse algorithm: segments shorter than this are merged with closest neighbour "
            f"(default: {FUSE_MIN_TRACK_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--lead",
        dest="lead_s",
        type=float,
        default=LEAD_SECONDS,
        metavar="SECONDS",
        help=(
            "Place track cut points this many seconds before detected loudness of next segment "
            f"(default: {LEAD_SECONDS:g})."
        ),
    )
    parser.add_argument(
        "--keep-start",
        dest="keep_start",
        action="store_true",
        help=(
            "Don't cut silence at the start of the first track."
        ),
    )
    parser.add_argument(
        "--enc-flac",
        dest="enc_flac",
        action="store_true",
        help="Instruct ffmpeg to encode as FLAC (-acodec flac)",
    )
    parser.add_argument(
        "--doit",
        action="store_true",
        help="Actually run the printed ffmpeg commands (default: dry run).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print analysis details (load info, threshold, segment table).",
    )
    return parser.parse_args(list(argv))


# ---------------------------------------------------------------------------
# Audio loading
# ---------------------------------------------------------------------------


class UnsupportedSoundFileFormat(RuntimeError):
    def __init__(self, input_path: str, cause_detail: str) -> None:
        self.input_path = input_path
        super().__init__(
            f"Unable to load {input_path!r} via SoundFile (libsndfile). {cause_detail}",
        )


def _libsndfile_format_unrecognized(lib_msg: str) -> bool:
    m = lib_msg.lower()
    return (
        "format not recognised" in m
        or "format not recognized" in m
        or "unsupported format" in m
        or "unknown format" in m
    )


def _bit_depth_label(subtype: str | None) -> str:
    if not subtype:
        return "unknown"
    upper = subtype.upper()
    if upper in _SUBTYPE_LABELS:
        return _SUBTYPE_LABELS[upper]
    parts = subtype.split("_")
    if len(parts) == 2 and parts[0].upper() == "PCM" and parts[1].isdigit():
        return f"{parts[1]}-bit"
    return subtype


def load_audio(path: str) -> LoadedAudio:
    import numpy as np
    import soundfile as sf

    try:
        with sf.SoundFile(path) as f:
            sr = float(f.samplerate)
            subtype = f.subtype
            data = f.read(dtype="float32", always_2d=True)
    except (OSError, sf.LibsndfileError, RuntimeError) as e:
        detail = str(e)
        if _libsndfile_format_unrecognized(detail):
            raise UnsupportedSoundFileFormat(path, detail) from e
        raise RuntimeError(
            f"Unable to load {path!r} via SoundFile (libsndfile). {detail}"
        ) from e

    if sr <= 0:
        raise RuntimeError(f"Invalid or missing sample rate in {path!r}")

    y = np.ascontiguousarray(data.T)
    n_chan = y.shape[0]
    duration_s = float(y.shape[1]) / sr
    mono = (
        np.mean(y, axis=0).astype(np.float64, copy=False)
        if n_chan > 1
        else y[0].astype(np.float64, copy=False)
    )

    return LoadedAudio(
        mono=mono,
        samplerate_hz=sr,
        channels=n_chan,
        duration_s=duration_s,
        bit_depth_label=_bit_depth_label(subtype),
    )


def _ffmpeg_convert_to_wav_command(input_path: str) -> str:
    inp = Path(input_path).expanduser()
    out = inp.with_suffix(".wav")
    return (
        f"ffmpeg -i {shlex.quote(str(inp))} -vn "
        f"-acodec pcm_s16le {shlex.quote(str(out))}"
    )


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def linear_to_dbfs(rms: float, eps: float = 1e-12) -> float:
    return max(20.0 * math.log10(max(rms, eps)), MIN_REPORT_DB_FS)


def rms_block_levels_dbfs(mono_samples, sr: float, block_s: float) -> list[float]:
    """Return per-block dBFS levels covering the whole signal."""
    import numpy as np

    if block_s <= 0:
        raise ValueError("block_s must be > 0")

    samples = np.asarray(mono_samples, dtype=np.float64)
    hop = max(1, int(round(sr * block_s)))
    n = samples.shape[0]
    levels: list[float] = []
    for start in range(0, n, hop):
        chunk = samples[start : start + hop]
        if chunk.size == 0:
            break
        rms = math.sqrt(float(np.mean(np.square(chunk))))
        levels.append(linear_to_dbfs(rms))
    return levels


def percentile_threshold_dbfs(db_levels: Sequence[float], percentile: float) -> float:
    import numpy as np

    if not db_levels:
        return MIN_REPORT_DB_FS
    return float(np.percentile(np.asarray(db_levels, dtype=np.float64), percentile))


def find_loud_segments(db_levels: Sequence[float], threshold_dbfs: float) -> list[Segment]:
    """Contiguous block-index ranges strictly above ``threshold_dbfs``."""
    segments: list[Segment] = []
    i = 0
    n = len(db_levels)
    while i < n:
        if db_levels[i] <= threshold_dbfs:
            i += 1
            continue
        start = i
        i += 1
        while i < n and db_levels[i] > threshold_dbfs:
            i += 1
        segments.append(Segment(start, i - 1))
    return segments


# ---------------------------------------------------------------------------
# Fusing
# ---------------------------------------------------------------------------


def _gap_blocks(left: Segment, right: Segment) -> int:
    return right.start_idx - left.end_idx - 1


def fuse_short_segments(
    segments: Sequence[Segment],
    block_s: float,
    *,
    min_track_s: float,
    gap_s: float,
) -> list[Segment]:
    """Repeatedly merge any too-short segment with its closest reachable neighbour."""
    if not segments:
        return []

    min_track_blocks = math.ceil(min_track_s / block_s)
    max_gap_blocks = int(math.floor(gap_s / block_s))

    segs = list(segments)
    while True:
        merged = False
        for idx, seg in enumerate(segs):
            if seg.length_blocks() >= min_track_blocks:
                continue

            choices: list[tuple[str, int]] = []
            if idx > 0:
                gap_l = _gap_blocks(segs[idx - 1], seg)
                if gap_l <= max_gap_blocks:
                    choices.append(("L", gap_l))
            if idx + 1 < len(segs):
                gap_r = _gap_blocks(seg, segs[idx + 1])
                if gap_r <= max_gap_blocks:
                    choices.append(("R", gap_r))

            if not choices:
                continue

            direction, _ = min(choices, key=lambda t: (t[1], 0 if t[0] == "L" else 1))
            if direction == "L":
                segs[idx - 1 : idx + 1] = [Segment(segs[idx - 1].start_idx, seg.end_idx)]
            else:
                segs[idx : idx + 2] = [Segment(seg.start_idx, segs[idx + 1].end_idx)]
            merged = True
            break

        if not merged:
            return segs


def fuse_close_neighbours(
    segments: Sequence[Segment],
    block_s: float,
    *,
    gap_s: float,
) -> list[Segment]:
    """Merge any consecutive segments separated by strictly less than ``gap_s``."""
    if len(segments) <= 1:
        return list(segments)

    min_gap_blocks = math.ceil(gap_s / block_s)
    out: list[Segment] = [segments[0]]
    for seg in segments[1:]:
        if _gap_blocks(out[-1], seg) < min_gap_blocks:
            out[-1] = Segment(out[-1].start_idx, max(out[-1].end_idx, seg.end_idx))
        else:
            out.append(seg)
    return out


# ---------------------------------------------------------------------------
# Track windows + ffmpeg
# ---------------------------------------------------------------------------


def contiguous_track_windows(
    segments: Sequence[Segment],
    block_s: float,
    total_duration_s: float,
    *,
    lead_s: float = LEAD_SECONDS,
    cut_start: bool = False,
) -> list[tuple[float, float]]:
    """Partition [first_start, total_duration_s) into N contiguous (start, duration) windows.

    With ``cut_start=False`` (default) the first window starts at 0, covering any
    pre-roll before the first detected segment. With ``cut_start=True`` it starts
    at ``segments[0].start - lead_s`` (clamped to 0), discarding the pre-roll.
    """
    n = len(segments)
    if n == 0:
        return []

    total = max(0.0, float(total_duration_s))
    first = (
        max(0.0, segments[0].start_seconds(block_s) - lead_s) if cut_start else 0.0
    )
    boundaries = [first]
    for i in range(1, n):
        boundaries.append(max(0.0, segments[i].start_seconds(block_s) - lead_s))
    boundaries.append(total)

    return [
        (boundaries[i], max(0.0, boundaries[i + 1] - boundaries[i]))
        for i in range(n)
    ]


def _ffmpeg_time_arg(seconds: float) -> str:
    s = f"{seconds:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


def build_ffmpeg_commands(
    segments: Sequence[Segment],
    block_s: float,
    source_file: str,
    duration_s: float,
    *,
    lead_s: float = LEAD_SECONDS,
    cut_start: bool = False,
    enc_flac: bool = False,
) -> list[list[str]]:
    """One ffmpeg command per output track. Outputs land in cwd.

    By default each command stream-copies audio. With ``enc_flac=True``, audio is
    re-encoded as FLAC and filenames use a ``.flac`` suffix.
    """
    src_path = Path(source_file).expanduser()
    base_name = src_path.name

    windows = contiguous_track_windows(
        segments, block_s, duration_s, lead_s=lead_s, cut_start=cut_start,
    )

    commands: list[list[str]] = []
    src_arg = str(src_path)
    for track_no, (start_s, dur_s) in enumerate(windows, start=1):
        if enc_flac:
            out_name = f"{track_no:02d} {src_path.stem}.flac"
            codec_args: list[str] = ["-map", "0:a:0", "-acodec", "flac"]
        else:
            out_name = f"{track_no:02d} {base_name}"
            codec_args = ["-map", "0:a:0", "-c", "copy"]
        commands.append([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", _ffmpeg_time_arg(start_s),
            "-i", src_arg,
            "-t", _ffmpeg_time_arg(dur_s),
            *codec_args,
            out_name,
        ])
    return commands


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


def format_duration_hms(total_seconds: float) -> str:
    sec = max(0, int(round(total_seconds)))
    hours, remainder = divmod(sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def print_load_summary(audio: LoadedAudio, path: str) -> None:
    ch_word = "channel" if audio.channels == 1 else "channels"
    print(
        f"Loaded {path} — {audio.channels} {ch_word}, "
        f"{audio.samplerate_hz / 1000:g} kHz, {audio.bit_depth_label}, "
        f"duration {format_duration_hms(audio.duration_s)}."
    )


def print_segments_table(segments: Sequence[Segment], block_s: float) -> None:
    if not segments:
        print("  (none)")
        return
    for i, seg in enumerate(segments):
        start_tc = format_duration_hms(seg.start_seconds(block_s))
        length_s = seg.length_seconds(block_s)
        if i + 1 < len(segments):
            gap_blocks = _gap_blocks(seg, segments[i + 1])
            gap_s = f", gap {max(0.0, gap_blocks * block_s):g} s"
        else:
            gap_s = ""
        print(
            f"  #{i + 1} start {start_tc}, length {length_s:5g} s{gap_s}"
        )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def compute_segments(
    db_levels: Sequence[float],
    *,
    thresh_pct: float | None,
    thresh_db: float | None,
    block_s: float,
    fuse: bool,
    fuse_min_track_s: float,
    fuse_gap_s: float,
    verbose: bool,
) -> list[Segment]:
    if thresh_db is not None:
        threshold = float(thresh_db)
        label = f"manual {threshold:.2f} dBFS"
    else:
        pct = DEFAULT_THRESHOLD_PERCENTILE if thresh_pct is None else float(thresh_pct)
        threshold = percentile_threshold_dbfs(db_levels, pct)
        label = f"{pct:g} percentile giving {threshold:.2f} dBFS"

    if verbose:
        print(f"Threshold: {label}")

    segments = find_loud_segments(db_levels, threshold)

    if fuse:
        before = len(segments)
        segments = fuse_short_segments(
            segments, block_s, min_track_s=fuse_min_track_s, gap_s=FUSE_MIN_TRACK_GAP_SECONDS,
        )
        after_short = len(segments)
        segments = fuse_close_neighbours(segments, block_s, gap_s=fuse_gap_s)
        if verbose:
            print(
                f"Fused: {before} tracks to {after_short} (short-track-merge) "
                f"and to {len(segments)} (short-gap-merge)."
            )

    if verbose:
        print(f"Tracks identified: (time resolution {block_s:g} s)")
        print_segments_table(segments, block_s)

    return segments


def run_commands(commands: Iterable[list[str]]) -> int:
    for cmd in commands:
        print(f"  $ {shlex.join(cmd)}")
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            print(f"ffmpeg failed (exit {proc.returncode}); stopping.", file=sys.stderr)
            return proc.returncode
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.block_seconds <= 0:
        print("Error: --block must be > 0.", file=sys.stderr)
        return 2
    if args.thresh_pct is not None and not (0.0 <= args.thresh_pct <= 100.0):
        print("Error: --thresh-pct must be in [0, 100].", file=sys.stderr)
        return 2

    analyse_path = args.analyse_path or args.input_path

    try:
        loaded = load_audio(analyse_path)
    except UnsupportedSoundFileFormat as e:
        print(f"Error: {e}", file=sys.stderr)
        print("\nConvert to WAV with ffmpeg:", file=sys.stderr)
        print(f"  {_ffmpeg_convert_to_wav_command(e.input_path)}\n", file=sys.stderr)
        print(
            "Or pass a separate analysis file via --analyse <wav>.",
            file=sys.stderr,
        )
        return 1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.verbose:
        print_load_summary(loaded, analyse_path)
        if analyse_path != args.input_path:
            print(f"(ffmpeg input is {args.input_path})")

    db_levels = rms_block_levels_dbfs(
        loaded.mono, loaded.samplerate_hz, args.block_seconds
    )

    segments = compute_segments(
        db_levels,
        thresh_pct=args.thresh_pct,
        thresh_db=args.thresh_db,
        block_s=args.block_seconds,
        fuse=args.fuse,
        fuse_min_track_s=args.fuse_min_track_s,
        fuse_gap_s=args.fuse_gap_s,
        verbose=args.verbose,
    )

    commands = build_ffmpeg_commands(
        segments,
        args.block_seconds,
        args.input_path,
        loaded.duration_s,
        lead_s=args.lead_s,
        cut_start=not args.keep_start,
        enc_flac=args.enc_flac,
    )

    if args.verbose:
        print()
    print(f"ffmpeg commands ({len(commands)} track{'s' if len(commands) != 1 else ''}):")
    if not commands:
        print("  (none — no segments detected)")
    else:
        for cmd in commands:
            print(f"  {shlex.join(cmd)}")

    if args.doit and commands:
        print()
        print("Running:")
        return run_commands(commands)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
