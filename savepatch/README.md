# Hermano — battery save patch

Adds a battery-backed save to the released *Hermano* ROM. The game autosaves
your progress as you play; pressing **B** on the title screen resumes from it.

The released cartridge has nowhere to save — its header declares
`MBC5` with **no RAM and no battery**:

| Header byte | Stock | Meaning          |
|-------------|-------|------------------|
| `$0147`     | `$19` | MBC5             |
| `$0149`     | `$00` | 0 bytes of SRAM  |

So the patch has to give the cartridge a RAM chip first, then add code that
uses it.

## Using it

You need your own copy of the ROM; none is distributed here.

```
python3 patch.py Hermano.gb -o Hermano-save.gb --ips Hermano-save.ips
```

`patch.py` needs nothing but Python 3. It refuses to run if the input is not
the expected ROM, if the region it wants to write into is not free padding, or
if any of the code it hooks does not look the way it expects — it will not
produce a half-patched ROM.

`Hermano-save.ips` is an ordinary IPS patch, if you would rather apply it with
your usual tool. It contains only the injected code and header changes, no
original game data.

### Controls

| Where         | Button   | Effect                                        |
|---------------|----------|-----------------------------------------------|
| Title screen  | **B**    | Resume from the last autosave                 |
| Title screen  | START    | Start a new game (unchanged)                  |
| Title screen  | SELECT   | Credits (unchanged)                           |

The title screen shows a **`B:CONTINUE`** prompt across the face of the moon,
between the logo and the blinking `PRESS START`, but only when there is a
valid save to continue from. With no save the screen is the stock artwork,
untouched.

There is no "erase save" — START always begins a fresh run from stage 1-1, and
the next checkpoint overwrites the save.

## What gets saved, and when

The autosave fires during gameplay whenever the run reaches a checkpoint in
the game's own sense of the word:

- **finishing a stage** — the stage-complete routine increments `level`
  (`$C11C`) and clears the checkpoint coordinates;
- **reaching a new world**;
- **touching a mid-stage checkpoint** — `StateGame`'s START spawns the player
  at `x_checkpoint`/`y_checkpoint` (`$C12F`/`$C131`), falling back to the
  stage's default entry point when they are zero;
- **losing or gaining a life**.

So resuming puts you back exactly where a death would have: same stage, same
mid-stage checkpoint, same lives. It never checkpoints a run whose lives have
already hit zero, so a game over always leaves the previous checkpoint intact.

The saved block is exactly the set of variables that `StateMenu`'s START
function initialises for a new game. That set is, by construction, the
complete definition of a run: world, stage, lives, keys, bombs and checkpoint
coordinates. Engine globals that share the same WRAM region — the music fade
counter at `$C10F` in particular — are deliberately excluded.

Restoring therefore reproduces exactly the state the game builds for itself
when it respawns you at a checkpoint, which is a well-travelled code path
rather than a new one.

### SRAM layout

```
$A000  magic "HRMSV"          5 bytes
$A005  format version         1
$A006  payload length         23
$A007  payload checksum       8-bit additive
$A008  payload                23 bytes
```

A load is rejected unless the magic, version, length and checksum all agree,
so a blank or corrupted chip leaves the game untouched. Cartridge RAM is kept
disabled except during the brief save and load, so a crash or a stray write
cannot damage the block.

## How it hooks in

The patch overwrites **no original code or data**. Its entire footprint is:

| Range             | Size | What                                              |
|-------------------|------|---------------------------------------------------|
| `$0147`, `$0149`  | 2    | cartridge type → MBC5+RAM+BATTERY, 8 KB of SRAM   |
| `$014D`–`$014F`   | 3    | recomputed header and global checksums            |
| `$3500`–`$36E3`   | 484  | injected code and glyphs, into what was `$FF` padding |
| `$6F25`, `$6F28`  | 2    | `startFuncs[StateMenu]` → `hook_menu_start`       |
| `$6F2D`, `$6F30`  | 2    | `updateFuncs[StateMenu]` → `hook_menu_update`     |
| `$6F6E`, `$6F71`  | 2    | `updateFuncs[StateGame]` → `hook_game_update`     |

ZGB's `InitStates` builds its state function tables in WRAM at runtime.
Rewriting three of the pointer immediates redirects individual state functions
into wrappers in bank 0, each of which calls the stock function first and then
adds its own behaviour. Bank 0 is permanently mapped, so the wrappers are
reachable whatever the game has paged in; the dispatcher has already paged in
the state's own bank, so the stock function is reachable with a plain call.

The tail of bank 0 (`$3489`–`$3FFF`) is `$FF` linker padding in the stock ROM,
which is where the code goes. `patch.py` checks that it really is still
padding before writing.

### Why the update dispatch, mostly

The obvious hook is ZGB's state START dispatch at `$0BCB` — one call site that
catches every state transition. It works, but it is not free: the START path
runs with the display off and ends in `DISPLAY_ON`, so adding anything there
shifts the raster phase. The game's `LCD_isr` toggles sprite visibility
mid-frame to hide sprites under the HUD window, and a shifted phase moves that
toggle by a scanline. Measured over a fixed 2432-frame input run, that showed
up as a 1–4 scanline difference on 12–13 frames.

That is not a cost of doing too much work in the hook — a bare `call`/`ret`
there produces the same 12 frames. It is the cost of touching that path at all.

The update dispatch has no such problem: the update loop is vblank
synchronised, so the extra work is absorbed. The two hooks that matter during
play live there and cost nothing observable.

`hook_menu_start` is the one exception, and it pays that cost knowingly: it
needs the display-off window to write VRAM, and it only runs when entering the
title screen, under the fade-in. Hooking that single slot rather than the whole
dispatch brings the count down from 12–13 frames to **4**, every one of them an
isolated frame that resyncs on the next — which is what `verify.py` asserts, in
preference to a bare equality check that would hide the difference between
raster phase and a real behaviour change.

### Drawing the prompt

The title screen is a full-screen illustration of unique tiles (`$00`–`$9B`)
with no reusable font, so the prompt brings its own glyphs — ten 8×8 tiles, one
per character of `B:CONTINUE`.

BG tile data is at `$8800` with signed addressing (LCDC bit 4 clear), so tile
`$9C` lands at `$89C0`. On the title screen that address begins 1600 bytes of
VRAM that is entirely zero and sits above every sprite tile the screen uses
(those stop at `$87FF`), giving 100 free tile slots with no conflict. The
glyphs go over the blank face of the moon at row 9, which the stock map fills
with a solid blank tile and which clears the `PRESS START` sprite at row 11.

`main()` holds the display off for the whole state START path and
`Start_StateMenu` never touches LCDC, so the write needs no vblank timing
games. The title screen's BG map is completely static — measured at zero
changes over 240 idle frames — so writing once per menu entry is enough.

Detecting "has the run changed" would normally want a byte of scratch RAM,
which there is no safe place for. Instead the hook compares against the copy
already in SRAM, so an unchanged frame costs a handful of instructions and no
write at all.

## Files

| File            | Purpose                                                |
|-----------------|--------------------------------------------------------|
| `patch.py`      | applies the patch; writes a ROM and optionally an IPS  |
| `savepatch.asm` | the injected code, commented                           |
| `asm.py`        | small LR35902 assembler (label resolution, two passes) |
| `verify.py`     | emulator test harness (needs `pyboy`, `pillow`)        |

```
python3 verify.py Hermano.gb Hermano-save.gb
```

checks rendering fidelity against the stock ROM, that progress reaches SRAM,
that it survives a power cycle, that the prompt appears only when there is a
valid save behind it, and that blank, truncated and corrupted saves are all
rejected.

## Possible extensions

- **Multiple save slots.** There is 8 KB of SRAM and the block uses 31 bytes.
- **Saving mid-stage rather than at checkpoints.** Would mean capturing enemy
  and item state, which the current variable block does not cover.

## Notes

Verified under emulation (PyBoy). The header change is the standard way to
declare a save-capable MBC5 cartridge, so flash carts should allocate and
persist the save file normally, but this has not been tested on hardware.

The stock ROM this was built against is 512 KB, MD5
`89465cae204767aba57c342c67624bee`. `patch.py` warns if yours differs.
