"""Patch selkies' input_handler.py so absolute mouse motion survives an
Xwayland pointer lock (marker scummvm-broker-mod:wayland-abs-motion).

usage: abs_motion_patch.py <path/to/selkies/input_handler.py>

Why: on the Wayland (labwc-in-pixelflux) image, every mouse event is injected
into pixelflux's own compositor and reaches labwc as a wl_pointer event. The
browser's normal mouse mode arrives as *absolute* motion, which labwc handles
in handle_motion_absolute(): it converts it to a delta and drops it whole
while a pointer lock is active (preprocess_cursor_motion returns early). A
*relative* motion goes through handle_motion(), which forwards the delta over
zwp_relative_pointer before the same early return, so a locked client still
receives it. ScummVM hides the system cursor and warps the pointer, and
Xwayland emulates that warp by locking the pointer for the rest of the game
(xwl_seat_emulate_pointer_warp -> zwp_locked_pointer): from then on the game
cursor only follows relative motion, which is why the client's pointer-lock
"game mode" worked and normal mode froze.

Fix: for every absolute target, first inject the delta from the previous
target as relative motion, then the absolute position. Locked: the relative
delta lands 1:1 and the absolute is dropped. Unlocked: labwc applies the
relative twice (pixelflux sends the relative arm as motion + relative_motion)
and the absolute that follows warps the cursor to the exact target, so the
net position is right either way. A game warp (or the lock starting
off-position) displaces Xwayland's emulated pointer from our targets; after a
short settle the X pointer is read back and the next delta is measured from
where the pointer actually is. The readback is gated: only once the last
injection had time to land (else a still-in-flight delta would be re-sent),
only while an Xwayland process exists (an X connection would otherwise spawn
labwc's lazy Xwayland for nothing), and only while the pointer is over an X
window (Xwayland reports a stale position elsewhere).
"""
import ast
import sys

MARKER = "scummvm-broker-mod:wayland-abs-motion"

OLD = """            if not is_static_relative:
                if relative:
                    if hasattr(self.wayland_input, 'inject_relative_mouse_move'):
                        self.wayland_input.inject_relative_mouse_move(float(x), float(y))
                    else:
                        self.wayland_input.inject_mouse_move(float(final_x), float(final_y))
                else:
                    self.wayland_input.inject_mouse_move(float(final_x), float(final_y))"""

NEW = """            if not is_static_relative:
                if relative:
                    if hasattr(self.wayland_input, 'inject_relative_mouse_move'):
                        self.wayland_input.inject_relative_mouse_move(float(x), float(y))
                    else:
                        self.wayland_input.inject_mouse_move(float(final_x), float(final_y))
                    # scummvm-broker-mod:wayland-abs-motion -- keep the
                    # absolute tracker in step with the relative path.
                    self._bm_last_abs = (float(final_x), float(final_y))
                else:
                    # scummvm-broker-mod:wayland-abs-motion -- relative delta
                    # first (crosses an Xwayland pointer lock), then the
                    # absolute position (exact when there is no lock).
                    _bm_abs_move(self, float(final_x), float(final_y))"""

HELPERS = '''

# scummvm-broker-mod:wayland-abs-motion helpers. See the patch header in the
# broker mod for the mechanism; in short: labwc drops absolute motion under a
# pointer lock but forwards relative motion, so every absolute move is sent as
# delta-then-absolute, and the X pointer is read back after a pause so a game
# warp does not leave a permanent offset.
import os as _bm_os
import time as _bm_time

_BM_SETTLE_S = 0.08
_BM_XWL_SCAN_S = 1.0
_bm_display = None
_bm_xwl_seen_at = -1e9
_bm_xwl_scan_at = -1e9


def _bm_xwayland_running():
    """True when an Xwayland process exists (cheap /proc scan, at most once a
    second while none is found). Connecting to labwc's lazy X socket would
    start one, so nothing below touches X until a client already has."""
    global _bm_xwl_seen_at, _bm_xwl_scan_at
    now = _bm_time.monotonic()
    if now - _bm_xwl_seen_at < _BM_XWL_SCAN_S:
        return True
    if now - _bm_xwl_scan_at < _BM_XWL_SCAN_S:
        return False
    _bm_xwl_scan_at = now
    try:
        for pid in _bm_os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm", "rb") as f:
                    if f.read(9).rstrip() == b"Xwayland":
                        _bm_xwl_seen_at = now
                        return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def _bm_x_display():
    """The session's X display: what selkies resolved for app launches, else
    $DISPLAY, else :0."""
    return _bm_os.environ.get("DISPLAY") or ":0"


def _bm_query_pointer(handler):
    """The X pointer position, or None when it cannot be trusted: no live
    Xwayland, no X display, or the pointer over no X window (Xwayland then
    reports the last position it had inside one)."""
    global _bm_display
    if not _bm_xwayland_running():
        return None
    try:
        if _bm_display is None:
            import Xlib.display as _bm_xd
            name = None
            try:
                name = handler.app_session().get("x11_display")
            except Exception:
                pass
            _bm_display = _bm_xd.Display(name or _bm_x_display())
        q = _bm_display.screen().root.query_pointer()
        if not q.child:
            return None
        return (q.root_x, q.root_y)
    except Exception:
        try:
            if _bm_display is not None:
                _bm_display.close()
        except Exception:
            pass
        _bm_display = None
        return None


def _bm_abs_move(handler, tx, ty):
    """Inject an absolute move as relative delta + absolute position."""
    now = _bm_time.monotonic()
    prev = getattr(handler, "_bm_last_abs", None)
    dx = dy = 0.0
    if prev is not None:
        dx, dy = tx - prev[0], ty - prev[1]
        if now - getattr(handler, "_bm_last_abs_at", -1e9) >= _BM_SETTLE_S:
            pos = _bm_query_pointer(handler)
            if pos is not None and (abs(pos[0] - prev[0]) > 1
                                    or abs(pos[1] - prev[1]) > 1):
                dx, dy = tx - pos[0], ty - pos[1]
    wi = handler.wayland_input
    if (dx or dy) and hasattr(wi, "inject_relative_mouse_move"):
        wi.inject_relative_mouse_move(dx, dy)
    wi.inject_mouse_move(tx, ty)
    handler._bm_last_abs = (tx, ty)
    handler._bm_last_abs_at = now
'''


def main(path):
    src = open(path).read()
    if MARKER in src:
        print("[broker-mod] selkies wayland absolute-motion patch already applied.")
        return 0
    count = src.count(OLD)
    if count != 1:
        print(f"[broker-mod] ERROR: wayland mouse block found {count} times, "
              "absolute-motion patch NOT applied (upstream may have changed)")
        return 0
    patched = src.replace(OLD, NEW) + HELPERS
    ast.parse(patched)
    open(path, "w").write(patched)
    print("[broker-mod] Patched selkies wayland absolute pointer injection (lock-aware).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
