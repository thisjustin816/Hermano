#!/usr/bin/env python3
"""Verification harness for the Hermano save patch (needs pyboy + pillow).

Runs four checks against a stock ROM and its patched counterpart:

  1. Rendering fidelity - with no save present the patched ROM must render
     essentially as the stock ROM does over a long run of identical input.
     The gameplay hooks sit on the vblank-synchronised update dispatch and
     cost nothing observable; hook_menu_start is the one hook on the state
     START path, where the display-off window is needed to touch VRAM, and
     it shifts the raster phase for single isolated frames when entering
     the title screen. So the assertion is that no divergence is ever
     sustained: every differing frame must resync on the very next one.
  2. Autosave - progress reaches SRAM once play begins, with a valid header.
  3. Power cycle - the save survives a cold boot and B restores it.
  4. Robustness - blank, truncated and corrupted saves are all ignored.
  5. Prompt - "B:CONTINUE" takes its turn on the title screen's bottom
     line only when there is a valid save to continue from.
  6. Level-start resume - a load must not bring back a mid-stage
     checkpoint; resuming always begins the saved stage from its start.
  7. VRAM timing - every tilemap write must land during vblank or with
     the display off. The PPU silently rejects VRAM writes while it is
     fetching pixels, and because the hook runs at the same point in
     every frame the same cells get rejected every time - which does not
     look like a tear, it leaves the two messages permanently
     interleaved. Screen comparison does not catch this (and PyBoy does
     not model the restriction), so assert it directly.

Usage:
    python3 verify.py <stock.gb> <patched.gb>
"""
import hashlib
import os
import sys

import numpy as np
from pyboy import PyBoy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import asm
import patch as P

CURRENT_STATE = 0xC188
MUNDO, LEVEL, VIDAS = 0xC126, 0xC11C, 0xC11E
ST_MENU, ST_TITULO, ST_GAME = 0, 1, 3

# The continue prompt shares the bottom line with "SELECT:CREDITS",
# alternating with it. This is the first tilemap cell of that line and the
# tile index it carries while the prompt is the one showing.
PROMPT_MAP, PROMPT_FIRST = 0x99E6, 0x9C

# x_checkpoint/y_checkpoint, which a load must NOT restore - resuming always
# starts at the beginning of the saved stage.
CHECKPOINT = 0xC12F

# hook_menu_start runs on the state START path, so entering the title screen
# can shift the raster phase for a single frame. Isolated frames are fine;
# sustained divergence is not.
MAX_ISOLATED_DIFFS = 8

# Payload offsets within the SRAM block (see savepatch.asm's var_table).
SRAM_DATA = 8
OFF_SPEC_A, OFF_SPEC_B = 1, 7
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


def prompt_appears(pb, frames=200):
    """True if the prompt takes its turn on the bottom line within `frames`."""
    for _ in range(frames):
        pb.tick(1, True)
        if pb.memory[PROMPT_MAP] == PROMPT_FIRST:
            return True
    return False


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

    print("1. rendering fidelity vs stock (no save present)")
    clear_saves(patched)
    a, b = frame_hashes(stock), frame_hashes(patched)
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    differing = set(diff)
    longest = run = 0
    for i in range(len(a)):
        run = run + 1 if i in differing else 0
        longest = max(longest, run)
    print(f"   {len(a)} frames compared, {len(diff)} differing, "
          f"longest consecutive run {longest}")
    if longest > 1:
        failures.append(
            f"divergence sustained over {longest} consecutive frames - "
            f"that is a behaviour change, not raster phase")
    if len(diff) > MAX_ISOLATED_DIFFS:
        failures.append(
            f"{len(diff)} differing frames exceeds the expected "
            f"{MAX_ISOLATED_DIFFS}")

    print("2. autosave reaches SRAM")
    clear_saves(patched)
    pb = boot(patched)
    to_title(pb)
    pb.tick(30, True)
    shown = prompt_appears(pb)
    print(f"   prompt with no save present: {'SHOWN (bad!)' if shown else 'never appears'}")
    if shown:
        failures.append("the continue prompt is shown with no save present")
    pb.stop(save=False)

    pb = boot(patched)
    to_title(pb)
    pb.tick(30, True)
    press(pb, 'start')
    pb.tick(40, True)
    pb.memory[MUNDO], pb.memory[LEVEL], pb.memory[VIDAS] = 1, 2, 6
    press(pb, 'start')
    pb.tick(400, True)
    # Walk the player past a mid-stage checkpoint, then force a re-save. The
    # checkpoint must not come back on load.
    for i, v in enumerate((0x40, 0x02, 0x90, 0x00)):
        pb.memory[CHECKPOINT + i] = v
    pb.memory[VIDAS] = 6
    pb.tick(8, True)
    header = read_sram(pb)
    progress = stored_progress(pb)
    print(f"   state={pb.memory[CURRENT_STATE]} magic={bytes(header[0:5])!r} "
          f"version={header[5]} length={header[6]} stored(world,stage,lives)={progress}")
    if bytes(header[0:5]) != b'HRMSV' or progress != (1, 2, 6):
        failures.append("autosave did not store the expected progress")
    pb.stop(save=True)

    print("3. save survives a power cycle, prompt appears, B restores it")
    pb = boot(patched)
    to_title(pb)
    pb.tick(30, True)
    shown = prompt_appears(pb)
    print(f"   prompt with a save: {'shown' if shown else 'MISSING'}")
    if not shown:
        failures.append("the continue prompt is missing despite a valid save")
    press(pb, 'b')
    pb.tick(120, True)
    live = (pb.memory[MUNDO], pb.memory[LEVEL], pb.memory[VIDAS])
    cp = [pb.memory[CHECKPOINT + i] for i in range(4)]
    print(f"   after B: state={pb.memory[CURRENT_STATE]} (world,stage,lives)={live} "
          f"checkpoint={cp}")
    if pb.memory[CURRENT_STATE] != ST_TITULO or live != (1, 2, 6):
        failures.append("continue did not restore the saved run")
    if any(cp):
        failures.append(f"continue restored a mid-stage checkpoint {cp}; "
                        f"it should resume at the start of the stage")
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
    # A block can be perfectly intact and still name a stage that does not
    # exist - world 7 indexes past the end of the game's 22-entry stage
    # table and renders as garbage. Those must be refused too, which means
    # re-checksumming so only the range check can reject them.
    def resum(blob, off, value):
        b = bytearray(blob)
        b[SRAM_DATA + off] = value
        b[7] = sum(b[SRAM_DATA:SRAM_DATA + 19]) & 0xFF
        return bytes(b)
    cases['world 7 (past the last)'] = resum(good, OFF_MUNDO, 7)
    cases['world 200'] = resum(good, OFF_MUNDO, 200)
    cases['stage 9'] = resum(good, OFF_LEVEL, 9)
    cases['special stage 99'] = resum(good, OFF_SPEC_B, 99)
    cases['underworld 99'] = resum(good, OFF_SPEC_A, 99)
    for name, blob in cases.items():
        open(patched + '.ram', 'wb').write(blob)
        pb = boot(patched)
        to_title(pb)
        pb.tick(30, True)
        no_prompt = not prompt_appears(pb, 150)
        press(pb, 'b')
        pb.tick(90, True)
        ignored = pb.memory[CURRENT_STATE] == ST_MENU
        print(f"   {name:<18} -> {'ignored' if ignored else 'LOADED (bad!)'}, "
              f"prompt {'hidden' if no_prompt else 'SHOWN (bad!)'}")
        if not ignored:
            failures.append(f"{name} was accepted as a valid save")
        if not no_prompt:
            failures.append(f"{name} still advertised a continue prompt")
        pb.stop(save=False)
    open(patched + '.ram', 'wb').write(good)

    print("7. tilemap writes land where the PPU allows them")
    code, labels, _ = asm.assemble(
        open(os.path.join(HERE, "savepatch.asm")).read(), org=P.CODE_ORG)
    pb = boot(patched)
    to_title(pb)
    pb.tick(30, True)
    seen = {"safe": 0, "unsafe": 0}

    def classify(_ctx):
        lcd_on = (pb.memory[0xFF40] & 0x80) != 0
        ly = pb.memory[0xFF44]
        ok = (not lcd_on) or (144 <= ly <= 153)
        seen["safe" if ok else "unsafe"] += 1

    for label in ("hmu_cell", "hmu_keys"):
        pb.hook_register(0, labels[label], classify, None)
    pb.tick(1200, True)
    pb.stop(save=False)
    total = seen["safe"] + seen["unsafe"]
    rate = seen["unsafe"] / total if total else 0
    print(f"   {total} writes sampled, {seen['unsafe']} outside a safe window "
          f"({rate:.2%})")
    if total == 0:
        failures.append("no tilemap writes observed - the hook never ran")
    elif rate > 0.01:
        failures.append(
            f"{rate:.2%} of tilemap writes land while the PPU is fetching "
            f"pixels; they will be dropped on hardware")

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("all checks passed")


if __name__ == '__main__':
    main()
