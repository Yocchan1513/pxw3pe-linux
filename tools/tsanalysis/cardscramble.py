#!/usr/bin/env python3
"""cardscramble.py -- test card-side (bridge/crypto-chip) descramble
hypotheses on PAT (PID 0) packets of a PX-W3PE capture.

Usage:
    python3 -I tools/tsanalysis/cardscramble.py FILE.ts [--max N]
    python3 -I tools/tsanalysis/cardscramble.py --hex "c7 60 00 14 f2 ..." [--hex ...]
    ... [--keys KEYFILE] [--xor-seed 2F4656E3] [--write-out OUT.ts --scheme a|b]

Schemes tried on every PID-0 packet:
  raw  : no transformation (is the payload already clear?)
  a    : "ptx" XOR. [ptx] pxq3pe_pci.c: payload byte n ^= mask[n % 8],
         mask[k] = xor[idx[k]], idx = 0,0,3,1,0,2,1,2, xor = 2F 46 56 E3
         (seed overridable with --xor-seed).
  b    : "w3pe" XOR + DES-ECB decrypt (only with --keys). [w3pe]
         pxw3pe_pci.c ts_process(): per 8-byte block, ^= mask8, DES decrypt,
         even key for payload bytes 0..127, odd key for 128..183.
         Key file lines: even=16hex  odd=16hex  xor=8hex  (# comments ok)

For each scheme the PAT check reports pointer_field, table_id == 0x00,
section_syntax_indicator, section_length and CRC32/MPEG-2 (CRC only if the
whole section is inside the bytes given; --hex input may be truncated).

Structure checks on the *raw* ciphertext (no keys needed):
  - identical PAT content -> identical ciphertext across packets?
    (ECB-like, no per-packet / CC-dependent variation)
  - repeated 8-byte blocks inside one packet (the PAT's 0xFF stuffing
    under ECB or a periodic XOR shows up as repeats); assuming the repeated
    block is 0xFF stuffing, the implied XOR mask / seed is printed.

Broadcast MULTI2 (B-CAS) scrambling is NOT handled; TSC != 0 is only counted.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for -I

import tslib  # noqa: E402
from tslib import TS_SIZE, Header  # noqa: E402


# --------------------------------------------------------------------------
# PAT validity check on a single (possibly truncated) packet
# --------------------------------------------------------------------------

def check_pat_packet(pkt):
    """Check a single PID-0 packet (first byte already normalised).

    Returns dict(status, reasons, section, pat) where status is one of
      'valid'      : full section present, all checks + CRC OK
      'plausible'  : header fields OK but section truncated (CRC unknown)
      'invalid'    : some check failed
    """
    reasons = []
    if len(pkt) < 5:
        return {"status": "invalid", "reasons": ["packet too short"],
                "section": None, "pat": None}
    h = Header(pkt + bytes(max(0, TS_SIZE - len(pkt))))
    if not h.pusi:
        reasons.append("PUSI=0 (no section start in this packet)")
        return {"status": "invalid", "reasons": reasons, "section": None,
                "pat": None}
    if h.afc != 1:
        reasons.append("adaptation_field_control=%d (PAT normally 1)" % h.afc)
    off = 4
    if h.afc & 2:
        off = 5 + pkt[4]
        if off >= len(pkt):
            reasons.append("adaptation field runs past data")
            return {"status": "invalid", "reasons": reasons, "section": None,
                    "pat": None}
    ptr = pkt[off]
    s = off + 1 + ptr
    if ptr > 182 - (off - 4):
        reasons.append("pointer_field=%d out of range" % ptr)
        return {"status": "invalid", "reasons": reasons, "section": None,
                "pat": None}
    if ptr != 0:
        reasons.append("pointer_field=%d (PAT normally 0)" % ptr)
    if s + 3 > len(pkt):
        reasons.append("truncated before section header")
        return {"status": "plausible" if not reasons[:-1] else "invalid",
                "reasons": reasons, "section": None, "pat": None}
    tid, b1, b2 = pkt[s], pkt[s + 1], pkt[s + 2]
    slen = ((b1 & 0x0F) << 8) | b2
    fatal = False
    if tid != 0x00:
        reasons.append("table_id=0x%02X (want 0x00)" % tid)
        fatal = True
    if not (b1 & 0x80):
        reasons.append("section_syntax_indicator=0")
        fatal = True
    if b1 & 0x40:
        reasons.append("'0' bit set")
        fatal = True
    if slen < 9 or slen > 1021 or (slen - 9) % 4:
        reasons.append("section_length=%d implausible for PAT" % slen)
        fatal = True
    if fatal:
        return {"status": "invalid", "reasons": reasons, "section": None,
                "pat": None}
    end = s + 3 + slen
    if end > len(pkt):
        reasons.append("section (%d B) not complete in given bytes; CRC "
                       "not checked" % (3 + slen))
        return {"status": "plausible", "reasons": reasons, "section": None,
                "pat": None}
    sec = bytes(pkt[s:end])
    if tslib.crc32_mpeg2(sec) != 0:
        reasons.append("CRC32 mismatch")
        return {"status": "invalid", "reasons": reasons, "section": sec,
                "pat": None}
    reasons.append("CRC32 OK")
    return {"status": "valid", "reasons": reasons, "section": sec,
            "pat": tslib.parse_pat(sec)}


# --------------------------------------------------------------------------
# Ciphertext structure analysis
# --------------------------------------------------------------------------

def ecb_consistency(pkts):
    """Compare payloads (bytes 4..) of PID-0 packets.

    Returns dict with n, distinct payloads, distinct CC values, and a
    short interpretation string."""
    payloads = {}
    ccs = set()
    for p in pkts:
        cc = p[3] & 0xF
        ccs.add(cc)
        payloads.setdefault(bytes(p[4:]), set()).add(cc)
    n = len(pkts)
    d = len(payloads)
    if n < 2:
        interp = "need >= 2 PID-0 packets"
    elif d == 1:
        interp = ("all %d payloads identical across %d CC values -> no "
                  "per-packet variation (ECB-like / static XOR)" % (n, len(ccs)))
    elif d <= max(2, n // 8):
        interp = ("%d distinct payloads in %d packets -> mostly identical "
                  "(PAT version change or occasional errors?)" % (d, n))
    elif all(len(c) == 1 for c in payloads.values()) and len(ccs) > 1 and \
            d <= len(ccs):
        interp = ("payload varies with CC (%d distinct, one per CC) -> "
                  "keystream may depend on CC / packet counter" % d)
    else:
        interp = ("%d distinct payloads in %d packets -> per-packet "
                  "variation (not plain ECB with fixed keys)" % (d, n))
    return {"n": n, "distinct": d, "ccs": len(ccs), "interp": interp}


def block_repeats(pkt):
    """Find repeated 8-byte blocks in the payload (bytes 4..). Returns a list
    of (block_bytes, [block indices]) for blocks occurring >= 2 times,
    most frequent first."""
    pay = pkt[4:]
    seen = {}
    for i in range(0, len(pay) - 7, 8):
        seen.setdefault(bytes(pay[i:i + 8]), []).append(i // 8)
    reps = [(b, ix) for b, ix in seen.items() if len(ix) >= 2]
    reps.sort(key=lambda t: -len(t[1]))
    return reps


def implied_masks(pkt):
    """Assume the most repeated block(s) are 0xFF stuffing encrypted with a
    plain 8-byte periodic XOR, and derive mask = block ^ 0xFF.
    Returns list of (mask, seed_or_None, block indices)."""
    out = []
    for blk, ix in block_repeats(pkt)[:2]:
        mask = bytes(b ^ 0xFF for b in blk)
        out.append((mask, tslib.mask_to_seed(mask), ix))
    return out


# --------------------------------------------------------------------------

def _hexs(b):
    return " ".join("%02x" % x for x in b)


def load_pid0_from_file(path, max_n):
    pkts, tags = [], {}
    with open(path, "rb") as f:
        for tag, pkt in tslib.iter_packets(f):
            if ((pkt[1] & 0x1F) << 8 | pkt[2]) != 0:
                continue
            tags[tag] = tags.get(tag, 0) + 1
            pkts.append(pkt)
            if len(pkts) >= max_n:
                break
    return pkts, tags


def parse_hex_packet(s):
    b = bytes.fromhex("".join(s.replace(":", " ").split()))
    if not b:
        raise ValueError("empty --hex")
    if len(b) > TS_SIZE:
        raise ValueError("--hex longer than 188 bytes")
    tag = b[0]
    return tag, bytes((tslib.SYNC,)) + b[1:]


def apply_scheme(name, pkt, mask_a, keys):
    """Apply a descramble candidate to a full or truncated packet."""
    if name == "raw":
        return pkt
    if name == "a":
        out = bytearray(pkt)
        for n in range(len(pkt) - 4):
            out[4 + n] ^= mask_a[n & 7]
        return bytes(out)
    if name == "b":
        full = len(pkt) == TS_SIZE
        p = pkt if full else pkt + bytes(TS_SIZE - len(pkt))
        d = tslib.w3pe_descramble(p, keys)
        # Only whole 8-byte blocks that were actually given are meaningful.
        n_ok = 4 + ((len(pkt) - 4) // 8) * 8
        return d if full else d[:n_ok]
    raise ValueError(name)


def analyse(pkts, mask_a, keys=None, out=sys.stdout, verbose=True):
    """Run all checks; returns dict scheme -> list of check results and the
    ECB consistency dict (used by selftest)."""
    w = out.write
    schemes = ["raw", "a"] + (["b"] if keys else [])
    results = {s: [] for s in schemes}
    for i, p in enumerate(pkts):
        h = Header(p + bytes(max(0, TS_SIZE - len(p))))
        if verbose:
            w("packet %d: %d B, PID 0x%04X PUSI %d TSC %d AFC %d CC %d\n" % (
                i, len(p), h.pid, h.pusi, h.tsc, h.afc, h.cc))
        if h.tsc:
            w("  TSC=%d: broadcast-scrambled bit set on PID 0 (unexpected); "
              "not descrambled\n" % h.tsc)
        for s in schemes:
            d = apply_scheme(s, p, mask_a, keys)
            r = check_pat_packet(d)
            results[s].append(r)
            if verbose:
                w("  %-4s %-9s %s\n" % (s, r["status"], "; ".join(r["reasons"])))
                w("       payload[0:16] %s\n" % _hexs(d[4:20]))
                if r["pat"]:
                    w("       PAT tsid 0x%04X: %s\n" % (r["pat"]["tsid"], ", ".join(
                        "%d->0x%04X" % kv for kv in sorted(r["pat"]["programs"].items()))))

    w("\nsummary (valid / plausible / invalid out of %d):\n" % len(pkts))
    for s in schemes:
        st = [r["status"] for r in results[s]]
        w("  %-4s %d / %d / %d\n" % (s, st.count("valid"), st.count("plausible"),
                                     st.count("invalid")))

    full = [p for p in pkts if len(p) == TS_SIZE]
    ecb = ecb_consistency(full)
    w("\nciphertext consistency: %s\n" % ecb["interp"])
    if full:
        reps = block_repeats(full[0])
        if reps:
            w("repeated 8-byte blocks in packet 0 payload:\n")
            for blk, ix in reps[:3]:
                w("  %s at blocks %s\n" % (_hexs(blk), ix))
            for mask, seed, ix in implied_masks(full[0]):
                w("  if blocks %s are 0xFF stuffing under periodic XOR: mask %s"
                  " -> %s\n" % (ix[:6], _hexs(mask),
                                "seed %s (fits idx 0,0,3,1,0,2,1,2)" % _hexs(seed)
                                if seed else "does NOT fit the idx structure"))
            ix0 = reps[0][1]
            if any(i < 16 for i in ix0) and any(i >= 16 for i in ix0):
                w("  note: same block repeats across the even/odd (128 B) "
                  "boundary -> one key for all blocks\n")
        else:
            w("no repeated 8-byte blocks in packet 0 payload (stuffing not "
              "visible -> not ECB/periodic over 8 B, or no stuffing)\n")
    else:
        w("(no full 188-byte packets: block analysis skipped)\n")
    return results, ecb


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", nargs="?", help="TS capture (PID-0 packets used)")
    ap.add_argument("--hex", action="append", default=[],
                    help="one PID-0 packet as hex (may be truncated); repeatable")
    ap.add_argument("--max", type=int, default=64,
                    help="max PID-0 packets read from FILE (default 64)")
    ap.add_argument("--keys", help="key file for scheme (b)")
    ap.add_argument("--xor-seed", default=tslib.PTX_XOR_SEED.hex(),
                    help="4-byte seed for scheme (a), hex (default ptx 2f4656e3)")
    ap.add_argument("--write-out", metavar="OUT",
                    help="descramble the WHOLE file with --scheme into OUT "
                         "(tags normalised to 0x47)")
    ap.add_argument("--scheme", choices=("a", "b"), default="a")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="summary only, no per-packet lines")
    a = ap.parse_args(argv)

    try:
        mask_a = tslib.expand_mask(bytes.fromhex(a.xor_seed))
        keys = tslib.W3peKeys.load(a.keys) if a.keys else None
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if not a.file and not a.hex:
        ap.error("give FILE or --hex")

    pkts = []
    try:
        for s in a.hex:
            pkts.append(parse_hex_packet(s)[1])
        if a.file:
            fp, tags = load_pid0_from_file(a.file, a.max)
            print("read %d PID-0 packets from %s (tags: %s)" % (
                len(fp), a.file, ", ".join("0x%02X x%d" % kv
                                            for kv in sorted(tags.items()))))
            pkts += fp
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    if not pkts:
        print("no PID-0 packets found")
        return 1

    print("scheme a mask: %s" % _hexs(mask_a))
    if keys:
        print("scheme b keys loaded from %s (values not printed)" % a.keys)
    results, _ = analyse(pkts, mask_a, keys, verbose=not a.quiet)

    if a.write_out:
        if not a.file:
            ap.error("--write-out needs FILE")
        if a.scheme == "b" and not keys:
            ap.error("--scheme b needs --keys")
        n = 0
        with open(a.file, "rb") as fi, open(a.write_out, "wb") as fo:
            for _tag, pkt in tslib.iter_packets(fi):
                if a.scheme == "a":
                    fo.write(tslib.xor_payload(pkt, mask_a))
                else:
                    fo.write(tslib.w3pe_descramble(pkt, keys))
                n += 1
        print("wrote %d packets (scheme %s) to %s" % (n, a.scheme, a.write_out))

    any_valid = any(r["status"] == "valid" for s in results if s != "raw"
                    for r in results[s])
    return 0 if any_valid else 1


if __name__ == "__main__":
    sys.exit(main())
