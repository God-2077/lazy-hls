#!/usr/bin/env python3
"""核对 CODECS 探测结果与真实切片里的 SPS 是否一致。

启动时为了 master.m3u8 的 CODECS 属性，会「编 1 帧测试图案」来量 avc1 字符串
（见 server.measure_h264_codec_string）。这个脚本把探测结果和**真实切片**里的
SPS 对比，确认那条捷径没有量错东西。

用法：python tools/check_codec.py ["media/xxx.mp4"]
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as s  # noqa: E402


def sps_of_file(path: str):
    """从 mpegts 里找 SPS NAL（00 00 00 01 67 ...），返回 avc1.PPCCLL。"""
    data = open(path, "rb").read()
    for start in (b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67"):
        j = data.find(start)
        if j >= 0:
            p, c, lv = data[j + len(start):j + len(start) + 3]
            return f"avc1.{p:02x}{c:02x}{lv:02x}"
    return None


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        s.MEDIA_DIR, "sample-medium [h264 1920x1080 10m0s 122.0MB].mp4")
    s.SOURCE_FILE = src
    info = s.probe_source_info(src)
    s._prep_variants(info)
    print(f"源: {os.path.basename(src)}  {info['vcodec']} "
          f"{info['width']}x{info['height']} -> original="
          f"{'copy' if s.VARIANTS['original'].get('copy') else 'H.264 crf' + str(s.SOURCE_CRF)}")

    ok = True
    for name, cfg in s.VARIANTS.items():
        declared = cfg["codecs"]
        out = os.path.join(tempfile.gettempdir(), f"chk_{name}.ts")
        try:
            if cfg.get("copy"):
                cmd = s._cmd_copy(0.0, s.SEGMENT_DURATION, out)
            else:
                cmd = s._cmd_transcode(name, 0.0, s.SEGMENT_DURATION, out)
            subprocess.run(cmd, check=True, capture_output=True)
            real = sps_of_file(out)
        finally:
            if os.path.exists(out):
                os.remove(out)
        # 真实切片里是 H.264 才比得出（直通 AV1 源没有 SPS）
        if real is None:
            print(f"  [skip] {name:<14} 声明={declared}（切片不是 H.264，无 SPS 可比）")
            continue
        same = declared.split(",")[0] == real
        ok = ok and same
        print(f"  [{'OK  ' if same else 'FAIL'}] {name:<14} "
              f"声明={declared.split(',')[0]}  真实切片={real}"
              + ("" if same else "   <-- 不一致"))

    print("\n[check_codec] " + ("全部一致 ✅" if ok else "存在不一致 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
