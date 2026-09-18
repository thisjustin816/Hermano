# Hermano — battery save patch

Adds a battery-backed save to the released *Hermano* ROM. The game autosaves
your progress as you play; pressing **B** on the title screen resumes from it.

Both released cartridges have nowhere to save — their headers declare
`MBC5` with **no RAM and no battery**:

| Header byte | Stock | Meaning          |
|-------------|-------|------------------|
| `$0147`     | `$19` | MBC5             |
| `$0149`     | `$00` | 0 bytes of SRAM  |

So the patch has to give the cartridge a RAM chip first, then add code that
uses it.

## Supported releases

| Release | md5 | Payload |
|---------|-----|---------|
| Hermano (original) | `89465cae204767aba57c342c67624bee` | 19 bytes |
| Hermano World (ModRetro Chromatic, SGB enhanced) | `631a7113e6fb5fd0c876a2f19030007c` | 16 bytes |

Both come off the same ZGB build and draw the same title screen, so the
injected code is identical; what differs is where the engine globals and the
progress block sit, and two stock entry points. All of that lives in
`ROM_PROFILES` in `patch.py`, keyed by md5, and the payload offsets are derived
from each profile's progress block rather than written out twice.

`patch.py` refuses a ROM it has no profile for rather than applying addresses
recovered from a different build.

Adding another release means recovering its addresses and adding one profile —
no change to the assembly.

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

The title screen's bottom line alternates between the stock `SELECT:CREDITS`
and **`B:CONTINUE`** — same typeface, same size, same position — but only when
there is a valid save to continue from. With no save the line never changes and
the screen is the stock artwork, untouched.

Each message holds for 56 frames (~0.9 s) with the line blank for 8 frames
(~0.13 s) between them. The gap is deliberate: it makes the change read as an
alternation rather than as one string glitching into another. A cross-fade is
not an option — the DMG has a single background palette for the whole screen,
so fading this line would fade the entire title illustration with it.

### Finishing the game

Beating the final stage runs the ending cutscene (~24 s), the credits (~80 s)
and a closing screen, then returns to the title — about 113 seconds in all.
None of that is gameplay, and the autosave only fires from `StateGame`, so the
save is untouched: it still names the last stage you played. `B:CONTINUE` comes
back up and drops you at the start of the final stage, so the ending can be
replayed. Nothing unlocks, and there is no new game plus.

### Game over

Spending your last continue clears the save. The next boot starts from stage
1-1, the way the game behaved before it could save at all.

This is deliberate: the game is built to feel like a cartridge-era platformer,
and a save is already a concession. Losing the run when the run is genuinely
over keeps the stakes the original had. Nothing is softened — the continue
count itself is part of the saved block, so resuming gives you the continues
you actually had left, not a fresh three.

The continue screen itself does **not** clear it — it only clears once you are
actually out of the run.

There are two ways out of that screen, and both end the run. `CONTINUE?` is not
a button prompt: you walk the boy to the `NO` or the `YES` signpost. Walking to
`YES` spends a continue and drops you back into the stage. Walking to `NO` ends
it — the game zeroes the continue counter on its way to `StateGameOver`, so
answering `NO` spends whatever you had left, save included, even if you still
had all three. That is the intent: saying `NO` is saying the run is done.

So hooking `StateGameOver` catches exactly "the run is over", by either road.

### Starting a new game over an existing save

Pressing START does **not** immediately destroy the save. It survives the title
screen, the tutorial page and the whole stage banner; it is overwritten on the
first frame of actual play in stage 1-1. So there is roughly four seconds in
which resetting still leaves the old save intact.

There is no explicit "erase save".

## What gets saved, and when

The autosave fires during gameplay whenever the run's identity changes:

- **finishing a stage** — the stage-complete routine increments `level`
  (`$C11C`);
- **reaching a new world**;
- **losing or gaining a life**.

It never checkpoints a run whose lives have already hit zero — but spending
your last continue clears the save outright, as above.

**Resuming always starts the saved stage from the beginning.** The game does
have mid-stage checkpoints — `StateGame`'s START spawns the player at
`x_checkpoint`/`y_checkpoint` (`$C12F`/`$C131`), falling back to the stage's
default entry point when they are zero — but those four bytes are deliberately
left out of the save. `StateMenu`'s START has already zeroed them by the time
a load runs, so the restored run takes the stage's default entry point.

The saved block is otherwise exactly the set of variables that `StateMenu`'s
START function initialises for a new game. That set is, by construction, the
complete definition of a run: world, stage, lives, keys and bombs. Engine
globals that share the same WRAM region — the music fade counter at `$C10F` in
particular — are deliberately excluded.

### SRAM layout

```
$A000  magic "HRMSV"          5 bytes
$A005  format version         2
$A006  payload length         19
$A007  payload checksum       8-bit additive
$A008  payload                19 bytes
```

A load is rejected unless the magic, version, length and checksum all agree,
so a blank or corrupted chip leaves the game untouched. Cartridge RAM is kept
disabled except during the brief save and load, so a crash or a stray write
cannot damage the block.

A checksum only proves the block is intact, not that it describes a stage this
game has, so the stored run is range-checked as well:

| Field | Max | Why |
|-------|-----|-----|
| world (`$C126`) | 6 | the ending fires when the world reaches 6 |
| stage (`$C11C`) | 2 | three stages per world |
| special stage (`$C118`) | 3 | its index is `19 + (n-1)` |
| underworld (`$C110`) | 7 | its index is `15 + (n-1)` |

`StateGame`'s START looks a stage up in a 22-entry table (bank `$1C`, `$7F8E`),
indexed by `world * 3 + stage` or by 15 or 19 plus one of the special-stage
selectors. Past the end of it the game loads garbage — world 7 exists as far as
the code is concerned and renders as corrupt tiles. No normal play reaches it,
since the ending triggers at world 6, but a save naming one is treated as no
save at all: no prompt, and B does nothing.

## How it hooks in

The patch overwrites **no original code or data**. Its entire footprint is:

| Range             | Size | What                                              |
|-------------------|------|---------------------------------------------------|
| `$0147`, `$0149`  | 2    | cartridge type → MBC5+RAM+BATTERY, 8 KB of SRAM   |
| `$014D`–`$014F`   | 3    | recomputed header and global checksums            |
| `$3500`–`$371F`   | 544  | injected code and tiles, into what was `$FF` padding |
| `$6F25`, `$6F28`  | 2    | `startFuncs[StateMenu]` → `hook_menu_start`       |
| `$6F2D`, `$6F30`  | 2    | `updateFuncs[StateMenu]` → `hook_menu_update`     |
| `$6F50`, `$6F53`  | 2    | `startFuncs[StateGameOver]` → `hook_gameover_start` |
| `$6F6E`, `$6F71`  | 2    | `updateFuncs[StateGame]` → `hook_game_update`     |

ZGB's `InitStates` builds its state function tables in WRAM at runtime.
Rewriting four of the pointer immediates redirects individual state functions
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

The title screen is a dense full-screen illustration; measured tile column by
tile column, no row on it has enough clear space for another line of text. So
the prompt shares the line that already reads `SELECT:CREDITS`, alternating
with it every 64 frames.

It is set in the game's own 3×5 proportional typeface, lifted pixel for pixel
out of those very tiles — `C`, `E`, `T`, `I` and the colon are the game's own
letterforms, and `B`, `O`, `N` and `U`, which the stock string does not
contain, are drawn to match. Glyphs are 3px wide on a 4px pitch, alongside the
1px-wide `:` and `I` the game itself uses.

The house rule, read off the stock letters, is that a corner pixel is dropped
wherever a stroke curves — `C` cuts its two open corners, `D` and `R` cut the
corners on their round side. `B` stays 3px, since the stock `D` is built
the same way and is 3px. `O` and `U` are 4px, because cutting both corners of a
3px row would leave a single pixel; `N` is 4px so its diagonal has somewhere to
go.

The stock line lives in background tiles `$8C`–`$93` (row 15, columns 6–13).
Those are never modified. The prompt gets eight fresh tiles at `$89C0` — tile
`$9C` under the screen's signed BG addressing, at the start of 1600 bytes of
VRAM that is zero here and clear of every sprite tile the screen uses (those
stop at `$87FF`) — plus a ninth holding just the line's background, for the
pause between messages. Alternating is then just eight tilemap bytes, so the
original art survives intact and switching back is exact.

The tile data is written by `hook_menu_start`, where `main()` holds the display
off for the whole state START path and `Start_StateMenu` never touches LCDC —
measured, all 144 of those writes happen with the LCD off, so they need no
timing care at all. Only the tilemap is touched per frame, and only while the
title art is up: the tutorial page reuses `StateMenu` and redraws the whole
background, so it is left alone.

### Writing the tilemap at the right moment

The eight tilemap cells have to change between two frames, never during one,
and that takes more than writing them together. `hook_menu_update` runs at
**LY 17–31** — the middle of active display, because the stock menu update and
`SpriteManagerUpdate` run first — where the PPU silently rejects VRAM writes
while it is fetching pixels. Since the hook runs at the same point every frame,
the *same* cells are rejected every frame: the result is not a tear that
resolves, it is a line stuck permanently half `B:CONTINUE` and half
`SELECT:CREDITS`.

So the write waits for `LY` to reach 144–150 and does all eight there, with
interrupts masked so a timer interrupt cannot stretch the burst past the end of
vblank. The window stops at 150 rather than 153 because entering at 151+ leaves
too little of vblank for eight writes. When the LCD is off the wait is skipped
— VRAM is free then, and `LY` would never advance to satisfy it.

Comparing rendered frames does not catch any of this, and PyBoy does not model
the restriction, so `verify.py` asserts it directly by hooking the write and
reading `LY`/`LCDC`. Against the build that had the bug it reports 100% of
writes unsafe; against the current one, 0.2% — the remainder being frames
around a screen transition, where a dropped write is corrected by the next
frame rather than persisting.

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
that it survives a power cycle, that the prompt takes its turn only when there
is a valid save behind it, that a load does not bring back a mid-stage
checkpoint, and that blank, truncated and corrupted saves are all rejected.

## Possible extensions

- **Multiple save slots.** There is 8 KB of SRAM and the block uses 31 bytes.
- **Resuming mid-stage.** The checkpoint coordinates are already known and
  would only need adding back to `var_table`; they are left out on purpose so
  a resumed stage always starts clean.

## Notes

Verified under emulation (PyBoy). The header change is the standard way to
declare a save-capable MBC5 cartridge, so flash carts should allocate and
persist the save file normally, but this has not been tested on hardware.

The stock ROM this was built against is 512 KB, MD5
`89465cae204767aba57c342c67624bee`. `patch.py` warns if yours differs.
