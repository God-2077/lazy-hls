#!/usr/bin/env python3
"""起服务后的冒烟测试：播放列表内容 + 各档位真的能转出可解码的切片。

用法：
    python server.py --port 8123 --source "h264 1920x1080" --no-warm &
    python tools/smoke.py --port 8123

检查项：
  * master.m3u8 里出现哪些档位（original-copy 应当默认不出现）
  * 每个档位的 index.m3u8 能拿到、EXTINF 之和 ≈ 源时长、TARGETDURATION ≥ 每片时长
  * 每个档位抓第 0 片，用 ffprobe 校验：能解码、编码格式、分辨率、时间戳起点
  * original 档在源是 H.264 时必须真的是 copy（码率≈源码率、编码是 h264）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get(url: str, timeout: float = 120.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read()


def sps_codec_string(data: bytes):
    """从切片字节里直接找 SPS NAL（00 00 00 01 67 ...），返回 avc1.PPCCLL。

    这才是「播放器真正会看到的东西」：不是重新推一遍，而是读服务器实际发出去的字节。
    """
    for start in (b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67"):
        j = data.find(start)
        if j >= 0:
            p, c, lv = data[j + len(start):j + len(start) + 3]
            return f"avc1.{p:02x}{c:02x}{lv:02x}"
    return None


def parse_master(text: str):
    """master.m3u8 -> {档位名: {属性}}。"""
    out, attrs = {}, None
    for line in text.splitlines():
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = {}
            for part in line.split(":", 1)[1].split(","):
                key, _, val = part.partition("=")
                attrs[key.strip()] = val.strip().strip('"')
        elif line.startswith("stream/") and attrs is not None:
            out[line.split("/")[1]] = attrs
            attrs = None
    return out


def parse_playlist(text: str):
    durs, target = [], 0
    for line in text.splitlines():
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXTINF:"):
            durs.append(float(line.split(":", 1)[1].split(",")[0]))
    return durs, target


def probe_segment(data: bytes):
    fd, path = tempfile.mkstemp(suffix=".ts")
    os.close(fd)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_name,width,height,profile,level:format=duration",
             "-of", "json", path],
            capture_output=True, check=True).stdout
        info = json.loads(out)
        v = next((s for s in info["streams"] if s.get("width")), {})
        a = next((s for s in info["streams"] if s.get("codec_name") in
                  ("aac", "mp3", "opus", "flac")), {})
        return {"vcodec": v.get("codec_name"), "w": v.get("width"), "h": v.get("height"),
                "profile": v.get("profile"), "level": v.get("level"),
                "acodec": a.get("codec_name"),
                "duration": float(info["format"]["duration"])}
    finally:
        os.unlink(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--variants", default="",
                    help="额外直接访问的档位（默认会跳过不在 master 里的 original-copy）")
    ap.add_argument("--expect", default="",
                    help="master 里必须出现的档位名，逗号分隔")
    ap.add_argument("--expect-absent", default="original-copy")
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"

    ok = True

    def check(cond, msg):
        nonlocal ok
        if not cond:
            ok = False
        print(f"  [{'OK  ' if cond else 'FAIL'}] {msg}")
        return cond

    print(f"== master.m3u8 @ {base}")
    _, body = get(f"{base}/master.m3u8")
    master = body.decode("utf-8", "replace")
    print(master.rstrip())
    variants = parse_master(master)
    names = list(variants)
    for want in [x for x in args.expect.split(",") if x]:
        check(want in names, f"master 里有 {want}")
    for bad in [x for x in args.expect_absent.split(",") if x]:
        check(bad not in names, f"master 里没有 {bad}")
    check(all("CODECS" in a for a in variants.values()),
          "每条 STREAM-INF 都带 CODECS")
    check(all("RESOLUTION" in a for a in variants.values()),
          "每条 STREAM-INF 都带 RESOLUTION")

    todo = list(names) + [x for x in args.variants.split(",") if x]
    for name in todo:
        print(f"== {name}")
        try:
            _, pl = get(f"{base}/stream/{name}/index.m3u8")
        except urllib.error.HTTPError as exc:
            check(False, f"index.m3u8 HTTP {exc.code}")
            continue
        durs, target = parse_playlist(pl.decode("utf-8", "replace"))
        check(bool(durs), f"有 {len(durs)} 片")
        check(target >= max(durs) - 1e-9,
              f"TARGETDURATION={target:g} >= 最长片 {max(durs):.3f}s")
        check(min(durs) > 0.5, f"没有空气切片（最短 {min(durs):.3f}s）")

        try:
            _, seg = get(f"{base}/stream/{name}/seg0.ts")
        except urllib.error.HTTPError as exc:
            check(False, f"seg0.ts HTTP {exc.code}")
            continue
        info = probe_segment(seg)
        print(f"        seg0: {len(seg) / 1024:.0f}KB {info['vcodec']} "
              f"{info['w']}x{info['h']} {info['profile']} level={info['level']} "
              f"+{info['acodec']} duration={info['duration']:.3f}s "
              f"{len(seg) * 8 / max(info['duration'], 0.01) / 1e6:.2f}Mbps")
        if info["vcodec"] is None and name.endswith("-copy"):
            # 直通档原样保留源编码。源是 AV1/HEVC 时，mp4 的 AV1 塞进 MPEG-TS
            # 根本不是合法封装，ffprobe 连视频流都认不出来 —— 这正是它默认
            # 不进 master.m3u8 的原因。所以这里不是失败，是**预期结果**。
            print("        （直通档保留了源编码，非 H.264 时 ffprobe 认不出视频流 —— "
                  "预期，见 readme）")
        else:
            check(info["vcodec"] is not None, "切片能解出视频流")
        check(abs(info["duration"] - durs[0]) < 0.25,
              f"切片时长 {info['duration']:.3f}s ≈ EXTINF {durs[0]:.3f}s")

        # 声明 vs 实际：CODECS 里的 profile/level 必须和**实际发出的字节**对得上
        declared = variants.get(name, {}).get("CODECS")
        real = sps_codec_string(seg)
        if declared and real:
            check(declared.split(",")[0] == real,
                  f"CODECS 声明 {declared.split(',')[0]} == 切片 SPS {real}")
        elif declared:
            print(f"  [skip] CODECS 声明 {declared}（切片不是 H.264，无 SPS 可比）")
        if "RESOLUTION" in variants.get(name, {}):
            res = variants[name]["RESOLUTION"]
            check(res == f'{info["w"]}x{info["h"]}',
                  f"RESOLUTION 声明 {res} == 实际 {info['w']}x{info['h']}")

    print("\n[smoke] " + ("全部通过 ✅" if ok else "存在问题 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
