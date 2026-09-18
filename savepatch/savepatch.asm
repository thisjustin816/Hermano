; =====================================================================
; Hermano - battery save patch
;
; Injected into the free tail of ROM bank 0 ($3489-$3FFF in the stock
; ROM, all $FF padding). Bank 0 is permanently mapped, so the hooks are
; reachable no matter which bank the game has paged in.
;
; Nothing in the engine's code is overwritten. Instead, three entries of
; the state function table that ZGB's InitStates builds in WRAM are
; repointed at wrappers here:
;
;   startFuncs[StateMenu]  -> hook_menu_start    (title: build the prompt)
;   updateFuncs[StateMenu] -> hook_menu_update   (title: show it, B loads)
;   updateFuncs[StateGame] -> hook_game_update   (gameplay: autosave)
;
; Each wrapper calls the stock function first - the dispatcher has
; already paged in that state's bank, so a plain call reaches it - and
; then adds its own behaviour.
;
; The gameplay hook sits on the update dispatch rather than the state
; START dispatch, deliberately. The update loop is vblank-synchronised,
; so extra work there is absorbed; the START path runs with the display
; off and ends in DISPLAY_ON, where even a bare call/ret shifts the
; raster phase and moves the game's mid-frame sprite toggle by a
; scanline. hook_menu_start accepts that cost because it needs the
; display-off window to write VRAM, and only pays it when entering the
; title screen, under the fade-in.
; =====================================================================

; ---- engine globals (recovered from the ROM) ----
KEYS          = $CCCB           ; ZGB keys        (this frame)
PREV_KEYS     = $CCCA           ; ZGB previous_keys
TUTORIAL      = $CC11           ; StateMenu: 0 = title art, 1 = tutorial page
FRAME_TICK    = $C0A3           ; free-running, +1 per frame; read-only here
LEVEL         = $C11C           ; stage within the world, 0-based
VIDAS         = $C11E           ; lives, as shown on the stage banner
MUNDO         = $C126           ; world, 0-based
SETSTATE      = $0AB0           ; void SetState(UINT8 state) - arg in A
MENU_START    = $7EF7           ; stock Start_StateMenu,  in ROM bank $07
MENU_UPDATE   = $7FA6           ; stock Update_StateMenu, in ROM bank $07
GAME_UPDATE   = $7FFC           ; stock Update_StateGame, in ROM bank $1C

ST_TITULO     = 1               ; StateTituloNivel - the "STAGE x-y" banner

J_B           = $20             ; GBDK joypad bit for the B button

; ---- the continue prompt ----
; The title screen is a dense full-screen illustration with no blank row
; to put a second line of text on, so the prompt shares the line that
; already reads "SELECT:CREDITS", alternating with it about once a
; second. It is set in the game's own 3x5 typeface, lifted pixel for
; pixel out of those very tiles, at the same size and position.
;
; The stock line lives in background tiles $8C-$93 (row 15, columns
; 6-13). Those are left untouched; the prompt gets eight fresh tiles, so
; alternating is just eight tilemap bytes and the original art is never
; overwritten. BG tile data is at $8800 with signed addressing (LCDC bit
; 4 clear), so tile $9C lands at $89C0 - the start of 1600 bytes of VRAM
; that is zero on this screen and clear of every sprite tile it uses
; (those stop at $87FF).
LINE_TILES    = 8               ; tilemap cells the line occupies
LINE_BYTES    = 144             ; (LINE_TILES + 1) * 16; patch.py checks this
LINE_VRAM     = $89C0           ; where tile $9C lives under signed addressing
LINE_MAP      = $99E6           ; $9800 + 15*32 + 6 -> row 15, column 6
LINE_FIRST    = $9C             ; first tile of "B:CONTINUE"
ORIG_FIRST    = $8C             ; first tile of the stock "SELECT:CREDITS"
BLANK_TILE    = LINE_FIRST+LINE_TILES   ; the line's plain background

; The two messages swap on bit 6 of the frame counter, so each gets 64
; frames, and the line is held blank for the first 8 of those. A real
; cross-fade is not available: DMG has one background palette for the
; whole screen, so fading this line would fade the entire title art. The
; short gap is what makes the change read as deliberate rather than as
; one string glitching into another.
BLINK_BIT     = $40             ; which message
BLINK_GAP     = $38             ; zero for the first 8 frames of each message

; ---- save block layout in cartridge SRAM ----
SRAM_MAGIC    = $A000           ; 5 bytes
SRAM_VER      = $A005
SRAM_LEN      = $A006
SRAM_SUM      = $A007
SRAM_DATA     = $A008
PAYLOAD_LEN   = 19
SAVE_VERSION  = 2               ; v1 stored the checkpoint too; see var_table

; Offsets of individual variables inside the payload. They follow from
; var_table at the bottom of this file; patch.py re-derives them from
; that table and refuses to build if these disagree.
SRAM_LEVEL    = SRAM_DATA+11
SRAM_VIDAS    = SRAM_DATA+13
SRAM_MUNDO    = SRAM_DATA+16

; =====================================================================
; Hook A - installed as updateFuncs[StateGame].
;
; Autosaves whenever the run's identity changes: a new stage, a new
; world, or a change in lives (a death, or a 1-up). The comparison is
; made against the copy already in SRAM, so no scratch RAM is needed and
; an unchanged frame costs a handful of instructions and no SRAM write.
;
; Note what this means for a fresh run: pressing START does not destroy
; an existing save. It survives the title screen, the tutorial page and
; the whole stage banner, and is only overwritten on the first frame of
; actual play.
; =====================================================================
hook_game_update:
        call GAME_UPDATE        ; stock Update_StateGame (bank $1C, already paged in)
        call sram_on
        ld a,(VIDAS)
        ld hl,SRAM_VIDAS
        cp (hl)
        jr nz,hgu_changed
        ld a,(LEVEL)
        ld hl,SRAM_LEVEL
        cp (hl)
        jr nz,hgu_changed
        ld a,(MUNDO)
        ld hl,SRAM_MUNDO
        cp (hl)
        jr z,hgu_done           ; nothing worth writing
hgu_changed:
        ld a,(VIDAS)
        or a
        jr z,hgu_done           ; never checkpoint a run that is already over
        jr save_block
hgu_done:
        jp sram_off

; =====================================================================
; Hook B - installed as startFuncs[StateMenu].
;
; Builds the prompt's tiles, but only when there is a save worth
; advertising. main() holds the display off for the whole state START
; path (DISPLAY_ON comes after START returns), and Start_StateMenu never
; touches LCDC, so VRAM is freely writable here with no vblank juggling.
; Only the tile data is written; the tilemap is left alone, so a screen
; with no save behind it is the stock artwork, untouched.
; =====================================================================
hook_menu_start:
        call MENU_START         ; stock Start_StateMenu (bank $07, already paged in)
        call sram_on
        call save_valid
        jr nz,hms_done          ; nothing to continue from
        ld hl,line_tiles
        ld de,LINE_VRAM
        ld b,LINE_BYTES
hms_copy:
        ld a,(hl+)
        ld (de),a
        inc de
        dec b
        jr nz,hms_copy
hms_done:
        jp sram_off

; =====================================================================
; Hook C - installed as updateFuncs[StateMenu].
;
; Alternates the bottom line between the stock "SELECT:CREDITS" and
; "B:CONTINUE", and loads the save when B is pressed. Both are gated on
; the save validating, so with no save the line never changes and B does
; nothing. The tutorial page reuses this state and redraws the whole
; background, so leave its tilemap alone.
; =====================================================================
hook_menu_update:
        call MENU_UPDATE        ; stock Update_StateMenu (bank $07, already paged in)
        call sram_on
        call save_valid
        jr nz,hmu_done          ; no save: stock behaviour in full
        call sram_off
        ld a,(TUTORIAL)
        or a
        jr nz,hmu_keys          ; tutorial page showing - do not touch its map
        ld a,(FRAME_TICK)
        and BLINK_GAP
        jr z,hmu_blank          ; brief pause between the two messages
        ld a,(FRAME_TICK)
        and BLINK_BIT
        ld c,1                  ; each message is a run of consecutive tiles
        ld a,LINE_FIRST
        jr nz,hmu_draw          ; ld does not disturb the flags from AND
        ld a,ORIG_FIRST
        jr hmu_draw
hmu_blank:
        ld c,0                  ; the one background tile, eight times over
        ld a,BLANK_TILE
hmu_draw:
        ld de,LINE_MAP
        ld b,LINE_TILES
hmu_cell:
        ld (de),a
        inc de
        add a,c
        dec b
        jr nz,hmu_cell
hmu_keys:
        ld a,(KEYS)
        and J_B
        ret z                   ; B not held
        ld a,(PREV_KEYS)
        and J_B
        ret nz                  ; B was already held last frame - not a fresh press
        jp load_game
hmu_done:
        jp sram_off

; =====================================================================
; save_block - write payload, magic and checksum. Entered with SRAM
; already open; closes it again on the way out.
; =====================================================================
save_block:
        call vars_to_sram
        ld hl,magic
        ld de,SRAM_MAGIC
        ld b,5
sb_magic:
        ld a,(hl+)
        ld (de),a
        inc de
        dec b
        jr nz,sb_magic
        ld a,SAVE_VERSION
        ld (SRAM_VER),a
        ld a,PAYLOAD_LEN
        ld (SRAM_LEN),a
        call checksum
        ld (SRAM_SUM),a
        jp sram_off

; =====================================================================
; load_game - validate SRAM, restore the progress block, enter the
; stage banner state. Leaves everything untouched if the save is bad.
; =====================================================================
load_game:
        call sram_on
        call save_valid
        jr nz,lg_bad
        call sram_to_vars
        call sram_off
        ld a,ST_TITULO
        jp SETSTATE             ; tail call: state_running=0, next_state=1
lg_bad:
        jp sram_off

; Returns with Z set if SRAM holds a save this build can use. Assumes SRAM
; is already open. Clobbers A, B, DE, HL.
save_valid:
        ld hl,magic
        ld de,SRAM_MAGIC
        ld b,5
sv_magic:
        ld a,(de)
        cp (hl)
        ret nz
        inc hl
        inc de
        dec b
        jr nz,sv_magic
        ld a,(SRAM_VER)
        cp SAVE_VERSION
        ret nz
        ld a,(SRAM_LEN)
        cp PAYLOAD_LEN
        ret nz
        call checksum
        ld hl,SRAM_SUM
        cp (hl)                 ; Z set here means the checksum agrees
        ret

; =====================================================================
; Progress block copy helpers.
; var_table is a list of {address, length} runs, terminated by length 0.
; BC walks the table, HL the game variables, DE the SRAM payload.
; Both assume SRAM is already open.
; =====================================================================
vars_to_sram:
        ld bc,var_table
        ld de,SRAM_DATA
vts_next:
        ld a,(bc)
        ld l,a
        inc bc
        ld a,(bc)
        ld h,a
        inc bc
        ld a,(bc)
        inc bc
        or a
        ret z                   ; length 0 terminates the table
vts_byte:
        push af                 ; A doubles as the per-run byte counter
        ld a,(hl+)
        ld (de),a
        inc de
        pop af
        dec a
        jr nz,vts_byte
        jr vts_next

sram_to_vars:
        ld bc,var_table
        ld de,SRAM_DATA
stv_next:
        ld a,(bc)
        ld l,a
        inc bc
        ld a,(bc)
        ld h,a
        inc bc
        ld a,(bc)
        inc bc
        or a
        ret z
stv_byte:
        push af
        ld a,(de)
        ld (hl+),a
        inc de
        pop af
        dec a
        jr nz,stv_byte
        jr stv_next

; 8-bit additive checksum over the SRAM payload, result in A
checksum:
        ld hl,SRAM_DATA
        ld b,PAYLOAD_LEN
        xor a
cs_loop:
        add a,(hl)
        inc hl
        dec b
        jr nz,cs_loop
        ret

; ---- MBC5 SRAM gating. Kept closed except for the brief save/load, so
; ---- a crash or stray write cannot corrupt the battery-backed block.
sram_on:
        ld a,$0A
        ld ($0000),a            ; RAMG: enable cartridge RAM
        xor a
        ld ($4000),a            ; RAMB: select RAM bank 0
        ret

sram_off:
        xor a
        ld ($0000),a            ; RAMG: disable cartridge RAM
        ret

magic:
        db "HRMSV"

; ---- "B:CONTINUE" set in the game's own 3x5 typeface, occupying the
; ---- same eight tiles' worth of pixels as the stock credits line, plus
; ---- a ninth tile holding just the background for the pause between
; ---- messages. C, E, T, I and the colon are the game's own letterforms,
; ---- lifted pixel for pixel out of those tiles; B, O, N and U are drawn
; ---- to match. The house rule, read off the stock letters, is that a
; ---- corner pixel is dropped wherever a stroke curves - C cuts its two
; ---- open corners, D and R cut the corners on their round side. O and U
; ---- are four pixels wide so they can follow it (cutting both corners of
; ---- a 3px row would leave a single pixel), and N is four so its
; ---- diagonal has somewhere to go. Two bytes per row, low bitplane then high: the high
; ---- plane is all ones throughout, so blank pixels read as colour 2 (the
; ---- line's background) and text pixels as colour 3 (black), exactly as
; ---- the stock tiles are drawn.
line_tiles:
; text width 40px, centred at x+12 within the 64px credits block
        db $00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF   ; tile 0
        db $00,$FF,$00,$FF,$0C,$FF,$0A,$FF,$0C,$FF,$0A,$FF,$0C,$FF,$00,$FF   ; tile 1
        db $00,$FF,$00,$FF,$0C,$FF,$91,$FF,$11,$FF,$91,$FF,$0C,$FF,$00,$FF   ; tile 2
        db $00,$FF,$00,$FF,$C9,$FF,$2D,$FF,$2B,$FF,$29,$FF,$C9,$FF,$00,$FF   ; tile 3
        db $00,$FF,$00,$FF,$75,$FF,$25,$FF,$25,$FF,$25,$FF,$25,$FF,$00,$FF   ; tile 4
        db $00,$FF,$00,$FF,$29,$FF,$A9,$FF,$69,$FF,$29,$FF,$26,$FF,$00,$FF   ; tile 5
        db $00,$FF,$00,$FF,$70,$FF,$40,$FF,$60,$FF,$40,$FF,$70,$FF,$00,$FF   ; tile 6
        db $00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF   ; tile 7
        db $00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF,$00,$FF   ; blank, for the pause between messages

; ---- The progress block: the variables StateMenu's START initialises
; ---- for a new game, minus the checkpoint. That set is by construction
; ---- the complete definition of a run, and engine globals sharing the
; ---- region (the music fade counter at $C10F in particular) are
; ---- excluded.
; ----
; ---- x_checkpoint/y_checkpoint ($C12F-$C132) are deliberately NOT
; ---- saved. StateGame's START spawns the player at the stage's default
; ---- entry point when they are zero, and StateMenu's START has already
; ---- zeroed them by the time a load runs - so restoring always resumes
; ---- at the beginning of the saved stage rather than mid-stage.
var_table:
        db $0E,$C1,1            ; $C10E
        db $10,$C1,1            ; $C110
        db $13,$C1,13           ; $C113-$C11F  (includes level $C11C, lives $C11E)
        db $22,$C1,1            ; $C122
        db $26,$C1,1            ; $C126        (world)
        db $35,$C1,2            ; $C135-$C136
        db 0,0,0                ; terminator
