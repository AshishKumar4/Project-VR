"""Shared manifest parsing for the G2 telemetry pipeline.

The producer (Monado's u_g2_telemetry.c) emits a self-describing manifest.json.
Everything here is driven by that manifest -- no hardcoded byte offsets.
See docs/TELEMETRY-SCHEMA.md for the contract.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# manifest type token -> numpy little-endian scalar dtype
TYPE_MAP: dict[str, np.dtype] = {
    "u8": np.dtype("<u1"),
    "u16": np.dtype("<u2"),
    "u32": np.dtype("<u4"),
    "u64": np.dtype("<u8"),
    "f32": np.dtype("<f4"),
    "f64": np.dtype("<f8"),
}

# enum decode tables (kept here so producer + consumer share one source).
# Every value below has a real producer in the Monado taps (see TELEMETRY-SCHEMA.md):
#   pose_attempt outcome 2=recovered -> constellation labelled-blob re-acquisition path
#   fusion outcome 0=rejected -> optical-jump gate, 2=reset -> residual over residual_limit
#   event 3=optical_jump_rejected (value=jump m), 4=imu_anomaly (value=backwards delta ms),
#   5=ring_overflow (value=stream id; diagnostic, rate-limited <=1/s -- exclude from rates).
DEVICE_NAMES = {0: "HMD", 1: "left", 2: "right"}
POSE_OUTCOME = {0: "rejected", 1: "accepted", 2: "recovered"}
FUSION_OUTCOME = {0: "rejected", 1: "accepted", 2: "reset"}
EVENT_TYPES = {
    0: "lock_lost",
    1: "lock_acquired",
    2: "recover_attempt",
    3: "optical_jump_rejected",
    4: "imu_anomaly",
    5: "ring_overflow",
}


@dataclass
class Field:
    name: str
    type: str
    offset: int

    @property
    def np_dtype(self) -> np.dtype:
        if self.type not in TYPE_MAP:
            raise ValueError(f"unknown field type {self.type!r} for field {self.name!r}")
        return TYPE_MAP[self.type]


@dataclass
class Stream:
    name: str
    file: str
    row_size: int
    fields: list[Field]
    rows_written: int | None
    overflow_total: int | None

    def structured_dtype(self) -> np.dtype:
        """Build a numpy structured dtype that respects per-field offsets and
        uses row_size as the itemsize (so any inter-field/trailing padding the
        producer added is preserved and rows stride correctly)."""
        spec = {
            "names": [f.name for f in self.fields],
            "formats": [f.np_dtype for f in self.fields],
            "offsets": [f.offset for f in self.fields],
            "itemsize": self.row_size,
        }
        dt = np.dtype(spec)
        if dt.itemsize != self.row_size:
            raise ValueError(
                f"stream {self.name}: dtype itemsize {dt.itemsize} != manifest row_size {self.row_size}"
            )
        return dt


@dataclass
class Manifest:
    version: int
    clock: str
    start: dict
    types: str
    streams: dict[str, Stream]
    raw: dict

    @classmethod
    def load(cls, telemetry_dir: str | Path) -> "Manifest":
        d = Path(telemetry_dir)
        path = d / "manifest.json"
        if not path.is_file():
            raise FileNotFoundError(f"no manifest.json in {d}")
        raw = json.loads(path.read_text())
        streams: dict[str, Stream] = {}
        for name, s in raw.get("streams", {}).items():
            fields = [Field(f["name"], f["type"], int(f["offset"])) for f in s["fields"]]
            streams[name] = Stream(
                name=name,
                file=s["file"],
                row_size=int(s["row_size"]),
                fields=fields,
                rows_written=s.get("rows_written"),
                overflow_total=s.get("overflow_total"),
            )
        return cls(
            version=int(raw.get("version", 0)),
            clock=raw.get("clock", ""),
            start=raw.get("start", {}),
            types=raw.get("types", ""),
            streams=streams,
            raw=raw,
        )
