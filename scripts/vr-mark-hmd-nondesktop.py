#!/usr/bin/env python3
"""
Mark every connected VR-headset RandR output as non-desktop.

Direct-mode VR compositors (SteamVR's vrcompositor, Monado's RandR backend) can
only lease an output that X is *not* scanning out as desktop, i.e. one whose
RandR `non-desktop` property is 1. NVIDIA's proprietary X driver never sets this
from the EDID (unlike the in-tree DRM stack), so the HMD shows up as an ordinary
desktop monitor and `vkAcquireXlibDisplayEXT` / DRM-lease fails.

This script is general: it identifies headsets by the EDID's Microsoft VR
Vendor-Specific Data Block "primary use case" (7 = Virtual Reality Headset,
8 = Augmented Reality), not by a hardcoded model. Any WMR-class HMD works.

Idempotent. Safe to run repeatedly (e.g. from vrstartup.sh). No root required.
"""
import re
import subprocess
import sys


def xrandr_verbose() -> str:
    return subprocess.run(["xrandr", "--verbose"], capture_output=True,
                          text=True, check=True).stdout


def parse_outputs(text: str):
    """Yield (output_name, edid_hex_or_None, non_desktop_value_or_None)."""
    out = None
    edid_lines, in_edid = [], False
    name, edid, nondesktop = None, None, None
    results = []

    def flush():
        if name is not None:
            results.append((name, "".join(edid_lines) or None, nondesktop))

    for line in text.splitlines():
        m = re.match(r"^([A-Za-z][\w-]*) (connected|disconnected)", line)
        if m:
            flush()
            name = m.group(1)
            edid_lines, in_edid, edid, nondesktop = [], False, None, None
            continue
        if name is None:
            continue
        if re.match(r"^\tEDID:", line):
            in_edid = True
            continue
        if in_edid and re.match(r"^\t\t[0-9a-fA-F]+\s*$", line):
            edid_lines.append(line.strip())
            continue
        in_edid = False
        m = re.match(r"^\tnon-desktop:\s*(\d+)", line)
        if m:
            nondesktop = m.group(1)
    flush()
    return results


def is_vr_headset(edid_hex: str) -> bool:
    """True if edid-decode reports this EDID as a VR/AR headset."""
    try:
        raw = bytes.fromhex(edid_hex)
    except ValueError:
        return False
    try:
        dec = subprocess.run(["edid-decode"], input=raw, capture_output=True,
                             check=False)
        text = dec.stdout.decode(errors="ignore")
    except FileNotFoundError:
        print("vr-mark-hmd-nondesktop: edid-decode not installed", file=sys.stderr)
        return False
    return ("virtual reality headset" in text.lower()
            or "augmented reality" in text.lower())


def main() -> int:
    try:
        outputs = parse_outputs(xrandr_verbose())
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"vr-mark-hmd-nondesktop: cannot query xrandr ({e})", file=sys.stderr)
        return 1

    changed = 0
    for name, edid, nondesktop in outputs:
        if not edid or not is_vr_headset(edid):
            continue
        if nondesktop == "1":
            print(f"vr-mark-hmd-nondesktop: {name} already non-desktop")
            continue
        print(f"vr-mark-hmd-nondesktop: VR headset on {name} -> non-desktop=1")
        try:
            subprocess.run(["xrandr", "--output", name, "--set",
                            "non-desktop", "1"], check=True)
            changed += 1
        except subprocess.CalledProcessError as e:
            print(f"vr-mark-hmd-nondesktop: failed to set {name}: {e}",
                  file=sys.stderr)
            return 1

    if changed:
        print(f"vr-mark-hmd-nondesktop: marked {changed} HMD output(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
