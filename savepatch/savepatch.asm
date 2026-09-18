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

; Everything this file needs that differs between releases - the engine
; globals, the stock function addresses, the progress block and its
; bounds - is emitted ahead of it by patch.py from the matching ROM
; profile. See ROM_PROFILES there.

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

LCDC_REG      = $40             ; $FF40
LY_REG        = $44             ; $FF44
; Write only in the first few lines of vblank (LY 144-150). Entering at
; 151-153 leaves too little of it, and the eight writes spill past the end
; into the next frame's pixel fetch, where the PPU rejects them.
VBL_FIRST     = 144
VBL_LAST      = 151             ; exclusive

; ---- save block layout in cartridge SRAM ----
SRAM_MAGIC    = $A000           ; 5 bytes
SRAM_VER      = $A005
SRAM_LEN      = $A006
SRAM_SUM      = $A007
SRAM_DATA     = $A008
SAVE_VERSION  = 2               ; v1 stored the checkpoint as well

; PAYLOAD_LEN, the SRAM_* payload offsets and the MAX_* bounds all come
; from the ROM profile, so the offsets cannot drift out of step with the
; progress block they index into.

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
        jp save_block           ; jp, not jr: the game-over hook sits between
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
        ; All eight cells have to change between two frames, not during
        ; one. This hook runs around LY 17-31 - the middle of active
        ; display - where the PPU rejects VRAM writes while it is fetching
        ; pixels. Since it runs at the same point every frame, the same
        ; cells are rejected every frame, so the line does not merely tear
        ; for a frame: it sits permanently interleaved, half of one
        ; message and half of the other. Wait for vblank, where VRAM is
        ; always writable, and do all eight of them there.
        ld l,a                  ; park the tile index; the wait needs A
        ld de,LINE_MAP
        ld b,LINE_TILES
hmu_vbl:
        ldh a,(LCDC_REG)
        and $80
        jr z,hmu_go             ; display off - VRAM is free, do not wait for an LY that never advances
        ldh a,(LY_REG)
        cp VBL_FIRST
        jr c,hmu_vbl            ; still drawing
        cp VBL_LAST
        jr nc,hmu_vbl           ; too late in vblank - catch the next one
hmu_go:
        ld a,l
        di                      ; a timer interrupt mid-burst can stretch these
                                ; eight writes past the end of vblank, and the
                                ; ones that land after it are dropped
hmu_cell:
        ld (de),a
        inc de
        add a,c
        dec b
        jr nz,hmu_cell
        ei
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
; Hook D - installed as startFuncs[StateGameOver].
;
; Reaching this state means the run is over, and the save goes with it -
; the next boot starts from stage 1-1, the way the game behaved before it
; could save at all.
;
; Two routes lead here and both count. Losing your last life with no
; continues left comes straight here. Answering NO on the CONTINUE?
; screen also comes here - that screen is not a button prompt, you walk
; the boy to the NO or the YES signpost - and NO zeroes the continue
; counter on the instruction before it arrives, so choosing it spends
; whatever you had left. Either way the counter reads nought by the time
; this runs, which is exactly right: saying NO is saying the run is done.
;
; Only the magic is cleared. That is enough for save_valid to reject the
; block, and the next checkpoint rewrites the whole header anyway.
; =====================================================================
hook_gameover_start:
        call GAMEOVER_START     ; stock Start_StateGameOver (bank $0E, already paged in)
        call sram_on
        xor a                   ; after sram_on, which clobbers A
        ld hl,SRAM_MAGIC
        ld b,5
hgo_wipe:
        ld (hl+),a
        dec b
        jr nz,hgo_wipe
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
        cp (hl)
        ret nz                  ; checksum disagrees
        ; fall through: the checksum only proves the block is intact, not
        ; that it describes a stage this game has

; Returns Z if the stored run names a loadable stage. Assumes SRAM is
; open. Clobbers A.
bounds_ok:
        ld a,(SRAM_MUNDO)
        cp MAX_WORLD+1
        jr nc,bo_bad
        ld a,(SRAM_LEVEL)
        cp MAX_STAGE+1
        jr nc,bo_bad
        ld a,(SRAM_SPEC_B)
        cp MAX_SPEC_B+1
        jr nc,bo_bad
        ld a,(SRAM_SPEC_A)
        cp MAX_SPEC_A+1
        jr nc,bo_bad
        xor a                   ; Z: every value is in range
        ret
bo_bad:
        ld a,$FF
        or a                    ; NZ: refuse the save
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
; ---- open corners, D and R cut the corners on their round side. B stays
; ---- three wide because the stock D is built the same way and is three.
; ---- O and U are four, since cutting both corners of a 3px row would
; ---- leave a single pixel, and N is four so its diagonal has room. Two bytes per row, low bitplane then high: the high
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
line_tiles_end:
