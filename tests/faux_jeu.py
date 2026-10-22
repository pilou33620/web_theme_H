#!/usr/bin/env python3
"""Faux jeu de test : une fenêtre plein écran qui journalise souris et clavier."""
import sys, os, time
from Xlib import X, display, XK

cfg = [a for a in sys.argv[1:] if a.startswith('--config-file=')]
assert cfg and os.path.isfile(cfg[0].split('=', 1)[1]), 'config absente'
journal = open(os.environ['FAUX_JOURNAL'], 'a', buffering=1)
d = display.Display()
s = d.screen()
fen = s.root.create_window(0, 0, s.width_in_pixels, s.height_in_pixels, 0, s.root_depth,
                           background_pixel=s.white_pixel,
                           event_mask=X.PointerMotionMask | X.ButtonPressMask | X.ButtonReleaseMask |
                                      X.KeyPressMask | X.KeyReleaseMask)
fen.map()
gc = fen.create_gc(foreground=s.black_pixel)
fen.fill_rectangle(gc, 100, 100, 300, 200)
d.flush()
journal.write('pret %dx%d\n' % (s.width_in_pixels, s.height_in_pixels))
while True:
    e = d.next_event()
    if e.type == X.MotionNotify:
        journal.write('move %d %d\n' % (e.event_x, e.event_y))
    elif e.type == X.ButtonPress:
        journal.write('down %d %d %d\n' % (e.detail, e.event_x, e.event_y))
    elif e.type == X.ButtonRelease:
        journal.write('up %d %d %d\n' % (e.detail, e.event_x, e.event_y))
    elif e.type in (X.KeyPress, X.KeyRelease):
        nom = XK.keysym_to_string(d.keycode_to_keysym(e.detail, 0)) or d.keycode_to_keysym(e.detail, 0)
        journal.write('%s %s\n' % ('keydown' if e.type == X.KeyPress else 'keyup', nom))
