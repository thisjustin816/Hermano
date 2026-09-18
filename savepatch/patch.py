#!/usr/bin/env python3
"""Patch a battery-backed save into the released Hermano ROM.

The stock cartridge is MBC5 with no RAM and no battery, so the patch has to
do three things:

  1. Change the cartridge header to MBC5 + RAM + BATTERY with one 8 KB SRAM
     bank, then repair the header and global checksums.
  2. Assemble savepatch.asm into the free padding at the end of ROM bank 0.
  3. Repoint two entries of the state function table that ZGB's InitStates
     builds at runtime, so the title screen and gameplay update functions
     run through the injected hooks.

No original code or data is overwritten.

Usage:
    python3 patch.py <input.gb> [-o <output.gb>] [--ips <patch.ips>]
"""
import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asm

HERE = os.path.dirname(os.path.abspath(__file__))

# Where the injected code lives (ROM bank 0 is permanently mapped, so the
# ROM offset and the CPU address are the same).
CODE_ORG = 0x3500
CODE_LIMIT = 0x4000

ROM_SIZE = 512 * 1024

# Everything that differs between releases lives here. Both are the same ZGB
# build pipeline and the same title screen, so the injected code is identical;
# only the engine globals, two stock entry points and the progress block move.
#
#   consts   assembly constants emitted ahead of savepatch.asm
#   vars     the progress block, as {address, length} runs. This is the set
#            StateMenu's START initialises for a new game, minus the music
#            fade counter (an engine global that shares the region) and minus
#            the checkpoint coordinates, which are left out so a restored run
#            always starts its stage from the beginning.
#   roles    which address inside `vars` each named payload offset refers to;
#            the offsets are derived, so they cannot drift out of step.
#   hooks    (lo immediate, hi immediate, stock target, label) for the three
#            InitStates entries that get repointed.
ROM_PROFILES = {
    "89465cae204767aba57c342c67624bee": {
        "name": "Hermano (original release)",
        "consts": {
            "KEYS": 0xCCCB, "PREV_KEYS": 0xCCCA, "TUTORIAL": 0xCC11,
            "FRAME_TICK": 0xC0A3,
            "LEVEL": 0xC11C, "VIDAS": 0xC11E, "MUNDO": 0xC126,
            "SETSTATE": 0x0AB0,
            "MENU_START": 0x7EF7, "MENU_UPDATE": 0x7FA6, "GAME_UPDATE": 0x7FFC,
            "GAMEOVER_START": 0x7FC6,
            "MAX_WORLD": 6, "MAX_STAGE": 2, "MAX_SPEC_B": 3, "MAX_SPEC_A": 7,
        },
        "vars": [(0xC10E, 1), (0xC110, 1), (0xC113, 13), (0xC122, 1),
                 (0xC126, 1), (0xC135, 2)],
        "roles": {"SRAM_SPEC_A": 0xC110, "SRAM_SPEC_B": 0xC118,
                  "SRAM_LEVEL": 0xC11C, "SRAM_VIDAS": 0xC11E,
                  "SRAM_MUNDO": 0xC126, "SRAM_CONT": 0xC10E},
        "hooks": [(0x6F25, 0x6F28, 0x7EF7, "hook_menu_start"),
                  (0x6F2D, 0x6F30, 0x7FA6, "hook_menu_update"),
                  (0x6F50, 0x6F53, 0x7FC6, "hook_gameover_start"),
                  (0x6F6E, 0x6F71, 0x7FFC, "hook_game_update")],
    },
    "631a7113e6fb5fd0c876a2f19030007c": {
        "name": "Hermano World (ModRetro Chromatic, SGB enhanced)",
        "consts": {
            "KEYS": 0xCCC7, "PREV_KEYS": 0xCCC6, "TUTORIAL": 0xCC0D,
            "FRAME_TICK": 0xC0A3,
            "LEVEL": 0xC118, "VIDAS": 0xC11A, "MUNDO": 0xC122,
            "SETSTATE": 0x0AB0,
            "MENU_START": 0x7EF7, "MENU_UPDATE": 0x7F97, "GAME_UPDATE": 0x7FFC,
            "GAMEOVER_START": 0x7FC6,
            "MAX_WORLD": 6, "MAX_STAGE": 2, "MAX_SPEC_B": 3, "MAX_SPEC_A": 7,
        },
        "vars": [(0xC10D, 1), (0xC10F, 1), (0xC112, 5), (0xC117, 5),
                 (0xC11E, 1), (0xC122, 1), (0xC131, 2)],
        "roles": {"SRAM_SPEC_A": 0xC10F, "SRAM_SPEC_B": 0xC117,
                  "SRAM_LEVEL": 0xC118, "SRAM_VIDAS": 0xC11A,
                  "SRAM_MUNDO": 0xC122, "SRAM_CONT": 0xC10D},
        "hooks": [(0x6F25, 0x6F28, 0x7EF7, "hook_menu_start"),
                  (0x6F2D, 0x6F30, 0x7F97, "hook_menu_update"),
                  (0x6F50, 0x6F53, 0x7FC6, "hook_gameover_start"),
                  (0x6F6E, 0x6F71, 0x7FFC, "hook_game_update")],
    },
}

SRAM_DATA = 0xA008       # must match savepatch.asm

# Cartridge header fields.
OFF_CART_TYPE = 0x147
OFF_RAM_SIZE = 0x149
OFF_HDR_SUM = 0x14D
OFF_GLOBAL_SUM = 0x14E
CART_MBC5 = 0x19                 # MBC5
CART_MBC5_RAM_BATTERY = 0x1B     # MBC5 + RAM + BATTERY
RAM_NONE = 0x00
RAM_8K = 0x02                    # one 8 KB bank


def header_checksum(rom):
    """Boot-ROM header check over $0134-$014C."""
    x = 0
    for i in range(0x134, 0x14D):
        x = (x - rom[i] - 1) & 0xFF
    return x


def global_checksum(rom):
    """16-bit sum of every byte except the two checksum bytes themselves."""
    total = sum(rom) - rom[OFF_GLOBAL_SUM] - rom[OFF_GLOBAL_SUM + 1]
    return total & 0xFFFF


MAX_IPS_RECORD = 0xFFFF
IPS_MERGE_GAP = 8            # bridge gaps this short rather than pay 5 bytes of header
IPS_EOF_OFFSET = 0x454F4F    # a record starting here would read as the "EOF" marker


def make_ips(original, patched):
    """Build an IPS patch describing original -> patched."""
    if len(patched) != len(original):
        raise SystemExit("IPS generation expects same-sized images")

    # Group differing bytes into runs, bridging short identical gaps so we do
    # not pay a record header for every isolated byte.
    runs = []
    for i in range(len(patched)):
        if patched[i] == original[i]:
            continue
        if runs and i - runs[-1][1] <= IPS_MERGE_GAP:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])

    out = bytearray(b"PATCH")
    for start, end in runs:
        if start == IPS_EOF_OFFSET:
            start -= 1       # back up one byte so the offset is encodable
        while start < end:
            size = min(end - start, MAX_IPS_RECORD)
            out += bytes([(start >> 16) & 0xFF, (start >> 8) & 0xFF, start & 0xFF])
            out += bytes([(size >> 8) & 0xFF, size & 0xFF])
            out += patched[start:start + size]
            start += size
    out += b"EOF"
    return bytes(out)


def build_preamble(profile):
    """Emit the assembly constants and progress block for one ROM.

    The payload offsets are derived from the same `vars` list that becomes
    var_table, so a reordered progress block cannot leave the range checks
    and the change detection pointing at the wrong bytes.
    """
    offsets, n = {}, 0
    for addr, length in profile["vars"]:
        for i in range(length):
            offsets[addr + i] = n + i
        n += length

    out = [f"; ==== generated from the ROM profile: {profile['name']} ====",
           f"PAYLOAD_LEN   = {n}"]
    for name, value in profile["consts"].items():
        out.append(f"{name:<13} = ${value:04X}")
    for name, addr in profile["roles"].items():
        if addr not in offsets:
            raise SystemExit(
                f"{name} refers to {addr:#06x}, which the progress block does "
                f"not cover")
        out.append(f"{name:<13} = ${SRAM_DATA + offsets[addr]:04X}   "
                   f"; {addr:#06x}, payload byte {offsets[addr]}")
    out.append("var_table:")
    for addr, length in profile["vars"]:
        out.append(f"        db ${addr & 0xFF:02X},${addr >> 8:02X},{length}"
                   f"   ; {addr:#06x}-{addr + length - 1:#06x}")
    out.append("        db 0,0,0                ; terminator")
    return "\n".join(out) + "\n\n"


def check_var_offsets(code, labels, consts):
    """Sanity-check the prompt's tile data against the constants using it."""
    # LINE_TILES tiles of text, plus one blank tile for the pause between them
    if consts["LINE_BYTES"] != (consts["LINE_TILES"] + 1) * 16:
        raise SystemExit(
            f"LINE_BYTES is {consts['LINE_BYTES']} but LINE_TILES implies "
            f"{(consts['LINE_TILES'] + 1) * 16}"
        )
    tile_bytes = labels["line_tiles_end"] - labels["line_tiles"]
    if tile_bytes != consts["LINE_BYTES"]:
        raise SystemExit(
            f"line_tiles holds {tile_bytes} bytes but LINE_BYTES is "
            f"{consts['LINE_BYTES']}"
        )


def patch(rom_bytes, verbose=True):
    rom = bytearray(rom_bytes)

    md5 = hashlib.md5(rom_bytes).hexdigest()
    profile = ROM_PROFILES.get(md5)
    if profile is None:
        known = "\n".join(f"  {m}  {p['name']}" for m, p in ROM_PROFILES.items())
        raise SystemExit(
            f"unrecognised ROM (md5 {md5}).\n"
            f"Every address this patch uses was recovered from a specific "
            f"build, so it cannot be applied blind. Known ROMs:\n{known}")
    if verbose:
        print(f"ROM: {profile['name']}")

    if len(rom) != ROM_SIZE:
        raise SystemExit(f"expected a {ROM_SIZE}-byte ROM, got {len(rom)}")
    if rom[OFF_CART_TYPE] == CART_MBC5_RAM_BATTERY:
        raise SystemExit("this ROM already has SRAM enabled - already patched?")
    if rom[OFF_CART_TYPE] != CART_MBC5:
        raise SystemExit(
            f"unexpected cartridge type {rom[OFF_CART_TYPE]:#04x}; "
            f"this patch targets MBC5 ({CART_MBC5:#04x})"
        )

    # --- 1. assemble the injected code -----------------------------------
    with open(os.path.join(HERE, "savepatch.asm")) as f:
        source = build_preamble(profile) + f.read()
    code, labels, consts = asm.assemble(source, org=CODE_ORG)
    check_var_offsets(code, labels, consts)

    end = CODE_ORG + len(code)
    if end > CODE_LIMIT:
        raise SystemExit(f"injected code overruns bank 0 ({end:#06x} > {CODE_LIMIT:#06x})")

    # The target region must be untouched $FF padding.
    if any(b != 0xFF for b in rom[CODE_ORG:end]):
        raise SystemExit(f"target region {CODE_ORG:#06x}-{end:#06x} is not free padding")

    rom[CODE_ORG:end] = code
    if verbose:
        print(f"injected {len(code)} bytes at {CODE_ORG:#06x}-{end - 1:#06x} "
              f"({CODE_LIMIT - end} bytes of bank 0 padding left)")

    # --- 2. repoint the two state update functions at our hooks ----------
    for lo_off, hi_off, stock_target, label in profile["hooks"]:
        stock_lo, stock_hi = stock_target & 0xFF, stock_target >> 8
        if (rom[lo_off], rom[hi_off]) != (stock_lo, stock_hi):
            raise SystemExit(
                f"InitStates does not look as expected at {lo_off:#06x}: "
                f"{rom[lo_off]:#04x}/{rom[hi_off]:#04x} "
                f"(expected {stock_lo:#04x}/{stock_hi:#04x})"
            )
        target = labels[label]
        rom[lo_off] = target & 0xFF
        rom[hi_off] = target >> 8
        if verbose:
            print(f"hooked InitStates: ${stock_target:04X} -> ${target:04X}  ({label})")

    # --- 3. give the cartridge a battery-backed RAM chip ------------------
    rom[OFF_CART_TYPE] = CART_MBC5_RAM_BATTERY
    rom[OFF_RAM_SIZE] = RAM_8K
    rom[OFF_HDR_SUM] = header_checksum(rom)
    gsum = global_checksum(rom)
    rom[OFF_GLOBAL_SUM] = gsum >> 8
    rom[OFF_GLOBAL_SUM + 1] = gsum & 0xFF
    if verbose:
        print(f"header: cart type {CART_MBC5:#04x} -> {CART_MBC5_RAM_BATTERY:#04x} "
              f"(MBC5+RAM+BATTERY), RAM size {RAM_NONE:#04x} -> {RAM_8K:#04x} (8 KB)")
        print(f"header checksum {rom[OFF_HDR_SUM]:#04x}, global checksum {gsum:#06x}")

    return bytes(rom)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rom", help="stock Hermano ROM")
    ap.add_argument("-o", "--output", help="patched ROM (default: <rom>-save.gb)")
    ap.add_argument("--ips", help="also write an IPS patch here")
    args = ap.parse_args()

    original = open(args.rom, "rb").read()
    patched = patch(original)

    out = args.output or os.path.splitext(args.rom)[0] + "-save.gb"
    with open(out, "wb") as f:
        f.write(patched)
    print(f"wrote {out}  (md5 {hashlib.md5(patched).hexdigest()})")

    if args.ips:
        ips = make_ips(original, patched)
        with open(args.ips, "wb") as f:
            f.write(ips)
        print(f"wrote {args.ips}  ({len(ips)} bytes)")


if __name__ == "__main__":
    main()
