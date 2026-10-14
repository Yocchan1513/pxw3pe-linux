"""Shared helpers for the PX-W3PE MPEG-TS analysis tools (stdlib only).

Contents:
  - CRC32/MPEG-2
  - packet reader with sync-byte / source-tag normalisation and resync
  - TS header / adaptation field parsing
  - PSI section assembly and PAT/PMT parsing
  - candidate card-descramble schemes:
      (a) "ptx"  : plain XOR with an 8-byte mask built from a 4-byte seed
                   [ptx] drivers/media/pci/ptx/pxq3pe_pci.c (idx/xor arrays)
      (b) "w3pe" : XOR + DES-ECB decrypt, even/odd keys by payload position
                   [w3pe] inaba-garnet pxw3pe_pci.c (SPEC-ENC-DES, ts_process)
    Keys for (b) are not known; they are read from a user-supplied file.

This module never descrambles broadcast (MULTI2/B-CAS) scrambling; such
packets (transport_scrambling_control != 0) are only counted.
"""

import os
import sys

TS_SIZE = 188
SYNC = 0x47
NULL_PID = 0x1FFF

# Bridge source tags replacing the sync byte (measured on the real card).
TAG_NAMES = {0x47: "0x47 (sync / ISDB-S core)", 0xC7: "0xC7 (ISDB-T core tag)"}
DEFAULT_SYNCS = frozenset((0x47, 0xC7))

# --------------------------------------------------------------------------
# CRC32/MPEG-2: poly 0x04C11DB7, init 0xFFFFFFFF, no reflection, no xorout.
# Check value of b"123456789" is 0x0376E6E7. Running it over a whole PSI
# section including its trailing CRC gives 0 when the section is intact.
# --------------------------------------------------------------------------


def _crc_table():
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if (c & 0x80000000) else (c << 1)
        table.append(c & 0xFFFFFFFF)
    return tuple(table)


_CRC_TABLE = _crc_table()


def crc32_mpeg2(data, crc=0xFFFFFFFF):
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


# --------------------------------------------------------------------------
# Packet reader
# --------------------------------------------------------------------------


class ReaderStats:
    def __init__(self):
        self.packets = 0
        self.tags = {}            # first byte value -> count
        self.resyncs = 0          # number of times sync was lost
        self.skipped_bytes = 0    # bytes discarded while (re)syncing
        self.leading_skip = 0     # bytes skipped before the first packet
        self.trailing_bytes = 0   # incomplete packet at EOF


def _find_sync(buf, start, syncs, eof):
    """Find the first offset >= start where up to 3 consecutive packet
    starts carry an accepted sync/tag byte. Returns -1 if more data is
    needed (or nothing found at EOF)."""
    n = len(buf)
    i = start
    while i < n:
        if buf[i] in syncs:
            ok = True
            for k in (1, 2):
                j = i + k * TS_SIZE
                if j >= n:
                    if not eof:
                        return -1          # need more data to confirm
                    break                  # near EOF: accept what we can see
                if buf[j] not in syncs:
                    ok = False
                    break
            if ok:
                return i
        i += 1
    return -1


def iter_packets(f, syncs=DEFAULT_SYNCS, stats=None, max_packets=None,
                 chunk=1 << 20):
    """Yield (tag, packet) from a binary file object.

    `packet` is a 188-byte bytes object whose first byte is normalised to
    0x47; `tag` is the original first byte. Initial sync and lost sync are
    recovered by searching for 3 accepted sync bytes 188 bytes apart.
    A packet that is followed by a sync byte *inside* its own 188 bytes
    (i.e. it was truncated) is dropped; junk *between* packets is skipped.
    """
    if stats is None:
        stats = ReaderStats()
    buf = bytearray()
    pos = 0
    eof = False
    synced = False

    def skip(n):
        stats.skipped_bytes += n
        if not synced:
            stats.leading_skip += n

    while True:
        # Keep at least 3 packets + 1 byte of look-ahead buffered.
        if not eof and len(buf) - pos < 3 * TS_SIZE + 1:
            del buf[:pos]
            pos = 0
            data = f.read(chunk)
            if data:
                buf += data
            else:
                eof = True
            continue
        if len(buf) - pos < TS_SIZE:
            stats.trailing_bytes = len(buf) - pos
            return

        b0 = buf[pos]
        nxt = pos + TS_SIZE
        accept = False
        if synced and b0 in syncs:
            if nxt >= len(buf) or buf[nxt] in syncs:
                accept = True
            else:
                # Next start is bad: is this packet truncated (a valid
                # sync run begins inside it) or is junk following it?
                found = _find_sync(buf, pos + 1, syncs, True)
                accept = not (0 <= found < nxt)
        if not accept:
            found = _find_sync(buf, pos if not synced else pos + 1, syncs, eof)
            if found < 0:
                # Nothing usable in the buffer: drop all but a short tail.
                keep = len(buf) if eof else max(pos, len(buf) - 3 * TS_SIZE)
                skip(keep - pos)
                pos = keep
                if eof:
                    stats.trailing_bytes = 0
                    return
                del buf[:pos]
                pos = 0
                data = f.read(chunk)
                if data:
                    buf += data
                else:
                    eof = True
                continue
            if synced:
                stats.resyncs += 1
            skip(found - pos)
            pos = found
            b0 = buf[pos]
        synced = True
        pkt = bytes((SYNC,)) + bytes(buf[pos + 1:pos + TS_SIZE])
        stats.tags[b0] = stats.tags.get(b0, 0) + 1
        stats.packets += 1
        yield b0, pkt
        pos += TS_SIZE
        if max_packets is not None and stats.packets >= max_packets:
            return


def iter_packets_bytes(data, **kw):
    """Same as iter_packets() but over an in-memory bytes object."""
    import io
    return iter_packets(io.BytesIO(data), **kw)


# --------------------------------------------------------------------------
# TS header parsing
# --------------------------------------------------------------------------


class Header:
    __slots__ = ("tei", "pusi", "prio", "pid", "tsc", "afc", "cc",
                 "has_payload", "af_len", "disc", "rai", "pcr",
                 "payload_off", "af_bad")

    def __init__(self, pkt):
        b1, b2, b3 = pkt[1], pkt[2], pkt[3]
        self.tei = (b1 >> 7) & 1
        self.pusi = (b1 >> 6) & 1
        self.prio = (b1 >> 5) & 1
        self.pid = ((b1 & 0x1F) << 8) | b2
        self.tsc = (b3 >> 6) & 3
        self.afc = (b3 >> 4) & 3
        self.cc = b3 & 0xF
        self.has_payload = bool(self.afc & 1)
        self.af_len = 0
        self.disc = 0
        self.rai = 0
        self.pcr = None
        self.af_bad = False
        off = 4
        if self.afc & 2:
            # The adaptation field is part of the 184 bytes after the
            # header; on the card it is scrambled too, so treat it with care.
            self.af_len = pkt[4]
            off = 5 + self.af_len
            # Legal lengths: 183 if adaptation only, 0..182 with payload.
            self.af_bad = (self.af_len > 182) if self.has_payload \
                else (self.af_len != 183)
            if 0 < self.af_len <= 183:
                flags = pkt[5]
                self.disc = (flags >> 7) & 1
                self.rai = (flags >> 6) & 1
                if (flags & 0x10) and self.af_len >= 7:
                    p = pkt[6:12]
                    base = (p[0] << 25) | (p[1] << 17) | (p[2] << 9) | \
                           (p[3] << 1) | (p[4] >> 7)
                    ext = ((p[4] & 1) << 8) | p[5]
                    self.pcr = base * 300 + ext
        self.payload_off = min(off, TS_SIZE)


PCR_WRAP = (1 << 33) * 300  # PCR in 27 MHz units wraps at 2^33 * 300


# --------------------------------------------------------------------------
# PSI sections
# --------------------------------------------------------------------------


def check_section(sec):
    """Validate a complete long-form PSI section. Returns (ok, reason)."""
    if len(sec) < 3:
        return False, "too short"
    ssi = sec[1] >> 7
    slen = ((sec[1] & 0x0F) << 8) | sec[2]
    if len(sec) != 3 + slen:
        return False, "length mismatch"
    if not ssi:
        return False, "section_syntax_indicator=0"
    if slen > 1021:
        return False, "section_length>1021"
    if slen < 9:
        return False, "section_length<9"
    if crc32_mpeg2(sec) != 0:
        return False, "CRC32 mismatch"
    return True, "ok"


class SectionAssembler:
    """Reassemble PSI sections from TS payloads of one PID."""

    def __init__(self):
        self.buf = None  # bytearray of the section being collected

    def reset(self):
        self.buf = None

    def _drain(self, out):
        # Emit complete sections from self.buf; keep any remainder.
        while self.buf is not None:
            if len(self.buf) < 1:
                return
            if self.buf[0] == 0xFF:           # stuffing: rest of packet unused
                self.buf = None
                return
            if len(self.buf) < 3:
                return
            slen = ((self.buf[1] & 0x0F) << 8) | self.buf[2]
            total = 3 + slen
            if len(self.buf) < total:
                return
            out.append(bytes(self.buf[:total]))
            rest = self.buf[total:]
            self.buf = rest if rest else None

    def push(self, payload, pusi):
        """Feed one packet payload; returns list of complete sections."""
        out = []
        if pusi:
            if not payload:
                self.buf = None
                return out
            ptr = payload[0]
            if 1 + ptr > len(payload):
                self.buf = None
                return out
            if self.buf is not None:
                self.buf += payload[1:1 + ptr]
                self._drain(out)
                # Anything still incomplete at the next section start is lost.
            self.buf = bytearray(payload[1 + ptr:])
            self._drain(out)
        elif self.buf is not None:
            self.buf += payload
            self._drain(out)
        return out


def parse_pat(sec):
    """Return dict(tsid, version, programs={program_number: pid})."""
    tsid = (sec[3] << 8) | sec[4]
    version = (sec[5] >> 1) & 0x1F
    progs = {}
    end = len(sec) - 4
    i = 8
    while i + 4 <= end:
        pn = (sec[i] << 8) | sec[i + 1]
        pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
        progs[pn] = pid
        i += 4
    return {"tsid": tsid, "version": version, "programs": progs}


STREAM_TYPES = {
    0x01: "MPEG-1 video", 0x02: "MPEG-2 video", 0x03: "MPEG-1 audio",
    0x04: "MPEG-2 audio", 0x06: "PES private (subtitles etc.)",
    0x0B: "DSM-CC type B (data carousel)", 0x0C: "DSM-CC type C",
    0x0D: "DSM-CC type D", 0x0F: "AAC (ADTS)", 0x11: "AAC (LATM)",
    0x1B: "H.264", 0x24: "H.265", 0x05: "private sections",
}


def parse_pmt(sec):
    """Return dict(program, version, pcr_pid, streams=[(type, pid)])."""
    program = (sec[3] << 8) | sec[4]
    version = (sec[5] >> 1) & 0x1F
    pcr_pid = ((sec[8] & 0x1F) << 8) | sec[9]
    pil = ((sec[10] & 0x0F) << 8) | sec[11]
    i = 12 + pil
    end = len(sec) - 4
    streams = []
    while i + 5 <= end:
        st = sec[i]
        pid = ((sec[i + 1] & 0x1F) << 8) | sec[i + 2]
        eil = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
        streams.append((st, pid))
        i += 5 + eil
    return {"program": program, "version": version, "pcr_pid": pcr_pid,
            "streams": streams}


# --------------------------------------------------------------------------
# Candidate card-descramble schemes
# --------------------------------------------------------------------------

# [ptx] pxq3pe_pci.c: idx[8] = {0,0,3,1,0,2,1,2}, xor[4] = {2F,46,56,E3}.
# [w3pe] pxw3pe_pci.c: des_xor_idx[8] = {0,0,3,1,0,2,1,2} (same expansion).
XOR_IDX = (0, 0, 3, 1, 0, 2, 1, 2)
PTX_XOR_SEED = bytes((0x2F, 0x46, 0x56, 0xE3))


def expand_mask(seed4):
    """4-byte seed -> 8-byte mask, mask[k] = seed[XOR_IDX[k]]."""
    if len(seed4) != 4:
        raise ValueError("XOR seed must be 4 bytes")
    return bytes(seed4[i] for i in XOR_IDX)


def mask_to_seed(mask8):
    """Inverse of expand_mask(): returns the seed if mask8 has the required
    structure (m0=m1=m4, m3=m6, m5=m7), else None."""
    m = mask8
    if m[0] == m[1] == m[4] and m[3] == m[6] and m[5] == m[7]:
        return bytes((m[0], m[3], m[5], m[2]))
    return None


def xor_payload(pkt, mask8):
    """Scheme (a), ptx: XOR bytes 4..187 with mask8[n % 8] (n = payload
    index). Header bytes 0..3 untouched. XOR is its own inverse."""
    out = bytearray(pkt)
    for n in range(TS_SIZE - 4):
        out[4 + n] ^= mask8[n & 7]
    return bytes(out)


class W3peKeys:
    """Key material for scheme (b). Loaded from a text file with lines
    even=16hex, odd=16hex, xor=8hex ('#' comments allowed)."""

    def __init__(self, even, odd, xor_seed):
        from des import DES
        self.even = bytes(even)
        self.odd = bytes(odd)
        self.xor_seed = bytes(xor_seed)
        self.mask8 = expand_mask(self.xor_seed)
        self.des_even = DES(self.even)
        self.des_odd = DES(self.odd)

    @classmethod
    def load(cls, path):
        vals = {}
        with open(path, "r", encoding="ascii") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                if "=" not in line:
                    raise ValueError(f"{path}:{lineno}: expected key=hex")
                k, v = (s.strip().lower() for s in line.split("=", 1))
                vals[k] = bytes.fromhex(v.replace(" ", ""))
        need = {"even": 8, "odd": 8, "xor": 4}
        for k, n in need.items():
            if k not in vals or len(vals[k]) != n:
                raise ValueError(f"{path}: '{k}' must be {n * 2} hex digits")
        return cls(vals["even"], vals["odd"], vals["xor"])


def w3pe_descramble(pkt, keys):
    """Scheme (b), w3pe ts_process(): for each 8-byte block of the 184-byte
    payload: block ^= mask8, then DES-ECB decrypt with the even key for
    payload bytes 0..127 and the odd key for 128..183."""
    out = bytearray(pkt[:4])
    for b in range(0, TS_SIZE - 4, 8):
        blk = bytes(x ^ m for x, m in zip(pkt[4 + b:12 + b], keys.mask8))
        d = keys.des_even if b < 128 else keys.des_odd
        out += d.decrypt_block(blk)
    return bytes(out)


def w3pe_scramble(pkt, keys):
    """Inverse of w3pe_descramble() (used only to build test data)."""
    out = bytearray(pkt[:4])
    for b in range(0, TS_SIZE - 4, 8):
        d = keys.des_even if b < 128 else keys.des_odd
        enc = d.encrypt_block(pkt[4 + b:12 + b])
        out += bytes(x ^ m for x, m in zip(enc, keys.mask8))
    return bytes(out)


def ensure_local_import_path():
    """Allow `python3 -I tools/tsanalysis/x.py`: -I drops the script dir
    from sys.path, so sibling modules are added back explicitly."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


ensure_local_import_path()
