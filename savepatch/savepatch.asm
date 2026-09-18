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
;   startFuncs[StateMenu]  -> hook_menu_start    (title screen: prompt)
;   updateFuncs[StateMenu] -> hook_menu_update   (title screen: B loads)
;   updateFuncs[StateGame] -> hook_game_update   (gameplay: autosave)
;
; Each wrapper calls the stock function first - the dispatcher has
; already paged in that state's bank, so a plain call reaches it - and
; then adds its own behaviour.
;
; The two hooks that run during play sit on the update dispatch rather
; than the state START dispatch, deliberately. The update loop is
; vblank-synchronised, so extra work there is absorbed; the START path
; runs with the display off and ends in DISPLAY_ON, where even a bare
; call/ret shifts the raster phase and moves the game's mid-frame sprite
; toggle by a scanline. hook_menu_start accepts that cost because it
; needs the display-off window to touch VRAM, and it only pays it when
; entering the title screen, under the fade-in.
; =====================================================================

; ---- engine globals (recovered from the ROM) ----
KEYS          = $CCCB           ; ZGB keys        (this frame)
PREV_KEYS     = $CCCA           ; ZGB previous_keys
LEVEL         = $C11C           ; stage within the world, 0-based
VIDAS         = $C11E           ; lives, as shown on the stage banner
MUNDO         = $C126           ; world, 0-based
CHECKPOINT    = $C12F           ; x_checkpoint, y_checkpoint - four bytes, two
                                ; 16-bit coords. StateGame's START spawns the
                                ; player here, falling back to the stage's
                                ; default entry point when x is zero.
SETSTATE      = $0AB0           ; void SetState(UINT8 state) - arg in A
MENU_START    = $7EF7           ; stock Start_StateMenu,  in ROM bank $07
MENU_UPDATE   = $7FA6           ; stock Update_StateMenu, in ROM bank $07
GAME_UPDATE   = $7FFC           ; stock Update_StateGame, in ROM bank $1C

; ---- the "B:CONTINUE" prompt on the title screen ----
; The title screen is a full-screen illustration of unique tiles ($00-$9B)
; with no reusable font, so the prompt brings its own glyphs. BG tile data
; is at $8800 with signed addressing (LCDC bit 4 clear), so tile $9C lands
; at $89C0 - the start of 1600 bytes of VRAM that is zero on this screen
; and below every sprite tile the title screen uses (those stop at $87FF).
; The glyphs are drawn over the blank face of the moon, on the row between
; the logo and the blinking PRESS START sprite.
PROMPT_TILES  = 10              ; one tile per character
PROMPT_BYTES  = 160             ; PROMPT_TILES * 16; patch.py checks the two agree
PROMPT_FIRST  = $9C             ; first BG tile index used
PROMPT_VRAM   = $89C0           ; where tile $9C lives under signed addressing
PROMPT_MAP    = $9925           ; $9800 + 9*32 + 5 -> row 9, column 5

ST_TITULO     = 1               ; StateTituloNivel - the "STAGE x-y" banner

J_B           = $20             ; GBDK joypad bit for the B button

; ---- save block layout in cartridge SRAM ----
SRAM_MAGIC    = $A000           ; 5 bytes
SRAM_VER      = $A005
SRAM_LEN      = $A006
SRAM_SUM      = $A007
SRAM_DATA     = $A008
PAYLOAD_LEN   = 23
SAVE_VERSION  = 1

; Offsets of individual variables inside the payload. They follow from
; var_table at the bottom of this file; patch.py re-derives them from
; that table and refuses to build if these disagree.
SRAM_LEVEL    = SRAM_DATA+11
SRAM_VIDAS    = SRAM_DATA+13
SRAM_MUNDO    = SRAM_DATA+16
SRAM_CHECKPOINT = SRAM_DATA+17  ; four bytes, matching CHECKPOINT above

; =====================================================================
; Hook A - installed as updateFuncs[StateGame].
;
; Autosaves whenever the run reaches a new checkpoint in the game's own
; sense of the word: a new stage, a new world, a change in lives (a
; death, or a 1-up), or the player touching a mid-stage checkpoint. The
; comparison is made against the copy already in SRAM, so no scratch RAM
; is needed and an unchanged frame costs a handful of instructions and
; no SRAM write.
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
        jr nz,hgu_changed
        ld hl,SRAM_CHECKPOINT
        ld de,CHECKPOINT
        ld b,4
hgu_cp:
        ld a,(de)
        cp (hl)
        jr nz,hgu_changed
        inc hl
        inc de
        dec b
        jr nz,hgu_cp
        jr hgu_done             ; nothing worth writing
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
; Draws the "B:CONTINUE" prompt, but only when there is a save worth
; advertising. main() holds the display off for the whole state START
; path (DISPLAY_ON comes after START returns), and Start_StateMenu never
; touches LCDC, so VRAM is freely writable here with no vblank juggling.
; =====================================================================
hook_menu_start:
        call MENU_START         ; stock Start_StateMenu (bank $07, already paged in)
        call sram_on
        call save_valid
        jr nz,hms_done          ; nothing to continue from - leave the art alone
        ld hl,prompt_glyphs
        ld de,PROMPT_VRAM
        ld b,PROMPT_BYTES
hms_tiles:
        ld a,(hl+)
        ld (de),a
        inc de
        dec b
        jr nz,hms_tiles
        ld de,PROMPT_MAP
        ld a,PROMPT_FIRST
        ld b,PROMPT_TILES
hms_map:
        ld (de),a
        inc de
        inc a                   ; glyphs are stored in the order they are drawn
        dec b
        jr nz,hms_map
hms_done:
        jp sram_off

; =====================================================================
; Hook C - installed as updateFuncs[StateMenu].
; On the title screen, B loads the save and jumps to the stage banner.
; =====================================================================
hook_menu_update:
        call MENU_UPDATE        ; stock Update_StateMenu (bank $07, already paged in)
        ld a,(KEYS)
        and J_B
        ret z                   ; B not held
        ld a,(PREV_KEYS)
        and J_B
        ret nz                  ; B was already held last frame - not a fresh press
        jp load_game

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

; ---- Prompt glyphs, one 8x8 tile per character of "B:CONTINUE", stored
; ---- in drawing order. 2bpp Game Boy format: two bytes per row, low then
; ---- high bitplane. Both planes carry the same mask, so the text is
; ---- colour 3 (black under the title screen's BGP) on the blank moon.
prompt_glyphs:
        db $78,$78,$44,$44,$44,$44,$78,$78,$44,$44,$44,$44,$78,$78,$00,$00   ; 'B'
        db $00,$00,$18,$18,$18,$18,$00,$00,$18,$18,$18,$18,$00,$00,$00,$00   ; ':'
        db $38,$38,$44,$44,$40,$40,$40,$40,$40,$40,$44,$44,$38,$38,$00,$00   ; 'C'
        db $38,$38,$44,$44,$44,$44,$44,$44,$44,$44,$44,$44,$38,$38,$00,$00   ; 'O'
        db $44,$44,$64,$64,$54,$54,$54,$54,$4C,$4C,$44,$44,$44,$44,$00,$00   ; 'N'
        db $7C,$7C,$10,$10,$10,$10,$10,$10,$10,$10,$10,$10,$10,$10,$00,$00   ; 'T'
        db $38,$38,$10,$10,$10,$10,$10,$10,$10,$10,$10,$10,$38,$38,$00,$00   ; 'I'
        db $44,$44,$64,$64,$54,$54,$54,$54,$4C,$4C,$44,$44,$44,$44,$00,$00   ; 'N'
        db $44,$44,$44,$44,$44,$44,$44,$44,$44,$44,$44,$44,$38,$38,$00,$00   ; 'U'
        db $7C,$7C,$40,$40,$40,$40,$78,$78,$40,$40,$40,$40,$7C,$7C,$00,$00   ; 'E'

; ---- The progress block: exactly the variables StateMenu's START
; ---- initialises for a new game, which is by definition the complete
; ---- set that defines a run. Engine globals sharing the region (the
; ---- music fade counter at $C10F in particular) are deliberately
; ---- excluded.
var_table:
        db $0E,$C1,1            ; $C10E
        db $10,$C1,1            ; $C110
        db $13,$C1,13           ; $C113-$C11F  (includes level $C11C, lives $C11E)
        db $22,$C1,1            ; $C122
        db $26,$C1,1            ; $C126        (world)
        db $2F,$C1,4            ; $C12F-$C132  (checkpoint coords)
        db $35,$C1,2            ; $C135-$C136
        db 0,0,0                ; terminator
