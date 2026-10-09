#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Generate Mirakurun channels.yml entries for BS/CS from the NIT in recorded TS files.

recisdb in V4L-DVB mode selects the ISDB-S transport stream by TSID, so every
BS/CS channel gets `commandVars.tsidArg: --tsid <TSID>` (see tuners.yml).

Usage:
  recisdb tune -i /dev/dvb/adapter0/frontend0 -c BS15_0 --tsid 16625 -t 15 bs.ts
  recisdb tune -i /dev/dvb/adapter1/frontend0 -c CS2 -t 15 cs.ts
  python3 nit2channels.py bs.ts cs.ts > bscs-channels.yml
"""
import sys

BS_NID, CS_NIDS = 4, (6, 7)


def sections(data, pid):
    out, buf = [], None

    def flush():
        nonlocal buf
        if buf is not None and len(buf) >= 3:
            n = ((buf[1] & 0xf) << 8 | buf[2]) + 3
            if len(buf) >= n:
                out.append(bytes(buf[:n]))
                buf = None

    for i in range(len(data) // 188):
        p = data[i * 188:(i + 1) * 188]
        if p[0] != 0x47 or ((p[1] & 0x1f) << 8 | p[2]) != pid:
            continue
        o = 4 + (1 + p[4] if p[3] & 0x20 else 0)
        if p[1] & 0x40:
            ptr = p[o]
            o += 1
            if buf is not None:
                buf += p[o:o + ptr]
                flush()
            buf = bytearray(p[o + ptr:])
        elif buf is not None:
            buf += p[o:]
        flush()
    return out


def nit_entries(paths):
    """{tsid: (network_id, frequency_GHz)} from NIT actual/other (table 0x40/0x41)."""
    res = {}
    for path in paths:
        for s in sections(open(path, "rb").read(), 0x10):
            if s[0] not in (0x40, 0x41):
                continue
            nid = s[3] << 8 | s[4]
            o = 10 + (((s[8] & 0xf) << 8) | s[9])
            end = o + 2 + (((s[o] & 0xf) << 8) | s[o + 1])
            o += 2
            while o < end:
                tsid = s[o] << 8 | s[o + 1]
                q = o + 6
                qe = q + (((s[o + 4] & 0xf) << 8) | s[o + 5])
                while q < qe:
                    if s[q] == 0x43:  # satellite_delivery_system_descriptor
                        res[tsid] = (nid, int(s[q + 2:q + 6].hex()) / 100000)
                    q += 2 + s[q + 1]
                o = qe
    return res


def main():
    by_name = {}
    for tsid, (nid, ghz) in nit_entries(sys.argv[1:]).items():
        if nid == BS_NID:
            name = "BS%d" % (round((ghz - 11.72748) / 0.03836) * 2 + 1)
        elif nid in CS_NIDS:
            name = "CS%d" % (round((ghz - 12.291) / 0.04) * 2 + 2)
        else:
            continue
        by_name.setdefault(name, []).append(tsid)

    def key(n):
        return (n[:2], int(n[2:]))

    for name in sorted(by_name, key=key):
        tsids = sorted(by_name[name])
        for idx, tsid in enumerate(tsids):
            ch = "%s_%d" % (name, idx) if name.startswith("BS") else name
            print("- name: %s\n  type: %s\n  channel: %s\n  commandVars:\n    tsidArg: --tsid %d\n"
                  % (ch, name[:2], ch, tsid))


if __name__ == "__main__":
    main()
