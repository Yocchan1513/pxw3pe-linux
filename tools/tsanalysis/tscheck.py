#!/usr/bin/env python3
"""tscheck.py -- sanity-check a captured MPEG-TS (PX-W3PE bring-up aid).

Usage:
    python3 -I tools/tsanalysis/tscheck.py FILE.ts [--tag-ok] [--max-packets N]
                                                   [--tags 47,C7] [--quiet-pids]

Accepts the standard sync byte 0x47 and the PX-W3PE bridge source tag 0xC7
(both normalised to 0x47), resyncs on misalignment, and reports:
  packet count, PCR-based duration/bitrate, TEI count, per-PID counts,
  continuity-counter errors, transport_scrambling_control ratio, PAT/PMT
  (only when the payload parses with a valid CRC32), and a verdict:
  "payload looks clear" / "card-scrambled" / "broadcast-scrambled".

Without --tag-ok, packets whose first byte is not 0x47 are reported as a
warning (the bridge tag must be restored to 0x47 before handing the TS to
user space). --tag-ok declares the tags expected and silences that warning.

Exit status: 0 = no anomalies, 1 = anomalies found (sync loss, TEI, CC
errors, card-scrambled or unparsable PSI), 2 = usage / I/O error.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for -I

import tslib  # noqa: E402
from tslib import TS_SIZE, NULL_PID, PCR_WRAP, Header  # noqa: E402


class PidStats:
    __slots__ = ("count", "tsc", "cc_errors", "last_cc", "dup", "tei",
                 "payload_pkts", "pusi")

    def __init__(self):
        self.count = 0
        self.tsc = [0, 0, 0, 0]   # counts per transport_scrambling_control
        self.cc_errors = 0
        self.last_cc = None
        self.dup = 0              # consecutive duplicates seen
        self.tei = 0
        self.payload_pkts = 0
        self.pusi = 0


def _cc_check(ps, h):
    """Continuity-counter rules (ISO/IEC 13818-1 2.4.3.3):
    - CC increments (mod 16) only on packets carrying payload;
    - packets without payload must repeat the previous CC;
    - one duplicate packet (same CC, with payload) is allowed;
    - discontinuity_indicator=1 in the adaptation field resets checking.
    Returns 1 if this packet is a CC error, else 0."""
    err = 0
    if ps.last_cc is not None and not h.disc:
        if h.has_payload:
            if h.cc == ps.last_cc:
                ps.dup += 1
                if ps.dup > 1:
                    err = 1
            elif h.cc != ((ps.last_cc + 1) & 0xF):
                err = 1
                ps.dup = 0
            else:
                ps.dup = 0
        else:
            if h.cc != ps.last_cc:
                err = 1
    else:
        ps.dup = 0
    ps.last_cc = h.cc
    return err


def analyze(fileobj, syncs=tslib.DEFAULT_SYNCS, max_packets=None):
    """Analyse a TS from a binary file object. Returns a result dict."""
    rs = tslib.ReaderStats()
    pids = {}
    tei_total = 0
    af_bad = 0
    pcr_pid = None
    pcr_first = pcr_last = None     # (packet_index, pcr)
    pcr_elapsed = 0                 # accumulated 27 MHz ticks (wrap-safe)
    pcr_backjumps = 0

    # PSI bookkeeping (only clear, error-free packets are assembled).
    asm = {0: tslib.SectionAssembler()}
    pmt_pids = {}                   # pmt pid -> program number
    pat = None
    pmts = {}                       # program -> parsed PMT
    sec_ok = {}                     # pid -> valid section count
    sec_bad = {}                    # pid -> (count, last reason)

    idx = -1
    for tag, pkt in tslib.iter_packets(fileobj, syncs=syncs, stats=rs,
                                       max_packets=max_packets):
        idx += 1
        h = Header(pkt)
        ps = pids.get(h.pid)
        if ps is None:
            ps = pids[h.pid] = PidStats()
        ps.count += 1
        ps.tsc[h.tsc] += 1
        if h.af_bad:
            af_bad += 1
        if h.tei:
            tei_total += 1
            ps.tei += 1
            # Header itself may be corrupt: skip CC/PSI/PCR use.
            if h.pid in asm:
                asm[h.pid].reset()
            continue
        if h.has_payload:
            ps.payload_pkts += 1
            ps.pusi += h.pusi

        cc_err = 0
        if h.pid != NULL_PID:
            cc_err = _cc_check(ps, h)
            ps.cc_errors += cc_err

        # PCR: lock onto the first PID that carries one. The adaptation
        # field lies in the card-scrambled area, so on a card-scrambled
        # capture PCRs are garbage; jumps > 10 s are counted and skipped.
        if h.pcr is not None and not h.af_bad:
            if pcr_pid is None:
                pcr_pid = h.pid
            if h.pid == pcr_pid:
                if pcr_last is not None:
                    d = (h.pcr - pcr_last[1]) % PCR_WRAP
                    if d > 27_000_000 * 10:   # >10 s: treat as discontinuity
                        pcr_backjumps += 1
                    else:
                        pcr_elapsed += d
                else:
                    pcr_first = (idx, h.pcr)
                pcr_last = (idx, h.pcr)

        # PSI assembly (PAT and PMTs), clear packets only.
        a = asm.get(h.pid)
        if a is not None:
            if cc_err or h.disc:
                a.reset()
            if h.tsc != 0 or not h.has_payload or h.af_bad:
                continue
            payload = pkt[h.payload_off:]
            for sec in a.push(payload, h.pusi):
                ok, why = tslib.check_section(sec)
                expect_tid = 0x00 if h.pid == 0 else 0x02
                if ok and sec[0] != expect_tid:
                    ok, why = False, "unexpected table_id 0x%02X" % sec[0]
                if not ok:
                    n, _ = sec_bad.get(h.pid, (0, ""))
                    sec_bad[h.pid] = (n + 1, why)
                    continue
                sec_ok[h.pid] = sec_ok.get(h.pid, 0) + 1
                if h.pid == 0:
                    pat = tslib.parse_pat(sec)
                    for pn, ppid in pat["programs"].items():
                        if pn != 0 and ppid not in asm:
                            asm[ppid] = tslib.SectionAssembler()
                            pmt_pids[ppid] = pn
                else:
                    pmt = tslib.parse_pmt(sec)
                    pmts[pmt["program"]] = pmt

    duration = bitrate = None
    if pcr_first is not None and pcr_elapsed > 0:
        duration = pcr_elapsed / 27_000_000
        bitrate = (pcr_last[0] - pcr_first[0]) * TS_SIZE * 8 / duration

    return {
        "reader": rs, "pids": pids, "tei": tei_total, "af_bad": af_bad,
        "pcr_pid": pcr_pid, "duration": duration, "bitrate": bitrate,
        "pcr_backjumps": pcr_backjumps, "pat": pat, "pmts": pmts,
        "pmt_pids": pmt_pids, "sec_ok": sec_ok, "sec_bad": sec_bad,
        "verdict": _verdict(pids, pat, pmts, pmt_pids, sec_ok, sec_bad),
    }


def _verdict(pids, pat, pmts, pmt_pids, sec_ok, sec_bad):
    """Classify the payload state.

    card-scrambled     : PID 0 present with TSC=0, but no PAT section
                         ever passes table_id/syntax/CRC checks.
    broadcast-scrambled: PSI is clear, but elementary-stream PIDs carry
                         TSC != 0 (MULTI2 -- not handled here).
    payload looks clear: PSI valid and no (or negligible) TSC != 0.
    """
    p0 = pids.get(0)
    if p0 is None or p0.count == 0:
        return ("unknown", "no PID 0 (PAT) packets")
    if pat is None:
        clear0 = p0.tsc[0]
        if clear0 == 0:
            return ("unknown", "all PID 0 packets have TSC != 0 (unexpected)")
        why = sec_bad.get(0, (0, "no section start found"))[1]
        return ("card-scrambled",
                "PID 0 has %d clear-TSC packets but no valid PAT (%s)"
                % (clear0, why))
    scr = []
    for pid, ps in pids.items():
        if pid in (0, NULL_PID) or pid in pmt_pids:
            continue
        s = ps.count - ps.tsc[0]
        if ps.count and s / ps.count > 0.5:
            scr.append(pid)
    missing = [pp for pp in pmt_pids if pp in pids and sec_ok.get(pp, 0) == 0]
    if scr:
        return ("broadcast-scrambled",
                "PAT/PMT valid; %d PID(s) mostly TSC != 0: %s"
                % (len(scr), ", ".join("0x%04X" % p for p in sorted(scr))))
    note = "PAT valid"
    if pmts:
        note += ", %d PMT(s) valid" % len(pmts)
    if missing:
        note += "; PMT PID(s) seen but never valid: " + \
            ", ".join("0x%04X" % p for p in missing)
    return ("payload looks clear", note)


def report(res, tag_ok=False, quiet_pids=False, out=sys.stdout):
    rs = res["reader"]
    w = out.write
    anomalies = 0
    w("packets           : %d\n" % rs.packets)
    tags = ", ".join("%s x%d" % (tslib.TAG_NAMES.get(t, "0x%02X" % t), n)
                     for t, n in sorted(rs.tags.items()))
    w("first-byte tags   : %s\n" % (tags or "-"))
    if any(t != tslib.SYNC for t in rs.tags) and not tag_ok:
        w("WARNING           : non-0x47 first bytes present (bridge tags not "
          "normalised); use --tag-ok if expected\n")
        anomalies += 1
    w("sync              : leading skip %d B, resyncs %d, skipped %d B, "
      "trailing %d B\n" % (rs.leading_skip, rs.resyncs, rs.skipped_bytes,
                           rs.trailing_bytes))
    if rs.resyncs:
        anomalies += 1
    if res["pcr_pid"] is None:
        w("PCR               : none found (scrambled adaptation field?)\n")
    elif res["duration"] is None:
        w("PCR               : PID 0x%04X, too few PCRs\n" % res["pcr_pid"])
    else:
        w("PCR               : PID 0x%04X, duration %.3f s, bitrate %.3f Mbps"
          "%s\n" % (res["pcr_pid"], res["duration"], res["bitrate"] / 1e6,
                    ", %d jumps ignored" % res["pcr_backjumps"]
                    if res["pcr_backjumps"] else ""))
    w("TEI               : %d\n" % res["tei"])
    if res["af_bad"]:
        w("bad adapt. field  : %d packets (illegal adaptation_field_length)\n"
          % res["af_bad"])
    if res["tei"]:
        anomalies += 1

    pids = res["pids"]
    cc_total = sum(p.cc_errors for p in pids.values())
    w("CC errors (total) : %d\n" % cc_total)
    if cc_total:
        anomalies += 1
    if not quiet_pids:
        w("\n  PID     packets  %total  CC-err  TEI  TSC!=0  role\n")
        roles = {0: "PAT", 0x1FFF: "null", 0x10: "NIT", 0x11: "SDT",
                 0x12: "EIT", 0x14: "TDT/TOT", 0x24: "BIT"}
        for pp, pn in res["pmt_pids"].items():
            roles[pp] = "PMT prog %d" % pn
        for pmt in res["pmts"].values():
            for st, epid in pmt["streams"]:
                roles.setdefault(epid, tslib.STREAM_TYPES.get(
                    st, "stream_type 0x%02X" % st))
            roles.setdefault(pmt["pcr_pid"], "PCR")
        for pid in sorted(pids):
            p = pids[pid]
            scr = p.count - p.tsc[0]
            w("  0x%04X %8d  %5.1f%%  %6d  %3d  %5.1f%%  %s\n" % (
                pid, p.count, 100.0 * p.count / max(rs.packets, 1),
                p.cc_errors, p.tei, 100.0 * scr / p.count,
                roles.get(pid, "")))
        w("\n")

    pat = res["pat"]
    if pat:
        w("PAT               : tsid 0x%04X ver %d, %s\n" % (
            pat["tsid"], pat["version"], ", ".join(
                ("prog %d -> PMT 0x%04X" if pn else "prog %d -> NIT 0x%04X")
                % (pn, pid) for pn, pid in sorted(pat["programs"].items()))))
    for pn, pmt in sorted(res["pmts"].items()):
        w("PMT prog %-8d : ver %d, PCR 0x%04X, %s\n" % (
            pn, pmt["version"], pmt["pcr_pid"], ", ".join(
                "0x%04X:%s(0x%02X)" % (pid, tslib.STREAM_TYPES.get(st, "?"), st)
                for st, pid in pmt["streams"])))
    for pid, (n, why) in sorted(res["sec_bad"].items()):
        w("invalid sections  : PID 0x%04X x%d (last: %s)\n" % (pid, n, why))

    v, why = res["verdict"]
    if v in ("card-scrambled", "unknown"):
        anomalies += 1
    w("VERDICT           : %s -- %s\n" % (v, why))
    return anomalies


def parse_tags(s):
    vals = set()
    for t in s.split(","):
        t = t.strip()
        if t:
            vals.add(int(t, 16))
    return frozenset(vals)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("file", help="captured TS file ('-' for stdin)")
    ap.add_argument("--tag-ok", action="store_true",
                    help="bridge source tags (0xC7) are expected; no warning")
    ap.add_argument("--tags", default="47,C7",
                    help="accepted first-byte values, hex, comma-separated "
                         "(default 47,C7)")
    ap.add_argument("--max-packets", type=int, default=None)
    ap.add_argument("--quiet-pids", action="store_true",
                    help="omit the per-PID table")
    a = ap.parse_args(argv)
    try:
        syncs = parse_tags(a.tags)
        f = sys.stdin.buffer if a.file == "-" else open(a.file, "rb")
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    with f:
        res = analyze(f, syncs=syncs, max_packets=a.max_packets)
    n = report(res, tag_ok=a.tag_ok, quiet_pids=a.quiet_pids)
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
