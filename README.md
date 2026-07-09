# Manhunt Director Audio Fix (Xbox 360 Backwards Compatibility)

Makes the director, Lionel Starkweather, audible in the original-Xbox *Manhunt*
running under Xbox 360 backwards compatibility on a JTAG/RGH console. Without the
patch his voice is silent on a real 360; with it, both his execution-cue taunts
and his scripted dialogue play, with no effect on any other audio.

---

## Patching Manhunt

Three steps: extract the disc image, xplode the executable, run the patcher.

### 1. Extract the xiso

Extract your `Manhunt.iso` to a folder with **extract-xiso**:

```
extract-xiso -x Manhunt.iso
```

This produces a directory containing `default.xbe` and the game's data files.

### 2. Xplode `default.xbe`

The patch needs a code cave that does not exist in the stock executable, so a new
section must be added first ("xploding"). Use **XboxImageXploder v1.2**
(grimdoomer's command-line tool). Its syntax is
`XboxImageXploder.exe <xbe_file> <section_name> <section_size>` — it takes the
XBE, a section name, and a size in bytes, and computes the virtual address and
file offset itself. Run:

```
XboxImageXploder.exe default.xbe .hacks 4096
```

`4096` is the section size (`0x1000`). The tool prints the placement it chose; for
the Manhunt `default.xbe` it must be exactly:

```
Section Name: .hacks
Virtual Address: 0x0042D000
Virtual Size: 0x00001000
File Offset: 0x002FF000
File Size: 0x00001000
Successfully added new section to image!
```

**Verify those values.** The patch's jump displacements are hardcoded to VA
`0x0042D000` / file `0x2FF000`, with the section flagged Preload+Executable
(`0x06`), so if XboxImageXploder reports anything different the patch will not
work. A correctly-xploded Manhunt `default.xbe` is **exactly 3072 KB
(0x300000 bytes)**; the patcher checks this and refuses anything else, so if the
size or the printed addresses are wrong, redo this step.

> The patcher does **not** xplode for you. XBE section-addition has to match
> XboxImageXploder's exact output or the 360 loader rejects the file, so that
> step is intentionally left to the proven tool.

### 3. Run the patcher

Point it at the extracted folder (or directly at the xploded `default.xbe`):

```
python patch_manhunt_director.py /path/to/extracted_manhunt
```

It validates the xploded file, applies the patch, and writes a `.bak` backup
alongside the original. Deploy the whole extracted folder to your JTAG/RGH 360
(e.g. over FTP) and launch under backwards compatibility.

---

## Usage

```
python patch_manhunt_director.py <target> [--dry-run] [--no-backup]
```

- `<target>` — an extracted xiso folder, or a path to a xploded `default.xbe`.
  Given a folder, the tool finds `default.xbe` automatically.
- `--dry-run` — run all validation and report what would be written, but change
  nothing on disk.
- `--no-backup` — do not write the `.bak` copy.

The patcher is safe to run more than once: a second run detects the patch is
already present and does nothing. Before writing anything it verifies the file is
exactly 3072 KB, that the section count is 16, that the `.hacks` section record
is present and correct (VA `0x42D000`, raw `0x2FF000`, size `0x1000`,
flags `0x06`), and that the code-cave host region and redirect sites hold the
expected bytes. If any check fails it aborts without touching the file.

---

## What was wrong, and what the patch does

Two separate problems were solved.

### Bug 1 — the director is routed to a dropped audio channel

*Manhunt*'s voice engine sends each voice to a DirectSound "mixbin" (an output
bus). The director's voice lands on **mixbin 7**, which on original hardware is
`DSMIXBIN_XBOX_VOICE_UPLOAD` — the bus that feeds an Xbox Communicator
headset. The 360 backwards-compatibility layer has no such path and **silently
discards anything on mixbin 7**. Nothing errors; the sound simply never reaches
the speakers, so the director is mute. The assignment comes from a single
hardcoded `MOV EAX, 0x7` inside the per-frame mixbin writer (`FUN_00013930` at
VA `0x13B0F`); `EAX = 0` would be a live speaker bin, `EAX = 7` is the dropped
one.

The director actually speaks through two code paths — reactive execution-cue
lines and scripted subtitled dialogue — and both are silenced by the same
mixbin-7 drop.

**The fix.** Every director voice, in both paths, carries a unique marker: its
audio command slot holds `0xFFFFFFFF` at offset `+0x28`, whereas every
non-director sound (heartbeat, tutorial blips, item pickups, menu clicks) holds
`0x00000000` there. The patch installs two code caves:

- A **tag-cave** at the voice-bind function (`FUN_000d79d0`) reads that
  `+0x28` discriminator and, for director voices only, stamps a magic value
  (`0x5354524B`, the bytes `KRTS`) into the hardware voice object at `+0x7C`.
- A **read-cave** at the per-frame mixbin writer reads that tag: tagged voices
  are forced to live mixbin 0; everything else keeps stock mixbin 7.

The result is selective — the director becomes audible while all other audio is
untouched, and there is no hang.

A code-cave detail worth noting: the 360 recompiler hangs on any memory-operand
instruction executed from the added `.hacks` section at update frequency, but
runs the same instruction fine from the original `.text` section. So the memory
read is split into a small stub hosted in `.text`, while the `.hacks` cave stays
register-immediate only.

### Bug 2 — a self-inflicted "hang at level load"

During development the game began hanging at level load on real hardware. It was
misattributed to several successive code designs before being isolated to two
mechanical causes:

- **A stale code-cave stub** left in the `.text` terminal padding from an
  abandoned design. That padding is reached by a native code path at level load;
  the orphaned stub executed every load and jumped into the `.hacks` section,
  landing on a valid instruction (game runs) or on garbage (hang) purely by luck
  of what occupied the target address. Clearing the stub removed the hang.
- **A virtual-address-versus-file-offset mapping error.** In this title,
  `.text` file offset = VA − `0x10000`. Several patch writes were made at the
  file offset numerically equal to the VA, landing `0x10000` bytes away in
  unrelated live code and corrupting it.

Neither reproduces in the xemu emulator, which is why isolation required
hardware testing. The fix was mechanical once understood: remove the stale stub,
and compute every address and jump displacement by machine under the correct
`−0x10000` mapping. The shipped patcher encodes those verified bytes directly and
refuses to write if the target file's structure does not match, so neither
failure can recur.

---

## The patch, byte for byte

Applied to the xploded `default.xbe`. `.text` file offset = VA − `0x10000`;
`.hacks` file `0x2FF000` = VA `0x42D000`.

**Write 1 — Tag-cave**, file `0x20A2B1` (VA `0x21A2B1`), 74 bytes, hosted in the
dead `_atexit` RET-stub field:
```
8B 44 24 04 8B D0 C1 E2 07 81 C2 2C 4E 31 00 8B 52 28 83 FA FF 75 15
8B D0 83 E2 3F 8B 14 95 28 BD 31 00 C7 42 7C 4B 52 54 53 EB 13
8B D0 83 E2 3F 8B 14 95 28 BD 31 00 C7 42 7C 00 00 00 00
81 EC 88 00 00 00 E9 DB D6 EB FF
```
Reads `voice_index` from `[ESP+4]`; computes `command_slot = idx*0x80 + 0x314E2C`;
reads `[slot+0x28]`; compares `0xFFFFFFFF`. On match, resolves the hardware voice
`DAT_0031bde8[idx & 0x3F]` and writes MAGIC `0x5354524B` to `[voice+0x7C]`; on
miss, writes `0`. Preserves ECX, replays the displaced `SUB ESP,0x88`, returns to
`0xD79D6`.

**Write 2 — Redirect at `FUN_000d79d0` entry**, file `0xC79D0`:
```
E9 DC 28 14 00
```
Splits `SUB ESP,0x88`; the orphaned sixth byte at `0xC79D5` is harmless; the
continuation `53 8B 9C 24 94` at `0xC79D6` is untouched.

**Write 3 — Stub**, file `0x20A331` (VA `0x21A331`), 8 bytes:
```
8B 41 7C E9 C7 2C 21 00          ; MOV EAX,[ECX+0x7C]; JMP 0x42D000
```

**Write 4 — Read-cave**, file `0x2FF000` (VA `0x42D000`, `.hacks`), 24 bytes,
register-immediate only:
```
3D 4B 52 54 53 75 07 B8 00 00 00 00 EB 05 B8 07 00 00 00 E9 FC 6A BE FF
```
`CMP EAX,MAGIC; JNE +7; MOV EAX,0; JMP +5; MOV EAX,7; JMP 0x13B14`.

**Write 5 — Redirect at `FUN_00013930`**, file `0x3B0F` (VA `0x13B0F`):
```
E9 1D 68 20 00                   ; replaces MOV EAX,7 with JMP 0x21A331
```

**Reverts** (vestigial bytes from an abandoned experiment, restored to stock):
```
file 0x158C1C = 74 11
file 0x158C24 = 07
```

---

## Known cosmetic behavior (not a bug)

Some director lines are quieter than others. Voice-field captures show identical
parameters across loud and quiet lines (volume `0x64`, identical 3D position);
the difference is authored sample amplitude in the original game data. The patch
reproduces the game's original relative mix faithfully. Normalizing it would
override the authored levels and is an enhancement, not a fix.

---

## Tools

- **extract-xiso** — unpack the disc image to a folder.
- **XboxImageXploder v1.2** — add the `.hacks` section (step 2).
- **Ghidra** — static disassembly, decompilation, cross-references, and offset
  searches against a clean `default.xbe`.
- **xemu + GDB** — dynamic analysis (`set architecture i386`, breakpoints,
  conditional filters on the `+0x28` discriminator, memory dumps). Note the two
  bugs above do not reproduce in xemu; hardware testing was required.
- **FileZilla** — FTP deployment to the JTAG/RGH 360.
- **Python** — displacement computation and this patcher.
