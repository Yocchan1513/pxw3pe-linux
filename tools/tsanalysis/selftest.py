#!/usr/bin/env python3
"""selftest.py -- self-tests for the tsanalysis tools (stdlib unittest).

Usage:
    python3 -I tools/tsanalysis/selftest.py [-v]

Builds synthetic transport streams in memory (PAT + PMT + PCR + payload
PIDs, CRC32/MPEG-2 computed), injects sync loss, CC errors, TEI and
scrambling (broadcast TSC bits, card scheme (a), card scheme (b) with
made-up keys), and checks tscheck / cardscramble findings. Also runs the
DES known-answer vectors and the CRC32/MPEG-2 check value.
No files are written.
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for -I

import cardscramble  # noqa: E402
import des  # noqa: E402
import tscheck  # noqa: E402
import tslib  # noqa: E402
from tslib import TS_SIZE  # noqa: E402

PMT_PID = 0x01F0
PCR_PID = 0x01FF
VID_PID = 0x0111
AUD_PID = 0x0112
BITRATE = 16_000_000          # bit/s of the synthetic stream

# First 32 bytes of a real PAT packet captured from the PX-W3PE (T0, 27ch,
# seed = 16 zero bytes, card scrambling active).
REAL_PAT32 = ("c7 60 00 14 f2 c1 76 4f 31 d9 e8 d9 fe 16 35 99 "
              "66 8c 72 4b 2b 39 a4 61 c8 ba 28 c8 67 12 c0 a9")


# --------------------------------------------------------------------------
# Synthetic stream builder
# --------------------------------------------------------------------------

def make_section(table_id, ext, body, version=0):
    """Long-form PSI section with CRC32/MPEG-2."""
    slen = 5 + len(body) + 4
    hdr = bytes((table_id, 0xB0 | (slen >> 8), slen & 0xFF,
                 ext >> 8, ext & 0xFF, 0xC1 | (version << 1), 0, 0))
    sec = hdr + body
    return sec + tslib.crc32_mpeg2(sec).to_bytes(4, "big")


def pat_section():
    body = bytes((0, 0, 0xE0, 0x10,                      # prog 0 -> NIT
                  0, 1, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF))
    return make_section(0x00, 0x7FE0, body)


def pmt_section():
    body = bytes((0xE0 | (PCR_PID >> 8), PCR_PID & 0xFF, 0xF0, 0x00))
    for st, pid in ((0x02, VID_PID), (0x0F, AUD_PID)):
        body += bytes((st, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, 0x00))
    return make_section(0x02, 1, body)


def ts_packet(pid, cc, payload=b"", pusi=0, tsc=0, tei=0, af=None):
    """Build a 188-byte packet. af: adaptation-field bytes after the length
    byte (flags...), or None. Payload is padded with 0xFF."""
    afc = (2 if af is not None else 0) | (1 if payload else 0)
    hdr = bytes((0x47, (tei << 7) | (pusi << 6) | (pid >> 8), pid & 0xFF,
                 (tsc << 6) | (afc << 4) | (cc & 0xF)))
    body = b""
    if af is not None:
        room = 184 - len(payload)
        af_len = room - 1
        body = bytes((af_len,)) + af + b"\xff" * (af_len - len(af))
    body += payload
    if af is None:
        body = body + b"\xff" * (184 - len(body))
    assert len(body) == 184, len(body)
    return hdr + body


def pcr_af(pcr, disc=0):
    base, ext = divmod(pcr, 300)
    return bytes((0x10 | (disc << 7),
                  (base >> 25) & 0xFF, (base >> 17) & 0xFF, (base >> 9) & 0xFF,
                  (base >> 1) & 0xFF, ((base & 1) << 7) | 0x7E | (ext >> 8),
                  ext & 0xFF))


def build_stream(n=2000):
    """Return a list of 188-byte packets: a clear, error-free TS."""
    pat = pat_section()
    pmt = pmt_section()
    cc = {}
    pkts = []

    def nxt(pid):
        c = cc.get(pid, -1) + 1
        cc[pid] = c & 0xF
        return c & 0xF

    for i in range(n):
        pcr = int(i * TS_SIZE * 8 / BITRATE * 27_000_000)
        k = i % 20
        if k == 0:
            pkts.append(ts_packet(0, nxt(0), b"\x00" + pat, pusi=1))
        elif k == 1:
            pkts.append(ts_packet(PMT_PID, nxt(PMT_PID), b"\x00" + pmt, pusi=1))
        elif k == 2:
            # Adaptation-only PCR packet: CC must not increment.
            pkts.append(ts_packet(PCR_PID, cc.get(PCR_PID, 0), af=pcr_af(pcr)))
            cc[PCR_PID] = cc.get(PCR_PID, 0)
        elif k in (5, 15):
            pkts.append(ts_packet(AUD_PID, nxt(AUD_PID), bytes(range(184))))
        elif k == 19:
            pkts.append(ts_packet(0x1FFF, (i * 7) & 0xF, b"\xff" * 184))
        else:
            pkts.append(ts_packet(VID_PID, nxt(VID_PID),
                                  bytes((i + j) & 0xFF for j in range(184))))
    return pkts


def run_tscheck(data):
    return tscheck.analyze(io.BytesIO(bytes(data)))


def find(pkts, pid, nth=0):
    """Index of the nth packet of pid."""
    k = 0
    for i, p in enumerate(pkts):
        if ((p[1] & 0x1F) << 8 | p[2]) == pid:
            if k == nth:
                return i
            k += 1
    raise KeyError(pid)


def set_tag(pkts, tag):
    return [bytes((tag,)) + p[1:] for p in pkts]


# --------------------------------------------------------------------------

class TestPrimitives(unittest.TestCase):
    def test_des_vectors(self):
        for k, p, c in des.TEST_VECTORS:
            d = des.DES(bytes.fromhex(k))
            self.assertEqual(d.encrypt_block(bytes.fromhex(p)).hex().upper(), c)
            self.assertEqual(d.decrypt_block(bytes.fromhex(c)).hex().upper(), p)

    def test_des_roundtrip(self):
        d = des.DES(bytes.fromhex("A1B2C3D4E5F60718"))
        for i in range(32):
            blk = bytes((i * 37 + j * 11) & 0xFF for j in range(8))
            self.assertEqual(d.decrypt_block(d.encrypt_block(blk)), blk)

    def test_crc32_mpeg2(self):
        self.assertEqual(tslib.crc32_mpeg2(b"123456789"), 0x0376E6E7)
        self.assertEqual(tslib.crc32_mpeg2(pat_section()), 0)

    def test_mask_expand(self):
        m = tslib.expand_mask(tslib.PTX_XOR_SEED)
        self.assertEqual(m.hex(), "2f2f e346 2f56 4656".replace(" ", ""))
        self.assertEqual(tslib.mask_to_seed(m), tslib.PTX_XOR_SEED)


class TestTscheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pkts = build_stream()

    def test_clean(self):
        r = run_tscheck(b"".join(self.pkts))
        self.assertEqual(r["reader"].packets, len(self.pkts))
        self.assertEqual(r["reader"].resyncs, 0)
        self.assertEqual(r["tei"], 0)
        self.assertEqual(sum(p.cc_errors for p in r["pids"].values()), 0)
        self.assertEqual(r["pat"]["programs"], {0: 0x10, 1: PMT_PID})
        self.assertEqual(r["pmts"][1]["streams"], [(0x02, VID_PID), (0x0F, AUD_PID)])
        self.assertEqual(r["pmts"][1]["pcr_pid"], PCR_PID)
        self.assertEqual(r["pcr_pid"], PCR_PID)
        self.assertAlmostEqual(r["bitrate"] / BITRATE, 1.0, delta=0.01)
        self.assertEqual(r["verdict"][0], "payload looks clear")
        out = io.StringIO()
        self.assertEqual(tscheck.report(r, out=out), 0)

    def test_tag_c7_and_resync(self):
        tagged = set_tag(self.pkts, 0xC7)
        data = bytearray(b"\x12" * 37)                 # leading junk
        for i, p in enumerate(tagged):
            data += p
            if i == 500:
                data += b"\xc7\x00\x47\x11\x22"        # junk between packets
            if i == 900:
                data += b"\x47" * 3                    # more junk
        r = run_tscheck(data)
        self.assertEqual(r["reader"].packets, len(self.pkts))
        self.assertEqual(r["reader"].leading_skip, 37)
        self.assertEqual(r["reader"].resyncs, 2)
        self.assertEqual(r["reader"].tags, {0xC7: len(self.pkts)})
        self.assertEqual(sum(p.cc_errors for p in r["pids"].values()), 0)
        self.assertEqual(r["verdict"][0], "payload looks clear")
        out = io.StringIO()
        self.assertGreater(tscheck.report(r, out=out), 0)   # tag warning
        self.assertIn("WARNING", out.getvalue())

    def test_truncated_packet(self):
        pk = list(self.pkts)
        i = find(pk, VID_PID, 100)
        data = b"".join(pk[:i]) + pk[i][:100] + b"".join(pk[i + 1:])
        r = run_tscheck(data)
        self.assertEqual(r["reader"].packets, len(pk) - 1)
        self.assertEqual(r["reader"].resyncs, 1)
        self.assertEqual(r["pids"][VID_PID].cc_errors, 1)

    def test_cc_rules(self):
        pk = list(self.pkts)
        # 1) drop one video packet -> 1 error
        del pk[find(pk, VID_PID, 50)]
        # 2) duplicate one audio packet once -> allowed
        j = find(pk, AUD_PID, 10)
        pk.insert(j + 1, pk[j])
        # 3) triplicate another audio packet -> 1 error
        j = find(pk, AUD_PID, 30)
        pk.insert(j + 1, pk[j])
        pk.insert(j + 1, pk[j])
        # 4) PMT CC jump with discontinuity_indicator=1 -> no error
        #    (re-number all later PMT packets to keep continuity afterwards)
        n = 0
        for k, p in enumerate(pk):
            if ((p[1] & 0x1F) << 8 | p[2]) != PMT_PID:
                continue
            if n >= 20:
                cc = (n + 7) & 0xF
                if n == 20:
                    pk[k] = ts_packet(PMT_PID, cc, b"\x00" + pmt_section(),
                                      pusi=1, af=bytes((0x80,)))
                else:
                    pk[k] = ts_packet(PMT_PID, cc, b"\x00" + pmt_section(), pusi=1)
            n += 1
        # 5) null packets have random CC -> ignored (already in stream)
        r = run_tscheck(b"".join(pk))
        self.assertEqual(r["pids"][VID_PID].cc_errors, 1)
        self.assertEqual(r["pids"][AUD_PID].cc_errors, 1)
        self.assertEqual(r["pids"][PMT_PID].cc_errors, 0)
        self.assertEqual(r["pids"][PCR_PID].cc_errors, 0)   # AF-only, no incr
        self.assertEqual(r["pids"][0x1FFF].cc_errors, 0)

    def test_af_only_cc_change_is_error(self):
        pk = list(self.pkts)
        i = find(pk, PCR_PID, 10)
        p = bytearray(pk[i])
        p[3] = (p[3] & 0xF0) | ((p[3] + 1) & 0xF)
        pk[i] = bytes(p)
        r = run_tscheck(b"".join(pk))
        # changed CC on no-payload packet, and the next one differs again
        self.assertEqual(r["pids"][PCR_PID].cc_errors, 2)

    def test_tei(self):
        pk = list(self.pkts)
        for nth in (3, 40, 77):
            i = find(pk, VID_PID, nth)
            p = bytearray(pk[i])
            p[1] |= 0x80
            pk[i] = bytes(p)
        r = run_tscheck(b"".join(pk))
        self.assertEqual(r["tei"], 3)
        self.assertEqual(r["pids"][VID_PID].tei, 3)

    def test_broadcast_scrambled(self):
        pk = []
        for p in self.pkts:
            if ((p[1] & 0x1F) << 8 | p[2]) in (VID_PID, AUD_PID):
                p = p[:3] + bytes(((p[3] & 0x3F) | 0xC0,)) + p[4:]
            pk.append(p)
        r = run_tscheck(b"".join(pk))
        self.assertEqual(r["pids"][VID_PID].tsc[3], r["pids"][VID_PID].count)
        self.assertEqual(r["verdict"][0], "broadcast-scrambled")
        self.assertIn("0x0111", r["verdict"][1])

    def test_card_scrambled_scheme_a(self):
        mask = tslib.expand_mask(tslib.PTX_XOR_SEED)
        pk = set_tag([tslib.xor_payload(p, mask) for p in self.pkts], 0xC7)
        r = run_tscheck(b"".join(pk))
        self.assertEqual(r["verdict"][0], "card-scrambled")
        self.assertIsNone(r["pat"])
        # Header-level stats stay valid: no CC errors, TSC all 0.
        self.assertEqual(r["pids"][VID_PID].cc_errors, 0)


class TestCardscramble(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clear = build_stream(400)
        cls.pat_clear = [p for p in cls.clear if p[1] & 0x1F == 0 and p[2] == 0]

    def _run(self, pkts, keys=None):
        mask = tslib.expand_mask(tslib.PTX_XOR_SEED)
        return cardscramble.analyse(pkts, mask, keys, out=io.StringIO(),
                                    verbose=False)

    def test_clear_input(self):
        res, ecb = self._run(self.pat_clear)
        self.assertTrue(all(r["status"] == "valid" for r in res["raw"]))
        self.assertTrue(all(r["status"] == "invalid" for r in res["a"]))
        self.assertEqual(ecb["distinct"], 1)

    def test_scheme_a(self):
        mask = tslib.expand_mask(tslib.PTX_XOR_SEED)
        scr = [tslib.xor_payload(p, mask) for p in self.pat_clear]
        res, ecb = self._run(scr)
        self.assertTrue(all(r["status"] == "invalid" for r in res["raw"]))
        self.assertTrue(all(r["status"] == "valid" for r in res["a"]))
        self.assertEqual(res["a"][0]["pat"]["programs"][1], PMT_PID)
        # Same PAT, different CC -> identical ciphertext.
        self.assertEqual(ecb["n"], len(scr))
        self.assertEqual(ecb["distinct"], 1)
        self.assertGreater(ecb["ccs"], 1)
        # Stuffing reveals the mask and the seed.
        masks = cardscramble.implied_masks(scr[0])
        self.assertEqual(masks[0][0], mask)
        self.assertEqual(masks[0][1], tslib.PTX_XOR_SEED)

    def test_scheme_a_other_seed(self):
        seed = bytes.fromhex("11223344")
        scr = [tslib.xor_payload(p, tslib.expand_mask(seed))
               for p in self.pat_clear]
        res, _ = self._run(scr)
        self.assertTrue(all(r["status"] == "invalid" for r in res["a"]))
        self.assertEqual(cardscramble.implied_masks(scr[0])[0][1], seed)

    def test_scheme_b(self):
        keys = tslib.W3peKeys(bytes.fromhex("133457799BBCDFF1"),
                              bytes.fromhex("0E329232EA6D0D73"),
                              bytes.fromhex("A5C35A3C"))
        scr = [tslib.w3pe_scramble(p, keys) for p in self.pat_clear]
        self.assertEqual(tslib.w3pe_descramble(scr[0], keys), self.pat_clear[0])
        res, ecb = self._run(scr, keys)
        self.assertTrue(all(r["status"] == "invalid" for r in res["raw"]))
        self.assertTrue(all(r["status"] == "invalid" for r in res["a"]))
        self.assertTrue(all(r["status"] == "valid" for r in res["b"]))
        self.assertEqual(ecb["distinct"], 1)
        # ECB: stuffing blocks repeat within the even and within the odd area.
        reps = cardscramble.block_repeats(scr[0])
        self.assertGreaterEqual(len(reps), 2)
        self.assertTrue(all(i < 16 for i in reps[0][1]) or
                        all(i >= 16 for i in reps[0][1]))

    def test_keyfile(self):
        path = os.path.join(os.environ.get("TSANALYSIS_TMP", "/nonexistent"),
                            "keys.txt")
        text = "# test\neven=133457799BBCDFF1\nodd = 0E329232EA6D0D73\nxor=a5c35a3c\n"
        try:
            with open(path, "w") as fh:
                fh.write(text)
        except OSError:
            self.skipTest("TSANALYSIS_TMP not set/writable")
        k = tslib.W3peKeys.load(path)
        self.assertEqual(k.xor_seed, bytes.fromhex("a5c35a3c"))

    def test_per_packet_variation_detected(self):
        mask = tslib.expand_mask(tslib.PTX_XOR_SEED)
        scr = []
        for p in self.pat_clear:
            cc = p[3] & 0xF
            m = bytes((x + cc) & 0xFF for x in mask)    # CC-dependent mask
            scr.append(tslib.xor_payload(p, m))
        _, ecb = self._run(scr)
        self.assertGreater(ecb["distinct"], 1)
        self.assertIn("CC", ecb["interp"])

    def test_truncated_hex(self):
        mask = tslib.expand_mask(tslib.PTX_XOR_SEED)
        full = tslib.xor_payload(self.pat_clear[0], mask)
        # The synthetic PAT section is 20 B, so 32 B hold it with its CRC.
        _, pkt = cardscramble.parse_hex_packet(full[:32].hex())
        res, _ = self._run([pkt])
        self.assertEqual(res["a"][0]["status"], "valid")
        # 16 B: header fields checkable, CRC not.
        _, pkt = cardscramble.parse_hex_packet(full[:16].hex())
        res, _ = self._run([pkt])
        self.assertEqual(res["a"][0]["status"], "plausible")

    def test_real_capture_sample(self):
        tag, pkt = cardscramble.parse_hex_packet(REAL_PAT32)
        self.assertEqual(tag, 0xC7)
        h = tslib.Header(pkt + bytes(TS_SIZE - len(pkt)))
        self.assertEqual((h.pid, h.pusi, h.tsc, h.afc), (0, 1, 0, 1))
        res, _ = self._run([pkt])
        self.assertEqual(res["raw"][0]["status"], "invalid")
        self.assertEqual(res["a"][0]["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
