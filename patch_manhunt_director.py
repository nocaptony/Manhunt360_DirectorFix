#!/usr/bin/env python3
"""
patch_manhunt_director.py

Applies the director-audio patch to an ALREADY-XPLODED Manhunt default.xbe so
that Starkweather's voice is audible on Xbox 360 backwards compatibility.

This tool does NOT xplode. Xplode the XBE first with XboxImageXploder v1.2
(add a section: VA 0x0042D000, raw 0x2FF000, virtual/raw size 0x1000,
flags Preload+Executable). The correctly-xploded Manhunt default.xbe is
EXACTLY 3072 KB (0x300000 bytes); the tool refuses anything else.

WHAT THE PATCH DOES
  On 360 BC the game routes the director's voice to DirectSound mixbin 7
  (DSMIXBIN_XBOX_VOICE_UPLOAD, the headset-upload bin), which the BC audio layer
  silently drops - so the director is inaudible. Every director line (reactive
  execution-cue speech AND scripted subtitled dialogue) carries one universal
  marker: the audio command slot for that voice holds 0xFFFFFFFF at offset +0x28,
  while every non-director sound (heartbeat, tutorial blips, pickups, menu
  clicks) holds 0x00000000 there.

  A code cave at the voice-bind function (FUN_000d79d0) reads that discriminator
  and, for director voices only, stamps a magic tag (0x5354524B, "KRTS") into the
  hardware voice object at +0x7C. A second cave at the per-frame mixbin writer
  (FUN_00013930) reads the tag: tagged voices are forced to live mixbin 0,
  everything else keeps stock mixbin 7. Result: the director is audible,
  selectively, with zero collateral and no hang.

USAGE
    python patch_manhunt_director.py <extracted_xiso_folder | default.xbe>
    python patch_manhunt_director.py <target> --dry-run     # validate + report only
    python patch_manhunt_director.py <target> --no-backup   # skip the .bak copy
"""

import argparse
import os
import shutil
import struct
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

XBE_MAGIC = b"XBEH"

# A correctly-xploded Manhunt default.xbe is exactly this size. Nothing else is
# accepted: a smaller file was never xploded; a larger file is a different rip
# or a different xplode, and the hardcoded patch displacements would not fit it.
REQUIRED_SIZE = 0x300000            # 3072 KB

# .text mapping in this title: VA = file_offset + 0x10000.

# XBE image-header field offsets
OFF_BASE_ADDR     = 0x104
OFF_SECTION_COUNT = 0x11C
OFF_SECTION_HDRS  = 0x120           # VA pointer to the section-header array
SECTION_HDR_SIZE  = 0x38

# The .hacks section XboxImageXploder adds to Manhunt, and where its record sits.
HACKS_VA    = 0x0042D000
HACKS_RAW   = 0x2FF000
HACKS_SIZE  = 0x1000
HACKS_FLAGS = 0x06                  # Preload | Executable
HACKS_REC_OFF = 0x6B8               # file offset of the .hacks section-header record
EXPECTED_SECTION_COUNT = 16         # 15 stock + .hacks

# Expected fields of the .hacks record at file 0x6B8 (VA, vsize, raw, rsize).
# The flag byte at +0x00 must have Preload (bit 1) and Executable (bit 2) set;
# XboxImageXploder v1.2 writes 0x07 (Writable+Preload+Executable), older builds
# and some transcripts show 0x06 (Preload+Executable). Both are accepted.
HACKS_FLAG_REQUIRED_MASK = 0x06     # bits that MUST be set
HACKS_FLAG_ALLOWED_MASK  = 0x07     # bits that MAY be set (anything outside -> reject)
EXPECTED_HACKS_TAIL = bytes.fromhex(
    "00 D0 42 00 00 10 00 00 00 F0 2F 00 00 10 00 00".replace(" ", "")
)

# ---------------------------------------------------------------------------
# Patch write-set (the working +0x28 command-slot discriminator build).
# Every byte string and displacement is verified against:
#   VA = file + 0x10000 (.text);  .hacks file 0x2FF000 = VA 0x42D000.
# ---------------------------------------------------------------------------

def _b(h):
    return bytes.fromhex(h.replace(" ", ""))

# Write 1 - Tag-cave @ file 0x20A2B1 (VA 0x21A2B1), 74 bytes.
#   Hosted in the dead _atexit RET-stub field. Reads voice_index [ESP+4];
#   command_slot = idx*0x80 + 0x314E2C; reads [slot+0x28]; CMP 0xFFFFFFFF.
#   Match -> DAT_0031bde8[idx&0x3F], write MAGIC 0x5354524B to [+0x7C].
#   Miss -> write 0. Preserves ECX; replays SUB ESP,0x88; returns to 0xD79D6.
TAG_CAVE_OFF = 0x20A2B1
TAG_CAVE = _b(
    "8B 44 24 04 8B D0 C1 E2 07 81 C2 2C 4E 31 00 8B 52 28 83 FA FF 75 15 "
    "8B D0 83 E2 3F 8B 14 95 E8 BD 31 00 C7 42 7C 4B 52 54 53 EB 13 "
    "8B D0 83 E2 3F 8B 14 95 E8 BD 31 00 C7 42 7C 00 00 00 00 "
    "81 EC 88 00 00 00 E9 DB D6 EB FF"
)

# Write 2 - Redirect @ FUN_000d79d0 entry, file 0xC79D0 (VA 0xD79D0).
REDIR_D79D0_OFF = 0xC79D0
REDIR_D79D0 = _b("E9 DC 28 14 00")
STOCK_D79D0 = _b("81 EC 88 00 00 00")          # SUB ESP,0x88 (redirect splits it)

# Write 3 - Stub @ file 0x20A331 (VA 0x21A331): MOV EAX,[ECX+0x7C]; JMP 0x42D000.
STUB_OFF = 0x20A331
STUB = _b("8B 41 7C E9 C7 2C 21 00")

# Write 4 - Read-cave @ file 0x2FF000 (VA 0x42D000, .hacks), 24 bytes.
#   Register-immediate only. CMP EAX,MAGIC; JNE +7; EAX=0; JMP +5; EAX=7; JMP 0x13B14.
READ_CAVE_OFF = 0x2FF000
READ_CAVE = _b("3D 4B 52 54 53 75 07 B8 00 00 00 00 EB 05 B8 07 00 00 00 E9 FC 6A BE FF")

# Write 5 - Redirect @ FUN_00013930, file 0x3B0F (VA 0x13B0F): replaces MOV EAX,7.
REDIR_13B0F_OFF = 0x3B0F
REDIR_13B0F = _b("E9 1D 68 20 00")
STOCK_13B0F = _b("B8 07 00 00 00")             # MOV EAX,7

# Reverts - vestigial bytes from an abandoned experiment; restore to stock.
REVERT_158C1C_OFF = 0x158C1C
REVERT_158C1C = _b("74 11")
REVERT_158C24_OFF = 0x158C24
REVERT_158C24 = _b("07")


class PatchError(Exception):
    pass


# ---------------------------------------------------------------------------
# Locate default.xbe
# ---------------------------------------------------------------------------

def find_xbe(target):
    if os.path.isfile(target):
        return target
    if os.path.isdir(target):
        for cand in ("default.xbe", "Default.xbe", "DEFAULT.XBE"):
            p = os.path.join(target, cand)
            if os.path.isfile(p):
                return p
        hits = []
        for root, _dirs, files in os.walk(target):
            for f in files:
                if f.lower().endswith(".xbe"):
                    full = os.path.join(root, f)
                    hits.append((full[len(target):].count(os.sep), full))
        if hits:
            hits.sort()
            return hits[0][1]
    raise PatchError("Could not locate default.xbe under: %s" % target)


def rd32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


# ---------------------------------------------------------------------------
# Validation - refuse anything that is not a correctly-xploded Manhunt XBE
# ---------------------------------------------------------------------------

def validate(buf, log):
    # 1) magic
    if buf[:4] != XBE_MAGIC:
        raise PatchError("Not an XBE (missing 'XBEH' magic).")

    # 2) exact size
    if len(buf) != REQUIRED_SIZE:
        raise PatchError(
            "Wrong file size: 0x%X (%d bytes). A correctly-xploded Manhunt "
            "default.xbe is exactly 0x%X (3072 KB). If this file is smaller it "
            "was never xploded - run XboxImageXploder v1.2 first "
            "(section VA 0x0042D000, raw 0x2FF000, size 0x1000, "
            "flags Preload+Executable). If it is larger it is a different rip "
            "or a different xplode that this patch cannot target."
            % (len(buf), len(buf), REQUIRED_SIZE)
        )
    log.append("  [ ok ] size 0x%X (3072 KB)" % len(buf))

    # 3) section count bumped to 16
    nsec = rd32(buf, OFF_SECTION_COUNT)
    if nsec != EXPECTED_SECTION_COUNT:
        raise PatchError(
            "Section count is %d, expected %d. The .hacks section is missing - "
            "xplode with XboxImageXploder v1.2 first." % (nsec, EXPECTED_SECTION_COUNT)
        )
    log.append("  [ ok ] section count = %d" % nsec)

    # 4) the .hacks record at file 0x6B8: flag byte checked by mask (0x06 or 0x07
    #    are both valid XboxImageXploder outputs); the remaining 16 bytes
    #    (VA / vsize / raw / rsize + 3 zero pad bytes of flags) must match exactly.
    flag = rd32(buf, HACKS_REC_OFF) & 0xFFFFFFFF
    flag_byte = flag & 0xFF
    if (flag & 0xFFFFFF00) != 0:
        raise PatchError(
            ".hacks flags dword @ file 0x6B8 has garbage in upper bytes (0x%08X)."
            % flag
        )
    if (flag_byte & HACKS_FLAG_REQUIRED_MASK) != HACKS_FLAG_REQUIRED_MASK:
        raise PatchError(
            ".hacks flag byte @ file 0x6B8 is 0x%02X; Preload+Executable "
            "(mask 0x06) must be set. Re-xplode with XboxImageXploder v1.2."
            % flag_byte
        )
    if (flag_byte & ~HACKS_FLAG_ALLOWED_MASK) != 0:
        raise PatchError(
            ".hacks flag byte @ file 0x6B8 is 0x%02X; unexpected bits set. "
            "Re-xplode with XboxImageXploder v1.2." % flag_byte
        )
    tail = buf[HACKS_REC_OFF + 4:HACKS_REC_OFF + 4 + len(EXPECTED_HACKS_TAIL)]
    if tail != EXPECTED_HACKS_TAIL:
        raise PatchError(
            ".hacks section-header VA/size/offset fields @ file 0x6BC do not "
            "match the expected XboxImageXploder layout.\n  found    : %s\n"
            "  expected : %s\nRe-xplode: "
            "XboxImageXploder.exe default.xbe .hacks 4096"
            % (tail.hex(" "), EXPECTED_HACKS_TAIL.hex(" "))
        )
    log.append("  [ ok ] .hacks record @ 0x6B8 (VA 0x42D000, raw 0x2FF000, size 0x1000, flags 0x%02X)" % flag_byte)

    # 5) cross-check the section table actually contains the .hacks record too
    base = rd32(buf, OFF_BASE_ADDR)
    hdr = rd32(buf, OFF_SECTION_HDRS) - base
    found = False
    for i in range(nsec):
        r = hdr + i * SECTION_HDR_SIZE
        if r + SECTION_HDR_SIZE > len(buf):
            break
        if rd32(buf, r + 0x04) == HACKS_VA and rd32(buf, r + 0x0C) == HACKS_RAW:
            rsize = rd32(buf, r + 0x10)
            tab_flag = rd32(buf, r + 0x00) & 0xFF
            if rsize != HACKS_SIZE:
                raise PatchError(
                    ".hacks section present but raw size is 0x%X (expected 0x%X)."
                    % (rsize, HACKS_SIZE)
                )
            if (tab_flag & HACKS_FLAG_REQUIRED_MASK) != HACKS_FLAG_REQUIRED_MASK \
               or (tab_flag & ~HACKS_FLAG_ALLOWED_MASK) != 0:
                raise PatchError(
                    ".hacks section present but flag byte is 0x%02X "
                    "(expected 0x06 or 0x07)." % tab_flag
                )
            found = True
            break
    if not found:
        raise PatchError("Section table does not contain a .hacks record at VA 0x42D000 / raw 0x2FF000.")
    log.append("  [ ok ] .hacks confirmed in section table")

    # 6) .hacks body region must be free (zero-filled) so the read-cave has room
    body = buf[HACKS_RAW:HACKS_RAW + 0x40]
    if any(body) and body != READ_CAVE + b"\x00" * (0x40 - len(READ_CAVE)):
        # allow the case where the read-cave is already written (idempotent)
        if body[:len(READ_CAVE)] != READ_CAVE:
            raise PatchError(
                ".hacks body @ file 0x2FF000 is not empty (holds unexpected data). "
                "The section was not added cleanly."
            )
    log.append("  [ ok ] .hacks body @ 0x2FF000 is available")

    # 7) dead RET-stub host field for the caves (C3 + 0x90*15), or already patched
    if bytes(buf[TAG_CAVE_OFF:TAG_CAVE_OFF + 8]) != TAG_CAVE[:8]:
        boundary = buf[TAG_CAVE_OFF - 1]        # file 0x20A2B0
        pad = buf[TAG_CAVE_OFF:TAG_CAVE_OFF + 15]
        if boundary != 0xC3 or any(x != 0x90 for x in pad):
            raise PatchError(
                "Code-cave host field @ file 0x20A2B0 is not the expected dead "
                "RET-stub padding (boundary 0x%02X, pad %s). This is not the "
                "validated Manhunt build, or it is modified differently."
                % (boundary, pad.hex(" "))
            )
        log.append("  [ ok ] cave host field @ 0x20A2B0 confirmed dead RET-stub padding")
    else:
        log.append("  [note] cave host field already holds the tag-cave (re-patch)")

    # 8) redirect sites hold either stock bytes or the already-applied redirect
    for off, stock, patched, label in (
        (REDIR_D79D0_OFF, STOCK_D79D0, REDIR_D79D0, "FUN_000d79d0 entry"),
        (REDIR_13B0F_OFF, STOCK_13B0F, REDIR_13B0F, "FUN_00013930 mixbin site"),
    ):
        cur_stock = bytes(buf[off:off + len(stock)])
        cur_patch = bytes(buf[off:off + len(patched)])
        if cur_stock != stock and cur_patch != patched:
            raise PatchError(
                "Redirect site %s @ file 0x%X holds neither stock nor patched "
                "bytes (found %s). File diverges from the validated base."
                % (label, off, cur_stock.hex(" "))
            )
    log.append("  [ ok ] redirect sites hold expected stock/patched bytes")


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------

def _write(out, off, patch_bytes, stock_bytes, label, dry, log):
    n = len(patch_bytes)
    if bytes(out[off:off + n]) == patch_bytes:
        log.append("  [skip] %-22s already applied @ file 0x%X" % (label, off))
        return
    if stock_bytes is not None:
        cur = bytes(out[off:off + len(stock_bytes)])
        if cur != stock_bytes:
            raise PatchError(
                "Refusing to write %s @ file 0x%X: current %s != stock %s"
                % (label, off, cur.hex(" "), stock_bytes.hex(" "))
            )
    if dry:
        log.append("  [dry ] %-22s would write %d bytes @ file 0x%X" % (label, n, off))
        return
    out[off:off + n] = patch_bytes
    log.append("  [ ok ] %-22s wrote %d bytes @ file 0x%X" % (label, n, off))


def apply_patch(out, dry, log):
    _write(out, TAG_CAVE_OFF,    TAG_CAVE,    None,        "tag-cave",        dry, log)
    _write(out, REDIR_D79D0_OFF, REDIR_D79D0, STOCK_D79D0, "redirect d79d0",  dry, log)

    # Wipe any stale bytes between the tag-cave and the stub. A prior, longer cave
    # (e.g. the abandoned buffer-write build) leaves executable leftovers here that
    # corrupt the dead _atexit RET-stubs (turning a harmless C3 into garbage).
    # Restore the pristine dead-stub pattern: C3 at each 16-byte-aligned offset,
    # 0x90 elsewhere. The stub is (re)written on top immediately after.
    tail_start = TAG_CAVE_OFF + len(TAG_CAVE)   # 0x20A2FB
    tail_end = STUB_OFF + 0x10                   # through the stub's own block
    if not dry:
        for off in range(tail_start, tail_end):
            out[off] = 0xC3 if (off & 0xF) == 0 else 0x90
    log.append("  [ ok ] %-22s cleaned 0x%X-0x%X (dead-stub padding)"
               % ("tail scrub", tail_start, tail_end - 1))

    _write(out, STUB_OFF,        STUB,        None,        "stub",            dry, log)
    _write(out, READ_CAVE_OFF,   READ_CAVE,   None,        "read-cave",       dry, log)
    _write(out, REDIR_13B0F_OFF, REDIR_13B0F, STOCK_13B0F, "redirect 13b0f",  dry, log)

    for off, val, label in (
        (REVERT_158C1C_OFF, REVERT_158C1C, "revert 158C1C"),
        (REVERT_158C24_OFF, REVERT_158C24, "revert 158C24"),
    ):
        if bytes(out[off:off + len(val)]) != val:
            if not dry:
                out[off:off + len(val)] = val
            log.append("  [ ok ] %-22s -> %s" % (label, val.hex(" ")))
        else:
            log.append("  [skip] %-22s already stock" % label)

    # The .hacks flag byte is left as XboxImageXploder wrote it (v1.2 emits 0x07 =
    # Writable+Preload+Executable). The known-good file uses 0x07 as well, so the
    # bit is not modified; validation already confirmed it is 0x06 or 0x07.
    log.append("  [note] %-22s left as-is (0x%02X)" % ("hacks flag", out[HACKS_REC_OFF]))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Patch an already-xploded Manhunt default.xbe so the "
                    "director is audible on Xbox 360 backwards compatibility.")
    ap.add_argument("target", help="Extracted xiso folder, or a default.xbe path.")
    ap.add_argument("--dry-run", action="store_true", help="Validate and report; write nothing.")
    ap.add_argument("--no-backup", action="store_true", help="Do not write a .bak copy.")
    args = ap.parse_args()

    try:
        xbe_path = find_xbe(args.target)
    except PatchError as e:
        print("ERROR:", e, file=sys.stderr)
        return 2

    with open(xbe_path, "rb") as f:
        buf = bytearray(f.read())

    print("Target : %s" % xbe_path)
    print("Size   : %d bytes (0x%X)" % (len(buf), len(buf)))
    print()

    log = []
    print("Validating xploded XBE:")
    try:
        validate(buf, log)
    except PatchError as e:
        print("\n".join(log))
        print()
        print("ERROR (validation):", e, file=sys.stderr)
        return 3
    print("\n".join(log))
    print()

    log = []
    print("Applying patch:")
    try:
        apply_patch(buf, args.dry_run, log)
    except PatchError as e:
        print("\n".join(log))
        print()
        print("ERROR (patch):", e, file=sys.stderr)
        return 4
    print("\n".join(log))
    print()

    if args.dry_run:
        print("DRY RUN complete - no bytes written.")
        return 0

    if not args.no_backup:
        bak = xbe_path + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(xbe_path, bak)
            print("Backup : %s" % bak)
        else:
            print("Backup : %s (exists, kept)" % bak)

    with open(xbe_path, "wb") as f:
        f.write(buf)
    print("Patched: %s" % xbe_path)
    print()
    print("Deploy the extracted game to your JTAG/RGH 360 and launch under BC.")
    print("The director - execution-cue lines and scripted dialogue - is now")
    print("audible. All other audio is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())