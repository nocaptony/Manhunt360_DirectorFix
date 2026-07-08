# Making the Director Audible: Patching Manhunt's Voice Under Xbox 360 Backwards Compatibility

A reverse-engineering writeup covering two bugs, the dead ends between them, and the byte-level fix that shipped.

---

## The auto-patcher

Makes director Starkweather's voice audible in Manhunt (original Xbox) running
under Xbox 360 backwards compatibility on a JTAG/RGH console.

The tool operates on the default.xbe inside an EXTRACTED xiso (a folder tree,
NOT a packed .iso). It performs two jobs in one pass:

  1. XPLODE  - append a new executable section (".hacks", VA 0x0042D000) to the
               XBE, replicating what XboxImageXploder v1.2 does: extend the file,
               write a section-header entry, bump the section count, and update
               the header's image/size fields. Idempotent - if the section is
               already present the step is skipped.

  2. PATCH   - apply the director-audio code patch (the working "+0x28
               command-slot discriminator" build). Five writes plus a two-byte
               revert, all displacement-verified.

WHAT THE PATCH DOES (mechanism, in brief)
  On 360 BC the game routes the director's voice to DirectSound mixbin 7
  (DSMIXBIN_XBOX_VOICE_UPLOAD, the headset-upload bin), which the BC audio layer
  silently drops - so on a real 360 the director is inaudible. Every director
  line (both reactive execution-cue speech and scripted subtitled dialogue)
  carries a universal marker: the audio command slot for that voice holds
  0xFFFFFFFF at offset +0x28, whereas every non-director sound (heartbeat,
  tutorial blips, pickups, menu clicks) holds 0x00000000 there.

  A code cave at the voice-bind function (FUN_000d79d0) reads that discriminator
  and, for director voices only, stamps a magic tag (0x5354524B, "KRTS") into the
  hardware voice object at +0x7C. A second cave at the per-frame mixbin writer
  (FUN_00013930) reads that tag: tagged voices are forced to mixbin 0 (a live
  speaker bin); everything else keeps stock mixbin 7. Result: the director is
  audible, selectively, with zero collateral and no hang.

## USAGE
    python patch_manhunt_director.py /path/to/extracted_xiso
    python patch_manhunt_director.py /path/to/default.xbe
    python patch_manhunt_director.py <target> --dry-run
    python patch_manhunt_director.py <target> --no-backup

The tool locates default.xbe automatically if given a folder. A .bak backup is
written next to the file unless --no-backup is passed.
"""

## Summary

On a real Xbox 360 running the original-Xbox *Manhunt* under backwards compatibility, the director — Lionel Starkweather — is silent. Every line he speaks, both his reactive taunts during executions and his scripted cutscene monologues, produces no audio. Subtitles render, the game plays, but the voice is gone. On original hardware and in emulators (xemu) the voice plays normally, so the fault lives in the interaction between the game's audio engine and the 360's BC audio layer.

The project resolved two distinct bugs:

1. **The audio bug** (the real one): the director's voice is routed to DirectSound mixbin 7, which the 360 BC audio layer silently discards.
2. **A self-inflicted bug** (the one that cost the most time): a stale code-cave stub, compounded by a virtual-address-versus-file-offset mapping error, produced a phantom "hang at level load" that was misattributed to roughly fifteen consecutive innocent code architectures before being isolated.

The fix is a five-write binary patch to `default.xbe` plus a two-byte revert, driven by a universal one-word discriminator that identifies director voices with zero collateral. It is now packaged as a Python auto-patcher that also performs the XBE section-addition ("xplode") step.

---

## Background

### The target

*Manhunt* (Rockstar, 2003) is an original-Xbox title. On a JTAG/RGH-modded Xbox 360 it runs through the console's software backwards-compatibility layer, which translates the original Xbox's x86 code and Direct3D/DirectSound calls to the 360's PowerPC hardware and audio stack. That translation is not perfect, and audio routing is one of the places it diverges.

### The workflow

Every hypothesis was tested two ways:

- **Static analysis in Ghidra** against a clean, non-xploded `default.xbe`. The same clean binary was used throughout; function addresses and structure offsets are identical between the clean and xploded files because the section-addition step appends data without relocating existing code.
- **Dynamic analysis in xemu with GDB** (`xemu -s`, then `target remote localhost:1234`, `set architecture i386`). Breakpoints, watchpoints, and raw memory dumps confirmed or killed each theory before it reached hardware.

Deployment to the physical 360 was over FTP (FileZilla) to the console's USB storage. Real-hardware behavior was the final arbiter, because the two bugs — the mixbin drop and the recompiler hang — do **not** reproduce in xemu. Only the 360 exhibits them.

### The address mapping

One fact governed every byte written, and getting it wrong caused the second bug:

- **`.text` code:** file offset = virtual address − `0x10000`. (VA `0x13B0F` → file `0x3B0F`; VA `0xD79D0` → file `0xC79D0`.)
- **Absolute data operands** (`DAT_xxxxxxxx`): the operand value equals the VA, written little-endian as-is.
- **ECX-relative and stack-relative offsets:** unchanged.

Every jump displacement was computed by machine (Python), never by hand.

---

## Bug 1 — The mixbin-7 headset-channel drop

### The audio engine

*Manhunt*'s voice system routes each active voice through a set of DirectSound "mixbins" — output buses that feed the final mix. On the original Xbox, mixbin 7 is `DSMIXBIN_XBOX_VOICE_UPLOAD`: the bus that carries voice audio to a connected Xbox Communicator headset for upload. It is a real, functioning bin on original hardware.

On the 360 BC layer, that bin has no meaning — there is no Communicator upload path — and audio assigned to it is **silently dropped**. Nothing errors; the sound simply never reaches the speakers.

The per-frame function `FUN_00013930` writes each voice's slot-2 mixbin. At VA `0x13B0F` it executes:

```
MOV EAX, 0x7        ; B8 07 00 00 00
```

and that `7` flows to the slot-2 mixbin write a few instructions later at `0x13B2C`. `EAX = 0` yields a live (audible) bin; `EAX = 7` yields the dropped bin. This single hardcoded immediate is the mechanism behind every mixbin-voice-system sound that goes missing on BC — including the director.

### Two classes of director audio

The director does not speak through a single code path. He has two, and this distinction shaped the entire investigation:

1. **Reactive execution-cue speech** — the line he delivers the instant you execute a hunter. Short, unscripted, no on-screen caption tied to a named cue.
2. **Scripted subtitled dialogue** — the opening-cutscene monologue and the lines he repeats after executions. Each is tied to a named cue string ("BRN3A", "BRN1A", …) and a subtitle.

Early work solved the reactive class. The scripted class remained silent, and closing that gap consumed most of the project — first down a wrong path, then to the unifying discriminator.

---

## Approaches to Bug 1 — the discriminator evolution

The core problem was never "how do we set mixbin 0." That is one byte. The problem was **which voices to set it on**. Forcing every voice to mixbin 0 ("everything-loud") makes the director audible but also reroutes every other sound in the game. A discriminator was required: a test, evaluated at runtime, that says "this voice is the director" and nothing else.

Four discriminators were tried.

### Approach 1 — buffer-write to the voice's mixbin topology (failed)

The first idea wrote directly into the voice's mixbin-topology buffer at `[[voice+0x10]+4]+0x10`, setting slot-2 to 0 for the director voice at bind time.

**Why it failed:** `[voice+0x10]` is NULL at the moment the voice binds. The dereference chain does not yet exist. The write either faulted or hit garbage. The topology buffer is populated *after* bind, not before.

### Approach 2 — bank-comparison discriminator (partial success, then failure)

The reactive director samples are loaded into dedicated sample banks. The audio manager holds the "ready" bank IDs at fixed offsets `[ECX+0x27C]` and `[ECX+0x280]`. Comparing the incoming sample ID against those banks identifies reactive director voices.

This **worked for the reactive class** and produced the first audible director taunts. Combined with a tag mechanism (below), it shipped as the first working patch.

**Why it failed for the scripted class:** the scripted dialogue samples (IDs `0x20c`–`0x20f`) are **not** loaded into those banks. Captured at bind, all director banks read `0x3713` (the empty sentinel) during scripted speech. The bank comparison had nothing to match against. The scripted lines sailed through as unmatched and stayed on mixbin 7.

### Approach 3 — the `[voice+0x7C]` tag-and-read split

Rather than making the routing decision inside the per-frame mixbin writer (a hot function, hostile to added logic), the decision was split into two halves connected by a tag:

- At **bind time** (`FUN_000d79d0`), a "tag-cave" evaluates the discriminator and, for director voices, stamps a magic value into the hardware voice object at offset `+0x7C`.
- At **mix time** (`FUN_00013930`), a "read-cave" reads `[voice+0x7C]`: if it holds the magic, force mixbin 0; otherwise leave mixbin 7.

The magic is `0x5354524B` — the ASCII bytes `KRTS`, "STRK" (Starkweather) little-endian. The tag persists on the voice until the slot is recycled, and every bind overwrites it (magic for a director, zero for anything else), so recycled slots never carry a stale tag.

This architecture is sound and is what ultimately shipped. But with the **bank-comparison** discriminator feeding it, it still only tagged the reactive class.

### Approach 4 — the false trail into the subtitle system

To find where the scripted dialogue's audio was routed, the investigation followed the scripted-line dispatch chain: `FUN_000cad60` (director dispatcher) → `FUN_000c40c0` (builds the filename "BRN3A2" from cue + digit) → `FUN_00160b30` → `FUN_00168bb0`.

Substantial effort went into patching `FUN_00168bb0`, which contained a `PUSH 7` feeding a `FUN_000d1800(7, -1.0)` mixbin call — an apparently perfect second instance of the mixbin-7 bug. The immediate was patched `7 → 0`. **No effect.** Breakpoints then showed the `PUSH 7` did not even execute during the director's speech; it fired only *after* the line ended.

Decompiling the chain end-to-end revealed why: **it is the subtitle system, not the audio system.**

- `FUN_00160b30` is a subtitle-string table lookup, not a file open. The "file handles" it returned were subtitle-string buffer pointers.
- `FUN_00168bb0`'s `param_2` is the caption text (UTF-16 "These street", "and they're", …), and its `param_1` is the on-screen overlay object the caption renders into. Two "channels" surfaced here — `0x3f1f40` for director captions, `0x3eeb70` for tutorial captions — and considerable time went into treating them as DirectSound channels and dumping their fields for a mixbin difference. They are **text overlays**. The confirming decompilation was `FUN_001678d0`, a frontend/overlay initializer ("m_textOverlay.Create()...", "CreateCamera()…") that references `0x3f1f40` as a subtitle context.
- `FUN_00168a80` tokenizes the caption on spaces and resolves each *word*; `FUN_00167f90`/`FUN_00168050` are caption-word display queues. None of it touches audio.

The entire `FUN_00168xxx` subsystem is subtitle rendering that runs in parallel with the audio, dispatched from the same trigger. The audio for scripted dialogue never entered any function examined on this trail. This was the single largest dead end in the project.

### Approach 5 — the universal `[command_slot+0x28]` discriminator (the fix)

Backing out of the subtitle trail, the scripted audio was traced to its real path: it stages through `FUN_000cf8f0` into the *same* mixbin voice system as the reactive line, binds through the *same* `FUN_000d79d0`, and reaches the *same* `FUN_00013930`. Both director classes were always going through one bind function. The only failure was the discriminator feeding the tag-cave.

Dumping the full voice command slot at bind, for director lines and for every non-director sound interleaved (heartbeat, tutorial blips, pickups), exposed a clean, universal marker:

```
Director line   (sample 0x20d):  [command_slot+0x28] = 0xFFFFFFFF
Heartbeat       (sample 0x130):  [command_slot+0x28] = 0x00000000
Tutorial blip   (sample 0x7a):   [command_slot+0x28] = 0x00000000
Menu button:                     [command_slot+0x28] = 0x00000000
```

**`[command_slot+0x28] == 0xFFFFFFFF` for every director line — both classes — and `0x00000000` for everything else.** Confirmed across the reactive execution cue, the scripted dialogue, the health heartbeat, tutorial sounds, item pickups, and menu button clicks. Deterministic, universal, zero collateral.

The command slot address is computed from the voice index available at `FUN_000d79d0` entry:

```
command_slot = voice_index * 0x80 + 0x8F94 + 0x30BE98
             = voice_index * 0x80 + 0x314E2C
```

(Note the command-manager base `0x30BE98` is distinct from the voice-array manager base `0x31BEE0` — a point that itself cost a round of confusion when a field read against the wrong base returned nonsense.)

Rebuilding the tag-cave around this single test replaced the bank comparison and, in one change, covered both director classes. This is the shipped discriminator.

---

## The split-host code-cave architecture

Where the caves live is not incidental — it is dictated by a hard constraint of the 360 recompiler, and violating it was one of the ways the project generated phantom hangs.

### The `.hacks` recompiler constraint

Added code needs somewhere to live. The standard approach adds a new section ("xplode") to the XBE — here `.hacks` at VA `0x0042D000` — giving unlimited contiguous space. But testing established a rule the hard way:

> **Any instruction with a memory operand, executed inside the `.hacks` section at update frequency, hangs on real 360 BC.** Register-immediate-only code in `.hacks` runs fine. Memory-operand code hosted in the *original* `.text` section (which the recompiler holds resident-translated) runs fine.

So a cave that needs to read memory (`MOV EAX, [ECX+0x7C]`) cannot do that read from `.hacks`. But `.text` has no large contiguous free regions — the padding scan of the entire `.text` range found nothing longer than 15 bytes preceded by a terminator.

### The split

The read is therefore split from the logic:

- **Stub in `.text`** — a dead RET-stub cell (see below) hosts `MOV EAX, [ECX+0x7C]; JMP read-cave`. The memory load runs in resident-translated `.text`.
- **Read-cave in `.hacks`** — pure register-immediate: `CMP EAX, MAGIC; JNE +7; MOV EAX,0; JMP +5; MOV EAX,7; JMP 0x13B14`. No memory operand, no stall.

The tag-cave (74 bytes, more than any 15-byte `.text` gap holds) is hosted contiguously in a **dead `_atexit` RET-stub field** at VA `0x21A2B1`–`0x21A44F`. This region is a table of one-byte `C3` (RET) stubs, each followed by 15 bytes of `0x90` padding, registered as process-exit destructors that never run during gameplay. The tag-cave overwrites the padding and the dead `C3` boundaries as continuous code. The auto-patcher verifies this region actually holds `C3` + `90`×15 before writing — because writing it over live code is exactly how the project once corrupted the binary.

---

## Bug 2 — The phantom hang

This is the bug that did not exist in the game. We created it, then chased it through fifteen innocent suspects.

### The symptom

Across nearly every architecture tried — read-cave with stub, read-cave with a data cell, read-cave with an ECX-relative load, buffer-write tag-cave — the game hung at level load on real hardware. Each hang was attributed to whatever code architecture was current, and each architecture was rewritten to avoid the supposed cause. The hang persisted through all of them.

### Root cause A — the stale stub

An early architecture placed a code-cave stub at VA `0x21B9B1`, in the `.text` terminal padding: `MOV EAX,[ECX+0x7C]; JMP 0x42D005`. When that architecture was abandoned, **the stub was never cleared.**

That padding is not dead. A native code path reaches `0x21B9B1` during level load and falls through it. The stub executed on every load, jumping to `0x42D005` inside `.hacks`:

- In "everything-loud" test configurations, `0x42D005` happened to hold a valid `JMP` (the tail of a cave), so the stub landed on a real instruction and recovered. **The game ran.**
- In configurations where `0x42D005` was zeroed or held incompatible bytes, the stub landed on garbage and executed into the void. **The game hung.**

Every "hang at level load" traced to this single stale stub. The correlation that finally broke it: the hang tracked whether `0x42D005` held a valid landing, which had nothing to do with any tag-cave or read-cave being debugged. Clearing `0x20B9B1`–`0x20B9BF` to padding removed the hang entirely.

### Root cause B — the VA-versus-file-offset mapping error

Compounding the stale stub, several bind-path redirects were written at the **wrong file offset**. The `.text` mapping is file = VA − `0x10000`. Redirects intended for VA `0xD79D0` and `0xD089E` (file `0xC79D0` / `0xC089E`) were instead written at file `0xD79D0` / `0xD089E` — which correspond to VA `0xE79D0` / `0xE089E`, **unrelated live code.** This silently corrupted whatever functions lived there and produced additional phantom hangs stacked on top of the stub's.

### Why it was so hard to isolate

Three factors:

- **Neither bug reproduces in xemu.** Every hang was a hardware-only event, slow to test.
- **The "working" configurations were accidentally working** — the stub recovered by luck when its landing held a valid jump, so a genuinely-broken file appeared fine and a genuinely-fixed file appeared broken, inverting the usual signal.
- **The Ghidra base was a clean XBE while the deployed file was xploded and accreting edits.** Byte comparisons at a given "address" sometimes compared a file offset in one file against a virtual address in the other, manufacturing contradictions that sent the investigation sideways (the `0x13C00`-vs-`0x3C03` confusion is the clearest example).

The fix for Bug 2 was mechanical once isolated: clear the stale stub, and recompute every displacement and host address by machine under the correct `−0x10000` mapping. It informed a permanent discipline — never hand-compute an offset, and always verify the host region's actual bytes before writing.

---

## The final patch

Applied to a freshly-xploded `default.xbe`. All displacements machine-verified under file = VA − `0x10000` for `.text` and file `0x2FF000` = VA `0x42D000` for `.hacks`.

**Section:** `.hacks` at VA `0x42D000`, file `0x2FF000`, size `0x1000`, flags `0x06` (Preload | Executable). Header record at file `0x6B8`:
```
06 00 00 00 00 D0 42 00 00 10 00 00 00 F0 2F 00 00 10 00 00
```

**Write 1 — Tag-cave**, file `0x20A2B1` (VA `0x21A2B1`), 74 bytes, in the dead `_atexit` field:
```
8B 44 24 04 8B D0 C1 E2 07 81 C2 2C 4E 31 00 8B 52 28 83 FA FF 75 15
8B D0 83 E2 3F 8B 14 95 28 BD 31 00 C7 42 7C 4B 52 54 53 EB 13
8B D0 83 E2 3F 8B 14 95 28 BD 31 00 C7 42 7C 00 00 00 00
81 EC 88 00 00 00 E9 DB D6 EB FF
```
Reads `voice_index` from `[ESP+4]`; computes `command_slot = idx*0x80 + 0x314E2C`; reads `[slot+0x28]`; compares `0xFFFFFFFF`. On match, resolves the hardware voice `DAT_0031bde8[idx & 0x3F]` and writes MAGIC `0x5354524B` to `[voice+0x7C]`; on miss, writes `0`. Preserves ECX for `FUN_000d79d0`, replicates the displaced `SUB ESP,0x88`, and returns to `0xD79D6`.

**Write 2 — Redirect at `FUN_000d79d0` entry**, file `0xC79D0`:
```
E9 DC 28 14 00
```
Splits `SUB ESP,0x88`; the orphaned sixth byte at `0xC79D5` is harmless; the continuation `53 8B 9C 24 94` at `0xC79D6` is untouched.

**Write 3 — Stub**, file `0x20A331` (VA `0x21A331`), 8 bytes:
```
8B 41 7C E9 C7 2C 21 00          ; MOV EAX,[ECX+0x7C]; JMP 0x42D000
```

**Write 4 — Read-cave**, file `0x2FF000` (VA `0x42D000`, `.hacks`), 24 bytes, register-immediate only:
```
3D 4B 52 54 53 75 07 B8 00 00 00 00 EB 05 B8 07 00 00 00 E9 FC 6A BE FF
```
`CMP EAX,MAGIC; JNE +7; MOV EAX,0; JMP +5; MOV EAX,7; JMP 0x13B14`.

**Write 5 — Redirect at `FUN_00013930`**, file `0x3B0F` (VA `0x13B0F`):
```
E9 1D 68 20 00                   ; replaces MOV EAX,7 with JMP 0x21A331
```

**Reverts** (vestigial bytes from the abandoned streamed-mixbin experiment, restored to stock):
```
file 0x158C1C = 74 11
file 0x158C24 = 07
```

### End-to-end behavior

A director voice binds → tag-cave sees `[command_slot+0x28] == 0xFFFFFFFF` → stamps `KRTS` into `[voice+0x7C]`. Each frame, `FUN_00013930` would set slot-2 mixbin 7 → the redirect diverts through the stub → the stub loads `[voice+0x7C]` → the read-cave sees the magic → returns `EAX = 0` → the voice mixes to a live bin. A non-director voice carries no tag → read-cave returns `EAX = 7` → stock behavior, unchanged. Both director classes are audible; heartbeat, tutorial, pickups, and menu sounds are untouched; no hang.

---

## Result and residual

Both director audio classes are audible on real 360 BC hardware, selectively, with no hang and no effect on other audio.

One cosmetic residual, confirmed **not** to be a patch defect: some director lines are quieter than others. Voice-field captures showed identical parameters across loud and quiet lines (`vol1c = 0x64`, `vol34 = 0x64`, identical 3D position). The difference is authored sample amplitude — samples `0x20c`/`0x20d` were recorded quieter than `0x20e`/`0x20f` in the original game data. The patch faithfully reproduces the original relative mix. Normalizing it would override the game's authored levels and is an enhancement, not a fix.

---

## Tools used

- **Ghidra** — static disassembly and decompilation of the clean `default.xbe`; cross-reference queries (reads/writes to `DAT_` symbols); scalar searches for structure offsets (`0x8f50`, `0xcb48`) to locate the functions touching a given field; the `.text` padding scan for hostable dead space.
- **xemu + GDB** — dynamic analysis. `set architecture i386`, hardware and software breakpoints, conditional breakpoints filtering on the `+0x28` discriminator, memory dumps of voice command slots and manager structures, and the disciplined avoidance of watchpointing dynamically-allocated pool addresses (which are reassigned per run and turn deterministic tests into ghost-chasing).
- **XboxImageXploder v1.2** — the reference for the XBE section-addition transformation the auto-patcher reproduces (the `.hacks` section header at file `0x6B8`, section count `15 → 16`, image-size field updates).
- **FileZilla** — FTP deployment of the patched extracted game to the JTAG/RGH 360's USB storage.
- **Python** — every jump displacement and host-address computation (never by hand, after Bug 2), and the final `patch_manhunt_director.py` auto-patcher.
- **Gemini** — secondary assistant. Its most useful contribution was procedural: steering a capture away from a hardware watchpoint on a non-deterministic pool slot (`0x4ec588`) toward a deterministic sample-ID capture at the staging function, which is how the investigation reached the `+0x28` discriminator cleanly.

---



`patch_manhunt_director.py` performs both steps in one pass against an extracted xiso (or a direct `default.xbe`):

1. **Xplode** — appends `.hacks` (VA `0x0042D000`, file `0x2FF000`, size `0x1000`, flags `0x06`), writes the section-header record at file `0x6B8`, bumps the section count, and extends the image-size fields. Idempotent, and skippable with `--skip-xplode` if the section already exists.
2. **Patch** — applies the five writes and two reverts above.

Safety properties, all tested: a dry-run leaves the file byte-identical; a second run is a full no-op (every write detected as already applied); and the tag-cave host region is verified to hold the expected dead `C3 + 90×15` RET-stub padding before any write — if it holds anything else, the tool aborts without touching the file, which is the guard that would have prevented the Bug 2 corruption in the first place.
