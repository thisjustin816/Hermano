#!/usr/bin/env python3
"""Verification harness for the Hermano save patch (needs pyboy + pillow).

Runs four checks against a stock ROM and its patched counterpart:

  1. Rendering fidelity - the patched ROM must produce byte-identical frames
     to the stock ROM over a long run of identical input. This is the check
     that drove the design: hooking ZGB's state START dispatch shifts the
     raster phase and moves the game's mid-frame sprite toggle by a scanline
     on ~1% of frames, whereas hooking the vblank-synchronised update
     dispatch costs nothing observable.
  2. Autosave - progress reaches SRAM once play begins, with a valid header.
  3. Power cycle - the save survives a cold boot and B restores it.
  4. Robustness - blank, truncated and corrupted saves are all ignored.

Usage:
    python3 verify.py <stock.gb> <patched.gb>
"""
import hashlib
import os
import sys

import numpy as np
from pyboy import PyBoy

CURRENT_STATE = 0xC188
MUNDO, LEVEL, VIDAS = 0xC126, 0xC11C, 0xC11E
ST_MENU, ST_TITULO, ST_GAME = 0, 1, 3

# Payload offsets within the SRAM block (see savepatch.asm's var_table).
SRAM_DATA = 8
OFF_LEVEL, OFF_VIDAS, OFF_MUNDO = 11, 13, 16

# A fixed input script, long enough to cover the intro, the title screen,
# the tutorial page, the stage banner and a stretch of actual play.
SCRIPT = [
    (90, None), (6, 'start'), (30, None), (6, 'start'), (40, None), (6, 'start'),
    (60, None), (6, 'start'), (90, None), (6, 'start'), (200, None), (8, 'right'),
    (30, None), (8, 'a'), (40, None), (10, 'right'), (120, None), (20, 'right'),
    (8, 'a'), (60, None), (30, 'left'), (10, 'a'), (200, None), (40, 'right'),
    (8, 'a'), (300, None),
]


def boot(rom):
    pb = PyBoy(rom, window='null', sound_emulated=False)
    pb.tick(80, True)
    return pb


def press(pb, button, hold=6, after=20):
    pb.button_press(button)
    pb.tick(hold, True)
    pb.button_release(button)
    pb.tick(after, True)


def to_title(pb):
    """Skip the intro and stop on the title screen."""
    for _ in range(40):
        press(pb, 'start', 4, 10)
        if pb.memory[CURRENT_STATE] == ST_MENU:
            return True
    return False


def read_sram(pb, n=32):
    """Peek at cartridge RAM. The patch keeps RAMG closed outside its own
    save/load, so open it here the same way the cartridge would."""
    pb.memory[0x0000] = 0x0A
    pb.memory[0x4000] = 0x00
    data = [pb.memory[0xA000 + i] for i in range(n)]
    pb.memory[0x0000] = 0x00
    return data


def stored_progress(pb):
    d = read_sram(pb)
    return d[SRAM_DATA + OFF_MUNDO], d[SRAM_DATA + OFF_LEVEL], d[SRAM_DATA + OFF_VIDAS]


def frame_hashes(rom, extra=1000):
    pb = boot(rom)
    out = []
    def snap():
        out.append(hashlib.md5(np.asarray(pb.screen.ndarray).tobytes()).hexdigest())
    for n, btn in SCRIPT:
        if btn:
            pb.button_press(btn)
        for _ in range(n):
            pb.tick(1, True)
            snap()
        if btn:
            pb.button_release(btn)
    for _ in range(extra):
        pb.tick(1, True)
        snap()
    pb.stop(save=False)
    return out


def clear_saves(rom):
    for suffix in ('.ram', '.srm', '.sav'):
        p = rom + suffix
        if os.path.exists(p):
            os.remove(p)


def main():
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    stock, patched = sys.argv[1], sys.argv[2]
    failures = []

    print("1. rendering fidelity vs stock")
    a, b = frame_hashes(stock), frame_hashes(patched)
    differing = sum(1 for x, y in zip(a, b) if x != y)
    print(f"   {len(a)} frames compared, {differing} differing")
    if differing:
        failures.append(f"{differing} frames render differently")

    print("2. autosave reaches SRAM")
    clear_saves(patched)
    pb = boot(patched)
    to_title(pb)
    pb.tick(20, True)
    press(pb, 'start')
    pb.tick(40, True)
    pb.memory[MUNDO], pb.memory[LEVEL], pb.memory[VIDAS] = 1, 2, 6
    press(pb, 'start')
    pb.tick(400, True)
    header = read_sram(pb)
    progress = stored_progress(pb)
    print(f"   state={pb.memory[CURRENT_STATE]} magic={bytes(header[0:5])!r} "
          f"version={header[5]} length={header[6]} stored(world,stage,lives)={progress}")
    if bytes(header[0:5]) != b'HRMSV' or progress != (1, 2, 6):
        failures.append("autosave did not store the expected progress")
    pb.stop(save=True)

    print("3. save survives a power cycle and B restores it")
    pb = boot(patched)
    to_title(pb)
    pb.tick(30, True)
    press(pb, 'b')
    pb.tick(120, True)
    live = (pb.memory[MUNDO], pb.memory[LEVEL], pb.memory[VIDAS])
    print(f"   after B: state={pb.memory[CURRENT_STATE]} (world,stage,lives)={live}")
    if pb.memory[CURRENT_STATE] != ST_TITULO or live != (1, 2, 6):
        failures.append("continue did not restore the saved run")
    pb.stop(save=False)

    print("4. invalid saves are ignored")
    good = open(patched + '.ram', 'rb').read()
    cases = {
        'blank 0x00': bytes(len(good)),
        'blank 0xFF': b'\xff' * len(good),
        'bad magic': b'X' + good[1:],
        'bad checksum': good[:7] + bytes([(good[7] + 1) & 0xFF]) + good[8:],
        'corrupt payload': good[:20] + bytes([good[20] ^ 0xFF]) + good[21:],
        'wrong version': good[:5] + bytes([0x7F]) + good[6:],
        'wrong length': good[:6] + bytes([0x02]) + good[7:],
    }
    for name, blob in cases.items():
        open(patched + '.ram', 'wb').write(blob)
        pb = boot(patched)
        to_title(pb)
        pb.tick(30, True)
        press(pb, 'b')
        pb.tick(90, True)
        ignored = pb.memory[CURRENT_STATE] == ST_MENU
        print(f"   {name:<18} -> {'ignored' if ignored else 'LOADED (bad!)'}")
        if not ignored:
            failures.append(f"{name} was accepted as a valid save")
        pb.stop(save=False)
    open(patched + '.ram', 'wb').write(good)

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("all checks passed")


if __name__ == '__main__':
    main()
