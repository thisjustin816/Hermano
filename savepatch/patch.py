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

# Stock ROM fingerprint, for a clear error when fed a different dump.
STOCK_MD5 = "89465cae204767aba57c342c67624bee"
STOCK_SIZE = 512 * 1024

# InitStates builds ZGB's state function tables in WRAM at runtime with a
# sequence of `ld hl,<slot>` / `ld (hl),<lo>` / `inc hl` / `ld (hl),<hi>`.
# Rewriting those immediates repoints a single state at one of our hooks
# and leaves every other dispatch, and all engine code, untouched.
#
# Each entry: (lo immediate offset, hi immediate offset, stock target, hook)
TABLE_HOOKS = [
    (0x6F25, 0x6F28, 0x7EF7, "hook_menu_start"),   # startFuncs[StateMenu]
    (0x6F2D, 0x6F30, 0x7FA6, "hook_menu_update"),  # updateFuncs[StateMenu]
    (0x6F6E, 0x6F71, 0x7FFC, "hook_game_update"),  # updateFuncs[StateGame]
]

# Payload offsets the assembly hard-codes for its change detection; these
# are re-derived from var_table below and must agree.
EXPECTED_VAR_OFFSETS = {
    "SRAM_LEVEL": 0xC11C,
    "SRAM_VIDAS": 0xC11E,
    "SRAM_MUNDO": 0xC126,
    "SRAM_CHECKPOINT": 0xC12F,
}

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


def check_var_offsets(code, labels, consts):
    """Re-derive each variable's payload offset from var_table in the ROM
    image and confirm the constants the assembly compares against match.

    The change-detection in hook_game_update reads three fixed SRAM
    addresses. If var_table is ever reordered those constants silently
    start pointing at the wrong bytes, so tie them together here.
    """
    table = labels["var_table"] - CODE_ORG
    offset, derived = 0, {}
    while True:
        lo, hi, length = code[table], code[table + 1], code[table + 2]
        if length == 0:
            break
        base = (hi << 8) | lo
        for i in range(length):
            derived[base + i] = offset + i
        offset += length
        table += 3
    if offset != consts["PAYLOAD_LEN"]:
        raise SystemExit(
            f"var_table holds {offset} bytes but PAYLOAD_LEN is {consts['PAYLOAD_LEN']}"
        )
    if consts["PROMPT_BYTES"] != consts["PROMPT_TILES"] * 16:
        raise SystemExit(
            f"PROMPT_BYTES is {consts['PROMPT_BYTES']} but PROMPT_TILES implies "
            f"{consts['PROMPT_TILES'] * 16}"
        )
    glyph_bytes = labels["var_table"] - labels["prompt_glyphs"]
    if glyph_bytes != consts["PROMPT_BYTES"]:
        raise SystemExit(
            f"prompt_glyphs holds {glyph_bytes} bytes but PROMPT_BYTES is "
            f"{consts['PROMPT_BYTES']}"
        )
    for name, addr in EXPECTED_VAR_OFFSETS.items():
        want = consts["SRAM_DATA"] + derived[addr]
        if consts[name] != want:
            raise SystemExit(
                f"{name} is {consts[name]:#06x} but var_table puts {addr:#06x} "
                f"at {want:#06x} - fix the constant or the table"
            )


def patch(rom_bytes, verbose=True):
    rom = bytearray(rom_bytes)

    if len(rom) != STOCK_SIZE:
        raise SystemExit(f"expected a {STOCK_SIZE}-byte ROM, got {len(rom)}")
    if rom[OFF_CART_TYPE] == CART_MBC5_RAM_BATTERY:
        raise SystemExit("this ROM already has SRAM enabled - already patched?")
    if rom[OFF_CART_TYPE] != CART_MBC5:
        raise SystemExit(
            f"unexpected cartridge type {rom[OFF_CART_TYPE]:#04x}; "
            f"this patch targets MBC5 ({CART_MBC5:#04x})"
        )

    # --- 1. assemble the injected code -----------------------------------
    with open(os.path.join(HERE, "savepatch.asm")) as f:
        source = f.read()
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
    for lo_off, hi_off, stock_target, label in TABLE_HOOKS:
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
    md5 = hashlib.md5(original).hexdigest()
    if md5 != STOCK_MD5:
        print(f"warning: input md5 {md5} does not match the known release "
              f"({STOCK_MD5}); continuing anyway", file=sys.stderr)

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
