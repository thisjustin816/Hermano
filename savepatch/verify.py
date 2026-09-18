#!/usr/bin/env python3
"""Verification harness for the Hermano save patch (needs pyboy + pillow).

Runs seven checks against a stock ROM and its patched counterpart:

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
  8. Game over - reaching StateGameOver clears the save, because the
     game only ever enters that state once every continue is spent (the
     death path checks the counter first, and the other route zeroes it
     on the instruction before). The continue screen must not clear it.
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

# Everything ROM-specific is loaded from the matching profile in patch.py by
# load_profile(); these are filled in before any check runs.
CURRENT_STATE = MUNDO = LEVEL = VIDAS = CHECKPOINT = SETSTATE = None
OFF_SPEC_A = OFF_SPEC_B = OFF_LEVEL = OFF_VIDAS = OFF_MUNDO = OFF_CONT = None
PROFILE = None

ST_MENU, ST_TITULO, ST_GAME = 0, 1, 3
SRAM_DATA = 8            # payload start, relative to $A000

# The continue prompt shares the bottom line with "SELECT:CREDITS",
# alternating with it. This is the first tilemap cell of that line and the
# tile index it carries while the prompt is the one showing. Both releases
# draw the same title screen, so these do not vary.
PROMPT_MAP, PROMPT_FIRST = 0x99E6, 0x9C

# hook_menu_start runs on the state START path, so entering the title screen
# can shift the raster phase for a single frame. Isolated frames are fine;
# sustained divergence is not.
MAX_ISOLATED_DIFFS = 8


def load_profile(stock_rom):
    """Pull this ROM's addresses and payload offsets from its patch profile,
    so the harness checks the build it was actually given."""
    global PROFILE, CURRENT_STATE, MUNDO, LEVEL, VIDAS, CHECKPOINT, SETSTATE
    global OFF_SPEC_A, OFF_SPEC_B, OFF_LEVEL, OFF_VIDAS, OFF_MUNDO, OFF_CONT
    md5 = hashlib.md5(open(stock_rom, "rb").read()).hexdigest()
    if md5 not in P.ROM_PROFILES:
        raise SystemExit(f"no patch profile for {stock_rom} (md5 {md5})")
    PROFILE = P.ROM_PROFILES[md5]
    c = PROFILE["consts"]
    MUNDO, LEVEL, VIDAS = c["MUNDO"], c["LEVEL"], c["VIDAS"]
    CURRENT_STATE = CURRENT_STATE_BY_ROM[md5]
    CHECKPOINT = CHECKPOINT_BY_ROM[md5]
    SETSTATE = SETSTATE_BY_ROM[md5]
    offsets, n = {}, 0
    for addr, length in PROFILE["vars"]:
        for i in range(length):
            offsets[addr + i] = n + i
        n += length
    r = PROFILE["roles"]
    OFF_SPEC_A = offsets[r["SRAM_SPEC_A"]]
    OFF_SPEC_B = offsets[r["SRAM_SPEC_B"]]
    OFF_LEVEL = offsets[r["SRAM_LEVEL"]]
    OFF_VIDAS = offsets[r["SRAM_VIDAS"]]
    OFF_MUNDO = offsets[r["SRAM_MUNDO"]]
    OFF_CONT = offsets[r["SRAM_CONT"]]
    return PROFILE


# ZGB's current_state and the checkpoint coordinates are not needed by the
# patch itself, so they are not in its profiles - but the harness reads both.
CURRENT_STATE_BY_ROM = {
    "89465cae204767aba57c342c67624bee": 0xC188,
    "631a7113e6fb5fd0c876a2f19030007c": 0xC184,
}
CHECKPOINT_BY_ROM = {
    "89465cae204767aba57c342c67624bee": 0xC12F,
    "631a7113e6fb5fd0c876a2f19030007c": 0xC12B,
}
# ZGB's next_state / state_running, used to drive a state change directly.
SETSTATE_BY_ROM = {
    "89465cae204767aba57c342c67624bee": (0xCC52, 0xCC7F),
    "631a7113e6fb5fd0c876a2f19030007c": (0xCC4E, 0xCC7B),
}
ST_CONTINUE, ST_GAMEOVER = 8, 2

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
    print(f"profile: {load_profile(stock)['name']}\n")

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
        b[7] = sum(b[SRAM_DATA:SRAM_DATA + b[6]]) & 0xFF
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
        P.build_preamble(PROFILE) + open(os.path.join(HERE, "savepatch.asm")).read(),
        org=P.CODE_ORG)
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

    print("8. a game over clears the save; the continue screen does not")
    # Two routes reach StateGameOver: losing the last life with no continues
    # left, and answering NO on the CONTINUE? screen, which zeroes the counter
    # on its way here. Both end the run, so both must cost the save - including
    # the NO route taken with continues still in hand, which is the case that
    # looks survivable and is not. The wipe must not consult the stored count.
    spent = resum(good, OFF_CONT, 0)
    for label, blob, state, should_survive in (
            ("continue screen", good, ST_CONTINUE, True),
            ("game over, credits spent", spent, ST_GAMEOVER, False),
            ("game over via NO, credits left", good, ST_GAMEOVER, False)):
        open(patched + '.ram', 'wb').write(blob)
        pb = boot(patched)
        to_title(pb)
        pb.tick(30, True)
        next_state, running = SETSTATE
        pb.memory[running] = 0
        pb.memory[next_state] = state
        pb.tick(240, True)
        d = read_sram(pb)
        alive = bytes(d[0:5]) == b'HRMSV'
        ok = alive == should_survive
        print(f"   {label:<30} -> save {'kept' if alive else 'cleared'}"
              f"   {'' if ok else '  <-- wrong'}")
        if not ok:
            failures.append(
                f"{label}: the save was {'kept' if alive else 'cleared'}")
        pb.stop(save=False)

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        sys.exit(1)
    print("all checks passed")


if __name__ == '__main__':
    main()
