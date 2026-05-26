#!/usr/bin/env python3
"""convert.py -- G2 telemetry .bin -> .parquet, driven entirely by manifest.json.

For each stream in the manifest: build a numpy structured dtype from the fields
(respecting offsets + row_size as itemsize), np.fromfile the packed .bin, floor
to whole rows for crash-safety against a truncated final row, and write a Parquet
file. Prints rows / overflow per stream.

Usage:
    python3 convert.py [TELEMETRY_DIR] [-o OUT_DIR]

TELEMETRY_DIR defaults to $G2_TELEMETRY or the current directory.
OUT_DIR defaults to TELEMETRY_DIR.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np

from manifest import Manifest, Stream


def _arrow_table_from_array(arr: np.ndarray):
    """Convert a numpy structured array to a pyarrow Table (column per field)."""
    import pyarrow as pa

    cols = {name: pa.array(np.ascontiguousarray(arr[name])) for name in arr.dtype.names}
    return pa.table(cols)


def convert_stream(stream: Stream, telemetry_dir: Path, out_dir: Path) -> dict:
    """Read one stream's .bin and write its .parquet. Returns a stats dict."""
    bin_path = telemetry_dir / stream.file
    dt = stream.structured_dtype()
    info = {
        "stream": stream.name,
        "row_size": stream.row_size,
        "rows_manifest": stream.rows_written,
        "overflow": stream.overflow_total or 0,
        "rows_read": 0,
        "truncated": False,
        "parquet": None,
        "missing": False,
    }

    if not bin_path.is_file():
        info["missing"] = True
        return info

    # Crash-safety: a truncated final row (writer killed mid-row) must not corrupt
    # the read. Floor the byte count to a whole multiple of row_size.
    nbytes = bin_path.stat().st_size
    whole = nbytes - (nbytes % stream.row_size)
    if whole != nbytes:
        info["truncated"] = True
    count = whole // stream.row_size

    arr = np.fromfile(bin_path, dtype=dt, count=count)
    info["rows_read"] = int(arr.shape[0])

    table = _arrow_table_from_array(arr)
    import pyarrow.parquet as pq

    out_path = out_dir / f"{stream.name}.parquet"
    pq.write_table(table, out_path)
    info["parquet"] = str(out_path)
    return info


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Convert G2 telemetry .bin to .parquet")
    ap.add_argument(
        "telemetry_dir",
        nargs="?",
        default=os.environ.get("G2_TELEMETRY", "."),
        help="dir with manifest.json + *.bin (default: $G2_TELEMETRY or .)",
    )
    ap.add_argument("-o", "--out", default=None, help="output dir (default: telemetry_dir)")
    args = ap.parse_args(argv)

    telemetry_dir = Path(args.telemetry_dir)
    out_dir = Path(args.out) if args.out else telemetry_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest = Manifest.load(telemetry_dir)
    print(f"manifest version={manifest.version} clock={manifest.clock}")
    print(f"start: {manifest.start}")
    print(f"in:  {telemetry_dir}")
    print(f"out: {out_dir}")
    print()

    header = f"{'stream':<14}{'rows':>10}{'manifest':>10}{'overflow':>10}  notes"
    print(header)
    print("-" * len(header))

    any_overflow = False
    any_truncated = False
    for stream in manifest.streams.values():
        info = convert_stream(stream, telemetry_dir, out_dir)
        notes = []
        if info["missing"]:
            notes.append("MISSING .bin")
        if info["truncated"]:
            notes.append("truncated final row (floored)")
            any_truncated = True
        if info["overflow"] > 0:
            notes.append("OVERFLOW>0")
            any_overflow = True
        mf = "-" if info["rows_manifest"] is None else str(info["rows_manifest"])
        print(
            f"{info['stream']:<14}{info['rows_read']:>10}{mf:>10}"
            f"{info['overflow']:>10}  {', '.join(notes)}"
        )

    print()
    if any_truncated:
        print("note: one or more streams had a truncated final row; floored to whole rows.")
    if any_overflow:
        print("WARNING: ring overflow detected in the manifest (>0). Data has gaps.")
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
