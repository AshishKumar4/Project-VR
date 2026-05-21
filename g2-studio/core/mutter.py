"""Wayland (GNOME/mutter) display helpers via org.gnome.Mutter.DisplayConfig.

Factored from scripts/vr-display-heads.py. Provides read-only state queries
(GetCurrentState), HMD/duplicate detection by EDID vendor+product+serial, and
enable/disable of a hardware head via ApplyMonitorsConfig. On Wayland the HMD is
leased by Monado (wp_drm_lease), not enabled as a desktop monitor, so the only
config we apply is freeing a redundant head for the G2's 2Head1OR mode."""
import json
import os

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

STATE_FILE = "/tmp/mutter_heads_saved.json"
DEST = "org.gnome.Mutter.DisplayConfig"
PATH = "/org/gnome/Mutter/DisplayConfig"
GET_T = "(ua((ssss)a(siiddada{sv})a{sv})a(iiduba(ssss)a{sv})a{sv})"

# HP / HP Reverb manufacturer PNP IDs as reported in EDID vendor field.
HMD_VENDORS = ("HPN", "HP")
HMD_PRODUCT_HINTS = ("REVERB", "G2", "VR")


def bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def get_state():
    """Raw GetCurrentState: (serial, monitors, logical_monitors, props)."""
    r = bus().call_sync(DEST, PATH, DEST, "GetCurrentState", None,
                        GLib.VariantType(GET_T), Gio.DBusCallFlags.NONE, -1, None)
    return r.unpack()


def _current_mode(modes):
    for md in modes:
        if md[6].get("is-current"):
            return md[0]
    return modes[0][0]


def monitors():
    """List of dicts per physical monitor mutter currently knows about."""
    _, mons, _, _ = get_state()
    out = []
    for conn_info, modes, props in mons:
        connector, vendor, product, serial = conn_info
        out.append({
            "connector": connector,          # mutter name, e.g. DP-3 / HDMI-1
            "vendor": vendor,
            "product": product,
            "serial": serial,
            "display_name": props.get("display-name", ""),
            "is_for_lease": bool(props.get("is-for-lease", False)),
            "current_mode": _current_mode(modes),
        })
    return out


def canonical():
    """(serial, [(x,y,scale,transform,primary,[(conn,mode_id)])]) for re-apply."""
    serial, mons, logical, _ = get_state()
    modemap = {m[0][0]: _current_mode(m[1]) for m in mons}
    out = []
    for x, y, scale, transform, primary, lmons, _lp in logical:
        out.append((x, y, scale, transform, primary,
                    [(mm[0], modemap[mm[0]]) for mm in lmons]))
    return serial, out


def find_hmd():
    """Return the connected HMD monitor dict, or None.

    Prefers mutter's is-for-lease flag (set for VR/non-desktop outputs); falls
    back to EDID vendor (HPN/HP) + product hint match. The G2 only appears here
    when awake/connected, so None is normal while it sleeps."""
    cands = monitors()
    for m in cands:
        if m["is_for_lease"]:
            return m
    for m in cands:
        if m["vendor"].upper() in HMD_VENDORS and \
           any(h in m["product"].upper() for h in HMD_PRODUCT_HINTS):
            return m
    return None


def find_duplicate_head():
    """Return a redundant mirrored monitor to disable for VR, or None.

    Two connectors can carry the same panel (same vendor,product,serial) — e.g.
    an eARC HDMI link mirroring a DisplayPort monitor. Keep the primary (or first)
    member and return a redundant one to free its head; connector-type-agnostic."""
    _serial, logical = canonical()
    primary_conns = set()
    for _x, _y, _scale, _transform, primary, mons in logical:
        if primary:
            primary_conns.update(c for c, _mid in mons)

    groups = {}
    for m in monitors():
        groups.setdefault((m["vendor"], m["product"], m["serial"]), []).append(m)
    for _key, group in groups.items():
        if len(group) > 1:
            keep = next((g for g in group if g["connector"] in primary_conns), group[0])
            for g in group:
                if g is not keep:
                    return g
    return None


def apply(logical, method=1):
    """Apply a logical-monitor layout. method 1 = temporary (no persist)."""
    serial = get_state()[0]
    lms = []
    for x, y, scale, transform, primary, mons in logical:
        ms = [GLib.Variant("(ssa{sv})", (c, mid, {})) for c, mid in mons]
        lms.append(GLib.Variant("(iiduba(ssa{sv}))",
                                (x, y, scale, transform, primary, ms)))
    args = GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, method, lms, {}))
    bus().call_sync(DEST, PATH, DEST, "ApplyMonitorsConfig", args, None,
                    Gio.DBusCallFlags.NONE, -1, None)


def _normalize(logical):
    """Tile monitors left-to-right from origin so the layout has no gap/offset
    (mutter rejects ApplyMonitorsConfig if a monitor isn't reachable from 0,0)."""
    out = []
    cur_x = 0
    for x, y, scale, transform, primary, mons in sorted(logical, key=lambda m: (m[0], m[1])):
        w = int(mons[0][1].split("x")[0])           # pixel width from mode id
        lw = int(w / scale) if scale else w          # logical width
        out.append((cur_x, 0, scale, transform, primary, mons))
        cur_x += lw
    return out


def disable_connectors(drop):
    """Disable the named connectors; save prior layout to STATE_FILE for restore."""
    drop = set(drop)
    _serial, lg = canonical()
    json.dump(lg, open(STATE_FILE, "w"))
    keep = [l for l in lg if not any(c in drop for c, _ in l[5])]
    apply(_normalize(keep), method=1)
    return [c for l in keep for c, _ in l[5]]


def restore():
    """Re-apply the layout saved by disable_connectors()."""
    if not os.path.exists(STATE_FILE):
        return False
    lg = [tuple(l[:5]) + ([tuple(m) for m in l[5]],)
          for l in json.load(open(STATE_FILE))]
    apply(lg, method=1)
    return True


def modes_for(connector):
    """All modes of a connector as dicts {id,w,h,refresh,current,preferred},
    sorted by (pixels, refresh) descending."""
    _, mons, _, _ = get_state()
    for conn_info, modes, _props in mons:
        if conn_info[0] == connector:
            out = [{"id": md[0], "w": md[1], "h": md[2], "refresh": md[3],
                    "current": bool(md[6].get("is-current")),
                    "preferred": bool(md[6].get("is-preferred"))} for md in modes]
            out.sort(key=lambda m: (m["w"] * m["h"], m["refresh"]), reverse=True)
            return out
    return []


def refresh_ladder(connector):
    """Mode ids for a connector at its CURRENT resolution, highest refresh first,
    one per distinct refresh (VRR variants deduped, non-VRR preferred). Used to
    dip refresh without changing resolution."""
    ms = modes_for(connector)
    cur = next((m for m in ms if m["current"]), ms[0] if ms else None)
    if cur is None:
        return []
    same = [m for m in ms if m["w"] == cur["w"] and m["h"] == cur["h"]]
    seen = {}  # rounded refresh -> mode id (prefer the non-VRR variant)
    for m in same:
        key = round(m["refresh"])
        if key not in seen or ("+" in seen[key] and "+" not in m["id"]):
            seen[key] = m["id"]
    return [seen[k] for k in sorted(seen, reverse=True)]


def primary_connector():
    """Connector marked primary in the current logical layout, else None."""
    _, _, logical, _ = get_state()
    for _x, _y, _s, _t, primary, lmons, _lp in logical:
        if primary and lmons:
            return lmons[0][0]
    return None


def apply_modes(overrides):
    """Apply the current layout with per-connector mode overrides (e.g. dip
    refresh). overrides: {connector: mode_id}. Positions preserved (assumes the
    overrides keep the same resolution, only changing refresh)."""
    _, lg = canonical()
    newlg = [(x, y, s, t, p, [(c, overrides.get(c, mid)) for c, mid in mons])
             for x, y, s, t, p, mons in lg]
    apply(newlg, method=1)


def apply_layout(modes, primary=None):
    """Apply a desktop layout of EXACTLY the connectors in `modes`
    ({connector: mode_id}), tiled left-to-right from origin. Connectors not in
    `modes` are disabled (frees their head). Scale/transform inherited from the
    current layout; primary flag preserved when that connector is kept."""
    _, cur = canonical()
    attr = {c: (s, t) for _x, _y, s, t, _p, mons in cur for c, _m in mons}
    if primary is None:
        primary = primary_connector()
    items = sorted(modes.items(), key=lambda kv: (kv[0] != primary, kv[0]))
    lg, x = [], 0
    for conn, mid in items:
        s, t = attr.get(conn, (1.0, 0))
        w = int(mid.split("x")[0])
        lg.append((x, 0, s, t, conn == primary, [(conn, mid)]))
        x += int(w / s) if s else w
    apply(lg, method=1)
