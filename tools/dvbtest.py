#!/usr/bin/env python3
"""DVB の動作確認用: recisdb と同じ手順で ISDB-T を選局し、dvr から TS を保存する。

  python3 -I tools/dvbtest.py --adapter 0 --channel 27 --seconds 30 out.ts

手順(docs/04 7 章の recisdb の動き): frontend を O_RDWR で開く → FE_GET_INFO →
FE_SET_PROPERTY(DELIVERY_SYSTEM=ISDBT, FREQUENCY[Hz], TUNE)→ FE_READ_STATUS で
最大 6 秒ロックを待つ → demux に PID 0x2000 / DMX_OUT_TS_TAP / IMMEDIATE_START →
dvr0 を読む。

ioctl 番号と構造体の大きさは、/usr/include/linux/dvb のヘッダから C で出力した値
(x86-64、2026-10-14)。
"""
import argparse
import fcntl
import os
import select
import struct
import sys
import time

FE_GET_INFO = 0x80A86F3D          # sizeof(dvb_frontend_info) = 168
FE_READ_STATUS = 0x80046F45
FE_SET_PROPERTY = 0x40106F52      # sizeof(dtv_properties) = 16
FE_GET_PROPERTY = 0x80106F53
DMX_SET_PES_FILTER = 0x40146F2C   # sizeof(dmx_pes_filter_params) = 20
DMX_SET_BUFFER_SIZE = 0x6F2D
DMX_STOP = 0x6F2A

DTV_TUNE, DTV_FREQUENCY, DTV_DELIVERY_SYSTEM, DTV_STAT_CNR = 1, 3, 17, 63
SYS_ISDBT = 8
FE_HAS_LOCK = 0x10
DMX_IN_FRONTEND, DMX_OUT_TS_TAP, DMX_PES_OTHER, DMX_IMMEDIATE_START = 0, 2, 20, 4
PROP_SIZE = 76                    # sizeof(dtv_property), u at 16, result at 72


def uhf_hz(ch):
    """UHF 13〜62ch の中心周波数(Hz)。473.142857 MHz + 6 MHz × (ch - 13)"""
    return 473142857 + 6000000 * (ch - 13)


def props(*pairs):
    """(cmd, data) の列から dtv_property の配列(packed)を作る。"""
    buf = bytearray(PROP_SIZE * len(pairs))
    for i, (cmd, data) in enumerate(pairs):
        struct.pack_into("<I", buf, i * PROP_SIZE, cmd)
        struct.pack_into("<I", buf, i * PROP_SIZE + 16, data)
    return buf


def set_props(fd, *pairs):
    arr = props(*pairs)
    import ctypes
    cbuf = (ctypes.c_char * len(arr)).from_buffer(arr)
    fcntl.ioctl(fd, FE_SET_PROPERTY, struct.pack("<I4xQ", len(pairs), ctypes.addressof(cbuf)))


def get_cnr(fd):
    """DTV_STAT_CNR(FE_SCALE_DECIBEL, 0.001 dB)。取れなければ None。"""
    import ctypes
    arr = props((DTV_STAT_CNR, 0))
    cbuf = (ctypes.c_char * len(arr)).from_buffer(arr)
    fcntl.ioctl(fd, FE_GET_PROPERTY, struct.pack("<I4xQ", 1, ctypes.addressof(cbuf)))
    # u.st: len(u8) at 16, stat[0] = scale(u8) at 17, svalue(s64) at 18
    length, scale = arr[16], arr[17]
    if length < 1 or scale != 1:        # FE_SCALE_DECIBEL = 1
        return None
    return struct.unpack_from("<q", arr, 18)[0] / 1000.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", type=int, default=0)
    ap.add_argument("--channel", type=int, default=27, help="UHF 物理チャンネル(13〜62)")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("out")
    a = ap.parse_args()
    base = f"/dev/dvb/adapter{a.adapter}"
    hz = uhf_hz(a.channel)

    fe = os.open(f"{base}/frontend0", os.O_RDWR)
    info = bytearray(168)
    fcntl.ioctl(fe, FE_GET_INFO, info)
    print("frontend:", info[:128].split(b"\0")[0].decode(), f"/ tune UHF {a.channel}ch = {hz} Hz")

    t0 = time.monotonic()
    set_props(fe, (DTV_DELIVERY_SYSTEM, SYS_ISDBT), (DTV_FREQUENCY, hz), (DTV_TUNE, 0))
    status = 0
    while time.monotonic() - t0 < 6.0:
        st = bytearray(4)
        fcntl.ioctl(fe, FE_READ_STATUS, st)
        status = struct.unpack("<I", st)[0]
        if status & FE_HAS_LOCK:
            break
        time.sleep(0.1)
    if not status & FE_HAS_LOCK:
        print(f"NO LOCK within 6 s (status=0x{status:02x})")
        os.close(fe)
        return 1
    print(f"LOCK after {time.monotonic() - t0:.2f} s (status=0x{status:02x})")
    time.sleep(0.6)                     # C/N register settles ~0.4 s after lock
    cnr = get_cnr(fe)
    print("C/N:", f"{cnr:.2f} dB" if cnr is not None else "n/a")

    dmx = os.open(f"{base}/demux0", os.O_RDWR)
    fcntl.ioctl(dmx, DMX_SET_BUFFER_SIZE, 8 * 1024 * 1024)
    fcntl.ioctl(dmx, DMX_SET_PES_FILTER,
                struct.pack("<H2xIIII", 0x2000, DMX_IN_FRONTEND, DMX_OUT_TS_TAP,
                            DMX_PES_OTHER, DMX_IMMEDIATE_START))
    dvr = os.open(f"{base}/dvr0", os.O_RDONLY | os.O_NONBLOCK)
    total, t1 = 0, time.monotonic()
    with open(a.out, "wb") as f:
        while time.monotonic() - t1 < a.seconds:
            if not select.select([dvr], [], [], 0.2)[0]:
                continue
            try:
                b = os.read(dvr, 188 * 1024)
            except BlockingIOError:
                continue
            except OSError as e:            # EOVERFLOW: dvr ring overflowed (docs/04 X6)
                print("dvr read error:", e)
                break
            f.write(b)
            total += len(b)
    dt = time.monotonic() - t1
    fcntl.ioctl(dmx, DMX_STOP)
    for fd in (dvr, dmx, fe):
        os.close(fd)
    print(f"read {total} bytes in {dt:.1f} s = {total * 8 / dt / 1e6:.2f} Mbps -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
