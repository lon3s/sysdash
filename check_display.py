#!/usr/bin/python3
"""Prueft die Screen-Position des SYSDASH-Fensters via AT-SPI (Wayland-tauglich)."""
import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

Atspi.init()
d = Atspi.get_desktop(0)
found = []

def walk(acc):
    for i in range(acc.get_child_count()):
        c = acc.get_child_at_index(i)
        if c is None:
            continue
        try:
            name = c.get_name() or ""
        except Exception:
            name = ""
        if "SYSDASH" in name.upper():
            try:
                ext = Atspi.Rect()
                c.query_component().get_extents(Atspi.CoordType.SCREEN, ext)
                found.append((name, ext.x, ext.y, ext.width, ext.height))
            except Exception as e:
                found.append((name, -1, -1, -1, str(e)))
        walk(c)

for i in range(d.get_child_count()):
    app = d.get_child_at_index(i)
    if app is None:
        continue
    try:
        if app.get_name() and "firefox" in app.get_name().lower():
            walk(app)
    except Exception:
        pass

if not found:
    print("KEIN SYSDASH-FENSTER VORHANDEN")
else:
    for name, x, y, w, h in found:
        disp = "ACER-Display (2.)" if x >= 3440 else "HAUPT-DISPLAY (1.)"
        print(f"FENSTER {name!r}: x={x} y={y} {w}x{h} -> {disp}")
