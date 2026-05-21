#!/usr/bin/env python3
"""Free/restore a hardware head by toggling display outputs via mutter DBus.
Subcommands: dump | disable <CONN..> | restore   (state saved to STATE_FILE).

Thin CLI over core/mutter.py — shared with g2-studio so the DBus logic lives
in one place."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import mutter  # noqa: E402

cmd = sys.argv[1] if len(sys.argv) > 1 else "dump"
if cmd == "dump":
    s, lg = mutter.canonical()
    print("serial", s)
    for l in lg:
        print(" ", l)
elif cmd == "disable":
    drop = sys.argv[2:]
    kept = mutter.disable_connectors(drop)
    print("keeping:", kept)
    print("applied (temporary).")
elif cmd == "restore":
    if not mutter.restore():
        print("no saved state")
        sys.exit(1)
    print("restored.")
else:
    print("usage: vr-display-heads.py dump|disable <CONN..>|restore")
    sys.exit(2)
