#!/usr/bin/env python3
"""
按需（Lazy / Just-In-Time）HLS 服务端测试，研究更好的实现方式
=====================================

核心思路
--------
传统做法：先 `ffmpeg -f hls` 把整个视频切成 n 个 .ts 再对外提供播放列表。
本方案：**播放列表（m3u8）是纯元数据，可以先算出来立刻返回**，
        而每个 .ts 切片等到客户端真正请求它时才用 ffmpeg 现场转码生成，并落盘缓存。

    1. 客户端 GET /master.m3u8             -> 直接返回 master 播放列表（瞬时）
    2. 客户端 GET /stream/720p/index.m3u8  -> ffprobe 拿时长 -> 算出切片数 -> 返回播放列表（<50ms）
    3. 客户端 GET /stream/720p/seg3.ts     -> 未命中缓存则 ffmpeg -ss/-t 只转这 6 秒 -> 返回并缓存
    4. 客户端 GET /stream/720p/seg3.ts     -> 命中缓存 -> 直接读文件返回（毫秒级）

这样首帧时间只取决于“第一个切片”的转码时间，而不是整个视频的转码时间。

本轮实现（readme 里的 1 / 2 两条）
----------------------------------
* **尾片合并（第 2 条）**：视频时长几乎不可能是切片时长的整数倍
  （实测 600.016083s / 6s），按 ceil 切会多出一个 0.016s 的“空气切片”。
  现在尾片不足 `MIN_TAIL_DURATION` 时并入前一片：既没有空片，
  播放列表 EXTINF 之和也仍然等于真实时长。
* **预生成 / prefetch（第 1 条）**：请求第 i 片时顺手把后面
  `PREFETCH_AHEAD_SECONDS` 秒的切片丢给后台线程池先转好，播到那片直接命中缓存；
  请求播放列表时预热第 0 片，所以点开就能播。
  预取线程不排队等待（抢不到锁就跳过）、请求到来时会剪掉过期任务（seek 之后
  前面排的预取任务就没意义了）。

本轮顺手修掉的坑 / 优化
-----------------------
* **`-force_key_frames "expr:gte(t,0)"` 是个大坑**：`gte(t,0)` 对每一帧都成立，
  等于把每片都编成 all-intra。实测 144 帧 = 144 个关键帧，
  同样是 CRF23，all-intra 比正常 GOP 多花 25% 比特。
  现在改成 `expr:gte(t,n_forced*SEG)`（只在片边界强制关键帧）。
* **关键帧扫描用 packet 而不是 frame**：`-skip_frame nokey -show_entries frame=`
  要解析到帧层，10 分钟 1080p 源实测 3189ms；改成 `-show_entries packet=pts_time,flags`
  只要 330ms（快约 10 倍），结果完全一致。
* **VBV 超发**：每片都是独立的 ffmpeg 进程、每个进程的 VBV 缓冲都是满的，
  于是每片开头都会“超发”比特。实测 `-bufsize` 取 2×码率时，6s 的 720p 片
  实际 2.96 Mbps（标称 2.5 Mbps，超 18%），取 1×码率后降到 2.60 Mbps。
  所以 `VBV_FACTOR = 1.0`，master.m3u8 里声明的 BANDWIDTH 也按
  `(视频码率 + 音频码率) × 1.15` 估算，宁可比实际高一点。

档位说明
--------
档位表**按源文件分辨率在启动时生成**（`_build_variants` / `_prep_variants`），不是写死的：

* `original`：**输出 H.264**，分辨率跟随源。
  源本来就是 H.264 -> 直接 `-c copy`，无损且零 CPU；
  源是 AV1/HEVC -> libx264 重编成 H.264（`crf 18`，不缩分辨率）。
* `original-copy`：原编码直通（`-c copy`），一帧不重编，编码格式原样保留。
  默认**不进 master.m3u8**（AV1/HEVC 装进 MPEG-TS 大部分播放器放不出来），
  单独打 `/stream/original-copy/index.m3u8` 可用，`--advertise-copy` 可列出来。
* `360p` / `480p` / `720p` / `1080p` / `2160p`：libx264 重编，固定 6s 一片，
  首帧强制关键帧，可独立解码。**源分辨率以上的自动隐藏**（1080p 源不给 4K 档，
  放大不会变清楚，只会多花几倍流量和 CPU），`--allow-upscale` 可强行打开。

copy 档的切片边界按源文件真实关键帧切分（copy 模式无法强制关键帧，只能用现成的），
所以 EXTINF 用的是实际时长而不是固定 6s；重编档不受这个限制。

`master.m3u8` 里的 `BANDWIDTH` 和 `CODECS` 都是**量出来的**：BANDWIDTH 按
(视频码率 + 音频码率) × 1.15 估，CODECS 直接编 1 帧从 SPS 里读（见
`measure_h264_codec_string`）。后者不是装饰 —— `avc1.PPCCLL` 里的 constraint 字节
推不出来，只能看编码器实际写了什么，我手推错过两次。

关键工程细节
------------
* `-ss` 放在 `-i` 之前 = 快速 seek（跳到目标位置之前最近的关键帧），比解码整段快几个数量级。
* `-output_ts_offset <start>` 把该切片的 PTS 偏移到全片时间轴上的正确位置，
  否则每个切片的时间戳都从 0 开始，播放器进度条/音画同步会乱。
* 每个切片是独立的 ffmpeg 进程、输出第一帧即关键帧，所以切片可独立解码、可随机 seek。
* 磁盘缓存 + 每切片一把锁：并发请求同一片时不会重复转码（缓存击穿保护）。

运行
----
    python3 server.py                       # 默认端口 8080
    python3 server.py --port 9000
    python3 server.py --source 1080         # 按关键字选 media/ 里的源文件
    python3 server.py --prefetch-ahead 0    # 关掉预生成，回到“纯按需”基线
    python3 server.py --max-height 1080     # 档位高度上限（默认跟随源分辨率）
    python3 server.py --selftest            # 只检查切片方案，不起服务

观测 / 对照实验
---------------
* `GET /stats`   -> JSON：请求数、命中、现转、预取命中率、TTFF、p95 延迟、缓存进度
* `GET /control?prefetch=0|1&ahead=18&reset=1` -> 运行时开关预取 / 重置统计
* `tools/bench.py` -> 模拟播放器的基准脚本（冷启动 TTFF、逐片延迟、是否卡顿）
* `tools/check_codec.py` -> 核对 master.m3u8 里的 CODECS 与真实切片的 SPS 是否一致
* `tools/smoke.py` -> 端到端冒烟：播放列表 + 每个档位抓一片下来解码校验
"""

from __future__ import annotations

import argparse
import glob
import heapq
import itertools
import json
import math
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

ROOT = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(ROOT, "media")
CACHE_DIR = os.path.join(ROOT, "cache")
# 单片时长（秒）。真实业务一般 4~10s，太短请求数爆炸，太长首帧慢。
SEGMENT_DURATION = 6.0

# 尾片短于这个值就并入前一片，避免出现 0.016s 这种“空气切片”。
# 0 表示不合并（保持老行为，方便对照）。
MIN_TAIL_DURATION = 1.0

# 预生成：请求某片时，把它后面这么多秒的切片提前转好（0 = 关闭）。
PREFETCH_AHEAD_SECONDS = 18.0

# 预取 worker 数。默认按核数给：核多给 2，小机器给 1。
# 实测在 CPU 吃紧的源上（AV1 1080p），2 个 worker + 请求路径 = 3 个 x264 并发会互相抢 CPU：
# 单片平均转码 2568ms -> 3374ms，最坏延迟 3367ms（1 个 worker 时是 2691ms / 2798ms）。
PREFETCH_WORKERS = max(1, min(2, (os.cpu_count() or 4) // 4))

# 每个切片一个独立 ffmpeg 进程时，VBV 缓冲倍数（相对视频码率）。
# 实测 2.0 会让每片实际码率超发约 18%，1.0 基本贴合标称码率。
VBV_FACTOR = 1.0

# x264 preset。首帧时间 = 第一个切片的转码时间，所以这个参数直接决定 TTFF。
# 实测（720p / 6s 片 / 2500k 上限 / 960x400 放大到 720p 的源，取 6~12s 片）：
#   veryfast  889ms  1.948MB  SSIM 0.9285   <- 默认，均衡
#   ultrafast 629ms  1.873MB  SSIM 0.9154   <- 快 29%，质量掉 0.013
#   veryfast + tune zerolatency  966ms 2.159MB  <- 反而更慢更胖，别用
# 注意：这个源的 SSIM 被“放大到 720p”这一步主导，编码器差异会被掩盖，
# 换成原生 720p 以上的源，码率/质量差异会比这里更明显。
PRESET = "veryfast"

# 关键帧策略：
#   "first" —— 只在片首强制一个关键帧（`expr:gte(t,n_forced*SEG)`），正常 GOP
#   "all"   —— `expr:gte(t,0)`：t>=0 恒真，等于每帧都是关键帧（all-intra）
# 实测（720p / 6s 片 / 同码率上限 / 同 SSIM）：
#   all-intra 每片省 ~16% 编码时间（620ms vs 740ms），但多花 ~6% 字节。
# 首帧时间就是这个项目的核心指标，所以两种都留着，默认 "first"（省流量）。
KEYFRAME_MODE = "first"

# `original` 档在「源不是 H.264、必须重编」时用的 CRF。
# 这一档的定位是「源文件画质 + H.264 兼容性」，所以给高质量的 18，
# 而不是压到某个码率上限（那正是固定档位干的事）。
SOURCE_CRF = 18

DEFAULT_SOURCE = "sample-medium [h264 960x400 47s 21.9MB].mp4"
DEFAULT_SOURCE = "sample-big [av1 1920x1080 23m36s 376.6MB].mp4"

# 输入源（由 main() 根据 --source 覆盖）
SOURCE_FILE = os.path.join(MEDIA_DIR, DEFAULT_SOURCE)

# 多码率档位。每个档位各自一条独立的切片/播放列表/缓存目录。
#
# 档位分三类：
#   original      —— **输出 H.264**，分辨率跟随源文件（源本来就是 H.264 的话直接 copy
#                    不重编，省 CPU 且无损；源是 AV1/HEVC 就转成 H.264）。
#   original-copy —— 原编码直通（`-c copy`，一帧不重编）。默认不进 master.m3u8：
#                    AV1/HEVC 塞进 MPEG-TS 大部分播放器放不出来，想用得单独打地址。
#   <高度>p       —— 固定档位，H.264 重编。启动时按源分辨率过滤（见 _build_variants）。
#
# 固定档位表（真正的 VARIANTS 由这张表按源分辨率生成，不是写死的）：
#   vb = 视频码率上限，ab = 音频码率。按像素数定标：360p 0.23MP / 720p 0.92MP
#   / 1080p 2.07MP / 4K 8.29MP。BANDWIDTH 不写死，启动时按 (vb+ab)×1.15 算。
# 4K 的 12000k 是实测定的：1080p 源放大到 3840x2160、6s 一片，
#   16000k -> 3358ms / 10.79 Mbps，12000k -> 3154ms / 8.12 Mbps（x264 只跑到 1.9x 实时）。
#   x264 在 4K 上编不出更高码率的意义不大（同一帧要 3 秒才编完，首帧时间已经很难看），
#   要真 4K 画质直接加 HEVC/AV1 档位，别指望 H.264。
LADDER = [
    {"name": "360p", "height": 360, "width": 640, "vb": "800k", "ab": "96k"},
    {"name": "480p", "height": 480, "width": 854, "vb": "1400k", "ab": "96k"},
    {"name": "720p", "height": 720, "width": 1280, "vb": "2500k", "ab": "128k"},
    {"name": "1080p", "height": 1080, "width": 1920, "vb": "5000k", "ab": "160k"},
    {"name": "2160p", "height": 2160, "width": 3840, "vb": "12000k", "ab": "192k"},
]

VARIANTS: dict = {}

# 源分辨率以上的固定档位自动隐藏（1080p 源就别放 4K 档了：放大不会变清楚，
# 只会多花几倍流量和 CPU）。--allow-upscale 可以强行打开，--max-height N 换个上限。
ALLOW_UPSCALE = False
MAX_HEIGHT = 0                     # 0 = 跟随源分辨率
ADVERTISE_COPY = False             # original-copy 是否列进 master.m3u8
WARM_VARIANTS = "original"         # 启动预热哪些档位（逗号分隔 / all / 空）
CODEC_PROBE = True                 # 启动时为每个档位编 1 帧，量出真实的 CODECS 字符串

# 源文件信息（启动时 ffprobe 填，见 probe_source_info）
SOURCE_INFO: dict = {}

# 运行时可变的开关（/control 会改它）
RUNTIME = {
    "prefetch": True,
    "ahead": PREFETCH_AHEAD_SECONDS,
}

# 进程内的小缓存：source 时长 / 切片方案 / 关键帧（真实项目可放 Redis）
CACHE: dict = {}
_CACHE_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def clear_cache():
    """清空缓存目录。"""
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    os.makedirs(CACHE_DIR, exist_ok=True)


def run(cmd, **kw):
    """执行命令，失败时抛出带 stderr 的异常。"""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"{proc.stderr.decode('utf-8', 'ignore')[-2000:]}"
        )
    return proc.stdout


def probe_duration(path: str) -> float:
    """用 ffprobe 读时长（秒）。播放列表就靠这个算出来。"""
    out = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ])
    return float(json.loads(out)["format"]["duration"])


def probe_source_info(path: str) -> dict:
    """读源文件的分辨率 / 码率 / 编码格式。

    分辨率用来过滤档位，编码格式决定 original 档是「直接 copy」还是「转成 H.264」：
    源已经是 H.264 时重编一遍纯属浪费（还掉画质），直接 copy 就是无损的 H.264 输出。
    """
    info = {"width": 0, "height": 0, "vbr": 0, "abr": 0,
            "vcodec": "", "acodec": "", "fps": 0.0, "level": 0, "profile": ""}

    try:
        out = run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,bit_rate,codec_name,level,profile,avg_frame_rate",
            "-of", "json", path,
        ])
        st = (json.loads(out).get("streams") or [{}])[0]
        info["width"] = int(st.get("width") or 0)
        info["height"] = int(st.get("height") or 0)
        info["vbr"] = int(st.get("bit_rate") or 0)
        info["vcodec"] = str(st.get("codec_name") or "")
        info["level"] = int(st.get("level") or 0)
        info["profile"] = str(st.get("profile") or "")
        num, _, den = str(st.get("avg_frame_rate") or "0/1").partition("/")
        try:
            info["fps"] = float(num) / float(den) if float(den) else 0.0
        except ValueError:
            info["fps"] = 0.0
    except Exception as exc:                       # noqa: BLE001
        print(f"[warn] probe video stream failed: {exc}")

    try:
        out = run([
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=bit_rate,codec_name",
            "-of", "json", path,
        ])
        st = (json.loads(out).get("streams") or [{}])[0]
        info["abr"] = int(st.get("bit_rate") or 0)
        info["acodec"] = str(st.get("codec_name") or "")
    except Exception:                              # noqa: BLE001
        pass

    return info


# H.264 level_idc 上限表：(level_idc, 最大宏块数/秒, 最大帧尺寸宏块数)
_H264_LEVELS = [
    (30, 40500, 1620), (31, 108000, 3600), (32, 216000, 5120),
    (40, 245760, 8192), (41, 245760, 8192), (42, 522240, 8704),
    (50, 589824, 22080), (51, 983040, 36864), (52, 2073600, 36864),
]

# ffprobe 的 profile 名 -> profile_idc。x264 用 -profile:v main 编出来就是 Main。
_H264_PROFILE_IDC = {
    "baseline": 0x42, "constrained baseline": 0x42,
    "main": 0x4d, "high": 0x64, "high 10": 0x6e,
}


def _h264_level_for(width: int, height: int, fps: float) -> int:
    """按宏块率 + 帧尺寸选一个够用的 level_idc。"""
    mb = (max(width, 16) // 16) * (max(height, 16) // 16)
    mbs = mb * (fps if fps > 0 else 30.0)
    for idc, max_mbs, max_fs in _H264_LEVELS:
        if mbs <= max_mbs and mb <= max_fs:
            return idc
    return 52


def measure_h264_codec_string(cmd: list, out_path: str, width: int, height: int,
                              fps: float, fallback: str) -> str:
    """把 `cmd` 的输入换成测试图案、只编 1 帧，从真实 SPS 里读出 CODECS 字符串。

    为什么值得多跑这一次 ffmpeg：`avc1.PPCCLL` 里的 PPCC 是「profile_idc +
    constraint_set_flags」，这个东西**推不出来**，只能看编码器实际写了什么：

        x264 -profile:v main        -> 4d 40（constraint_set1_flag=1）
        源文件是 High profile       -> 64 00
        源文件是 Constrained Baseline -> 42 c0

    我一开始就是按「Main 就是 4d40」手推的，结果 `original` 档在源是 H.264 时
    是直接 copy 的，流里是源自己的 High profile，声明成 Main 就是「声明与实际不符」。
    这里改成真编一帧来量，测出来的就是切片里真实会有的字节。

    改动 `cmd` 有三处，每处都踩过坑：
    * 测试图案的尺寸必须**和源文件一样** —— libx264 是按输入分辨率决定 level 的，
      用 64x64 试的话 4K 档也会声明成 level 1.0（真踩过）。
    * 只能删**输出侧**那个 `-f mpegts`：输入侧还有 `-f lavfi`，两个都删掉
      ffmpeg 就会把 "testsrc=..." 当文件名（真踩过）。
    * 输出路径必须按字符串匹配删掉，不能 `rest.pop()` —— 命令行最后一个参数
      是 `-muxpreload 0` 的 `0` 而不是路径，pop 掉它就变成
      `Expected number for muxpreload but found: -frames:v`（也真踩过）。

    读不到就退回 `fallback`（按分辨率算出来的 profile/level）。
    """
    try:
        i = cmd.index("-i")
    except ValueError:
        return fallback
    src = (f"testsrc=size={max(int(width), 16)}x{max(int(height), 16)}:"
           f"rate={fps if fps > 0 else 24:.6g}:duration={max(1.0, 1.0 / (fps or 24)):.6g}")
    test = list(cmd[:i])                       # ... -ss x -t y
    test += ["-f", "lavfi", "-i", src]
    rest = [a for a in cmd[i + 2:] if a != out_path]   # 源文件之后的参数（含编码参数）
    if "mpegts" in rest:                       # 只删输出侧那个 -f mpegts
        j = rest.index("mpegts")
        if j > 0 and rest[j - 1] == "-f":
            del rest[j - 1:j + 1]
    rest = ["-frames:v", "1"] + rest
    test += rest + ["-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"]
    proc = subprocess.run(test, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        return fallback
    # SPS NAL = 00 00 00 01 67 <profile_idc> <constraint_flags> <level_idc>
    for start in (b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67"):
        j = proc.stdout.find(start)
        if j >= 0:
            p, c, lv = proc.stdout[j + len(start):j + len(start) + 3]
            return f"avc1.{p:02x}{c:02x}{lv:02x},mp4a.40.2"
    return fallback


def measure_copy_codec_string(cmd: list, fallback: str) -> str:
    """直通档（`-c copy`）的 CODECS：真的 copy 一片源文件，从 SPS 里读。

    这里不能像重编档那样编测试图案 —— 直通档输出的就是**源自己的字节**，
    它可能是 High profile，也可能是 Constrained Baseline（实测三个样本源分别是
    avc1.640028 / avc1.640028 / avc1.42c01e），编出来的图案和它没有任何关系。

    所以这里只把 seek 挪到文件中间一小段（避开开头可能有的封面/黑场），
    其余参数原样，输出到临时文件读 SPS。失败就退回 `fallback`。
    """
    out = cmd[-1] + ".probe"
    test = list(cmd[:-1])
    if "-ss" in test:
        test[test.index("-ss") + 1] = f"{min(3.0, SEGMENT_DURATION):.3f}"
    try:
        subprocess.run(test + [out], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
        data = open(out, "rb").read()
    except (OSError, subprocess.SubprocessError):
        return fallback
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    for start in (b"\x00\x00\x00\x01\x67", b"\x00\x00\x01\x67"):
        j = data.find(start)
        if j >= 0:
            p, c, lv = data[j + len(start):j + len(start) + 3]
            return f"avc1.{p:02x}{c:02x}{lv:02x},mp4a.40.2"
    return fallback


def h264_codec_string(width: int, height: int, fps: float,
                      profile: str = "main", level: int = 0) -> str:
    """兜底用的 CODECS 估算（`avc1.<profile><constraint><level>,mp4a.40.2`）。

    正常情况下由 measure_h264_codec_string() 实测量出来，这个函数只在测量失败
    （临时文件写不出来之类）时兜底。level 按目标分辨率算，profile 由调用方给：
    copy 档用 ffprobe 读到的源 profile/level，重编档是 Main。
    constraint 字节推不出来，按经验给：Baseline 家族 0x40，其它 0x00。
    """
    idc = _H264_PROFILE_IDC.get(str(profile or "").strip().lower(), 0x4d)
    lvl = int(level) or _h264_level_for(width, height, fps)
    constraint = 0x40 if idc == 0x42 else 0x00
    return f"avc1.{idc:02x}{constraint:02x}{lvl:02x},mp4a.40.2"


def _build_variants(info: dict, max_height: int = 0, allow_upscale: bool = False):
    """按源文件信息生成档位表（在 main() 里、起服务之前调一次）。

    过滤规则：**源分辨率以上的档位自动隐藏**。1080p 源给 360p/480p/720p/1080p，
    换 4K 源跑 2160p 档才出现。理由是放大不会变清楚，只会多花几倍流量和 CPU
    （实测 1080p 源做 4K 档：6s 一片要 3154ms、8.12 Mbps）。
    和源一样高的那一档保留 —— 它和 original 的差别是码率策略不同，有对照价值。

    `original-copy` 恒在（但默认不进 master.m3u8），`original` 恒在。
    """
    global VARIANTS
    limit = int(max_height) if max_height else int(info.get("height") or 0)

    variants: dict = {
        # 原编码直通：-c copy，一帧不重编
        "original-copy": {"copy": True, "passthrough": True,
                          "width": info.get("width") or 0,
                          "height": info.get("height") or 0},
        # 输出 H.264、分辨率跟随源：源是 H.264 就退化成无损 copy
        "original": {"copy": True, "passthrough": False, "target_codec": "h264",
                     "width": info.get("width") or 0,
                     "height": info.get("height") or 0},
    }

    for spec in LADDER:
        h = int(spec["height"])
        if not allow_upscale and limit and h > limit:
            continue
        variants[spec["name"]] = {
            "width": int(spec["width"]), "height": h,
            "vb": spec["vb"], "ab": spec["ab"],
        }

    # 档位顺序 = master.m3u8 里的顺序。original 家族在最前（不参与 ABR 排序），
    # 固定档位由低到高排，ABR 日志里读起来顺。
    ordered = {"original-copy": variants.pop("original-copy")}
    if "original" in variants:
        ordered["original"] = variants.pop("original")
    ordered.update(variants)
    VARIANTS = ordered
    return VARIANTS


def variant_dir(variant: str) -> str:
    d = os.path.join(CACHE_DIR, variant)
    os.makedirs(d, exist_ok=True)
    return d


def segment_path(variant: str, index: int) -> str:
    return os.path.join(variant_dir(variant), f"seg{index}.ts")


def _is_cached(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def clear_segment_cache() -> int:
    """只删已完成的 seg*.ts（不碰 .part），这样正在转的片不会被误伤。

    这是给对照实验用的：冷启动 / 热缓存的差别全靠它。
    """
    n = 0
    for variant in VARIANTS:
        d = os.path.join(CACHE_DIR, variant)
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith("seg") and e.name.endswith(".ts"):
                        try:
                            os.remove(e.path)
                            n += 1
                        except OSError:
                            pass
        except (FileNotFoundError, NotADirectoryError):
            pass
    return n


# --------------------------------------------------------------------------- #
# 切片规划（时间轴：播放列表与转码共用同一份，保证严格一致）
# --------------------------------------------------------------------------- #


def split_uniform(duration: float, seg: float = SEGMENT_DURATION,
                  min_tail: float = MIN_TAIL_DURATION):
    """把 [0, duration) 平均切成 seg 秒一片，返回 [[start, dur], ...]。

    最后一片不足 min_tail 时并入前一片 —— 这就是「避免 0.0001 秒空切片」的那条。
    不这么做的话：600.016083s / 6s 向上取整 = 101 片，第 101 片只有 0.016s，
    播放器会去请求一个几乎没有内容的切片，等于白等一次网络 + 一次转码。

    注意：合并后最后一片会略长于 seg（最多 seg + min_tail），
    所以 EXT-X-TARGETDURATION 必须按真实最大值算（见 build_variant_playlist）。
    """
    if duration <= 0:
        return []

    n = max(1, int(math.ceil(duration / seg - 1e-9)))
    plan = []
    for i in range(n):
        start = i * seg
        plan.append([start, min(seg, duration - start)])

    if len(plan) >= 2 and plan[-1][1] < min_tail - 1e-9:
        plan[-2][1] += plan[-1][1]
        plan.pop()
    return plan


def _keyframe_times(path: str):
    """拿视频关键帧的 pts（秒）。原画直通档位必须按真实关键帧切。

    用 packet 而不是 frame：`-skip_frame nokey -show_entries frame=pts_time`
    要解析到帧层（10 分钟 1080p 实测 3189ms），
    `-show_entries packet=pts_time,flags` 只看包（330ms，快约 10 倍），关键帧结果一致。
    """
    with _CACHE_LOCK:
        per_file = CACHE.setdefault("keyframes", {})
        if path in per_file:
            return per_file[path]

    try:
        out = run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags",
            "-of", "csv=p=0",
            path,
        ])
    except Exception as exc:                       # noqa: BLE001
        print(f"[warn] ffprobe keyframes failed: {exc}")
        return []

    times = []
    for line in out.decode("utf-8", "ignore").splitlines():
        fields = line.strip().split(",")
        if len(fields) < 2 or "K" not in fields[1]:
            continue
        try:
            times.append(float(fields[0]))
        except ValueError:
            continue

    times = sorted(set(round(t, 4) for t in times))
    with _CACHE_LOCK:
        CACHE.setdefault("keyframes", {})[path] = times
    return times


def copy_plan(path: str, duration: float, seg: float = SEGMENT_DURATION,
              min_tail: float = MIN_TAIL_DURATION):
    """直通档位（`-c copy`）的切片方案：边界必须落在源文件真实关键帧上。

    否则每个切片都从「目标时间之前最近的关键帧」开始，相邻切片会重叠，
    EXTINF 也对不上，播放器时间轴直接乱掉。

    注意：只有 **直通** 档位走这里。`original` 现在是「输出 H.264」档，
    源是 H.264 时它也是 copy（走这里），源是 AV1/HEVC 时它要重编，
    重编就可以自己决定关键帧位置了，所以走 split_uniform()。

    代价：关键帧间隔不均匀（实测 0 / 6.006 / 12.888 / 17.684 ...），
    所以片长也不均匀，取的是「第一个离起点 >= seg 秒的关键帧」。
    """
    kfs = _keyframe_times(path)
    pts = sorted({0.0, round(float(duration), 4)}
                 | {t for t in kfs if 0 < t < duration - 1e-3})
    if len(pts) < 2:
        return [[0.0, float(duration)]]

    plan = []
    i = 0
    while i < len(pts) - 1:
        j = i + 1
        # 从 pts[i] 开始，往后找到第一个「离它 >= seg」的关键帧
        while j < len(pts) - 1 and (pts[j] - pts[i]) < seg - 1e-6:
            j += 1
        plan.append([pts[i], pts[j] - pts[i]])
        i = j

    # 尾片过短同样并入前一片：边界依然在关键帧上，时间轴依然连续
    if len(plan) >= 2 and plan[-1][1] < min_tail - 1e-9:
        plan[-2][1] += plan[-1][1]
        plan.pop()
    return plan


def variant_is_passthrough(name: str) -> bool:
    """这个档位是不是「一帧不重编」。"""
    return bool(VARIANTS[name].get("passthrough"))


def plan_for(cfg: dict, path: str, duration: float, seg: float = SEGMENT_DURATION,
             min_tail: float = MIN_TAIL_DURATION):
    """按档位配置算切片方案。抽出来是为了 --selftest 能对任意文件跑。

    只有真正直通（`-c copy`）的档位才必须贴着源关键帧切；重编的档位
    （包括 original 在源不是 H.264 时的重编路径）可以自己定关键帧，用均匀切分。
    """
    if cfg.get("passthrough"):
        return copy_plan(path, duration, seg, min_tail)
    return split_uniform(duration, seg, min_tail)


def segment_plan(variant: str):
    """返回该档位的 [(start, dur), ...]。惰性计算 + 缓存。"""
    key = ("plan", variant)
    cached = CACHE.get(key)
    if cached is not None:
        return cached

    # 注意：plan_for() 里（直通档）会去拿 _CACHE_LOCK 读关键帧缓存，
    # 所以这里绝不能在持锁的情况下调用它 —— 否则同一把非重入锁自锁死。
    plan = plan_for(VARIANTS[variant], SOURCE_FILE, CACHE["duration"],
                    SEGMENT_DURATION, MIN_TAIL_DURATION)
    with _CACHE_LOCK:
        return CACHE.setdefault(key, plan)


# --------------------------------------------------------------------------- #
# 播放列表生成（瞬时，不碰转码）
# --------------------------------------------------------------------------- #


def build_variant_playlist(variant: str) -> str:
    """构造媒体播放列表（media playlist）。这里就已经列出了全部分片，无需转码。"""
    plan = segment_plan(variant)
    # TARGETDURATION 必须 >= 每一片的真实时长（尾片合并后可能略长于 SEGMENT_DURATION），
    # 取整 + 极小 epsilon，避免 6.0000001 被算成 7。
    target = max(1, math.ceil(max(d for _, d in plan) - 1e-9))

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",          # 点播：播放列表静态不变
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",           # 第一片的序号
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for i, (_, dur) in enumerate(plan):
        lines.append(f"#EXTINF:{dur:.3f},")
        lines.append(f"seg{i}.ts")
    lines.append("#EXT-X-ENDLIST")           # VOD 结束标记
    return "\n".join(lines) + "\n"


def build_master_playlist() -> str:
    """主播放列表：把各码率档位串起来，播放器据此做自适应码率（ABR）切换。"""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for name, cfg in VARIANTS.items():
        # 原编码直通档默认不列出来：AV1/HEVC 装进 MPEG-TS 大部分播放器放不出来，
        # 列进去会让整个 master 被播放器判成「不支持」。想用就单独打
        # /stream/original-copy/index.m3u8，或者启动时加 --advertise-copy。
        if cfg.get("passthrough") and not ADVERTISE_COPY:
            continue
        bw = cfg["bandwidth"]
        attrs = [f"BANDWIDTH={bw}", f"AVERAGE-BANDWIDTH={int(bw * 0.85)}"]
        if cfg.get("width") and cfg.get("height"):
            attrs.append(f'RESOLUTION={cfg["width"]}x{cfg["height"]}')
        if cfg.get("codecs"):
            attrs.append(f'CODECS="{cfg["codecs"]}"')
        lines.append("#EXT-X-STREAM-INF:" + ",".join(attrs))
        # 注意：这里的 URI 是相对 master.m3u8 所在位置解析的，
        # 而 master 由 /master.m3u8 提供，所以子播放列表必须带上 stream/ 前缀。
        lines.append(f"stream/{name}/index.m3u8")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# 指标统计（/stats 用）
# --------------------------------------------------------------------------- #

STATS_LOCK = threading.Lock()
STATS = {
    "started_at": time.time(),
    "master_requests": 0,
    "playlist_requests": 0,
    "segment_requests": 0,
    "cache_hit": 0,              # 第一眼就命中（含被预取好的）
    "waited": 0,                 # 撞上别人正在转，排队等结果
    "generated_by_request": 0,   # 请求路径自己转的
    "prefetch_generated": 0,     # 预取转好的片数
    "prefetch_useful": 0,        # 预取转好、且后来真的被请求到了
    "bytes_served": 0,
    "gen_seconds": {},           # variant -> 累计 ffmpeg 墙钟秒
    "gen_count": {},             # variant -> 次数
    "latency_ms": [],            # 最近 200 次切片请求耗时
    "ttff_ms": None,             # master.m3u8 请求 -> 第一个切片返回（首帧代理指标）
    "first_master_at": None,
}

# (variant, index) -> "request" / "prefetch"：这片是谁转的
ORIGIN: dict = {}


def _bump(field, n=1):
    with STATS_LOCK:
        STATS[field] = STATS.get(field, 0) + n


def _record_gen(variant: str, seconds: float):
    with STATS_LOCK:
        STATS["gen_seconds"][variant] = STATS["gen_seconds"].get(variant, 0.0) + seconds
        STATS["gen_count"][variant] = STATS["gen_count"].get(variant, 0) + 1


def _record_latency(ms: float):
    with STATS_LOCK:
        buf = STATS["latency_ms"]
        buf.append(round(ms, 2))
        if len(buf) > 200:
            del buf[:-200]


def _percentile(values, pct):
    if not values:
        return None
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def _cached_count(variant: str) -> int:
    n = 0
    try:
        with os.scandir(os.path.join(CACHE_DIR, variant)) as it:
            for e in it:
                if (e.name.startswith("seg") and e.name.endswith(".ts")
                        and e.is_file() and e.stat().st_size > 0):
                    n += 1
    except (FileNotFoundError, NotADirectoryError):
        pass
    return n


def stats_snapshot() -> dict:
    """把内部统计整理成 JSON（/stats 与播放器面板都用它）。"""
    with STATS_LOCK:
        lat = list(STATS["latency_ms"])
        gen_seconds = dict(STATS["gen_seconds"])
        gen_count = dict(STATS["gen_count"])
        snap = {
            "uptime_s": round(time.time() - STATS["started_at"], 2),
            "master_requests": STATS["master_requests"],
            "playlist_requests": STATS["playlist_requests"],
            "segment_requests": STATS["segment_requests"],
            "cache_hit": STATS["cache_hit"],
            "waited": STATS["waited"],
            "generated_by_request": STATS["generated_by_request"],
            "prefetch_generated": STATS["prefetch_generated"],
            "prefetch_useful": STATS["prefetch_useful"],
            "bytes_served": STATS["bytes_served"],
            "ttff_ms": STATS["ttff_ms"],
        }

    total_gen = sum(gen_seconds.values())
    total_n = sum(gen_count.values())
    snap.update({
        "source": os.path.basename(SOURCE_FILE),
        "segment_duration": SEGMENT_DURATION,
        "min_tail_duration": MIN_TAIL_DURATION,
        "prefetch": {
            "enabled": bool(RUNTIME["prefetch"]),
            "ahead_seconds": RUNTIME["ahead"],
            "workers": PREFETCH_WORKERS,
            "queued": PREFETCHER.pending() if PREFETCHER else 0,
            "dropped": PREFETCHER.dropped if PREFETCHER else 0,
        },
        "gen_seconds_total": round(total_gen, 3),
        "gen_avg_ms": round(1000.0 * total_gen / total_n, 1) if total_n else None,
        "gen_avg_ms_by_variant": {
            v: round(1000.0 * gen_seconds[v] / gen_count[v], 1)
            for v in gen_seconds if gen_count.get(v)
        },
        "latency_ms": {
            "samples": len(lat),
            "last": lat[-1] if lat else None,
            "p50": _percentile(lat, 50),
            "p95": _percentile(lat, 95),
            "max": max(lat) if lat else None,
        },
        "variants": {
            name: {
                "segments": len(segment_plan(name)),
                "cached": _cached_count(name),
            }
            for name in VARIANTS
        },
    })
    return snap


# --------------------------------------------------------------------------- #
# 切片按需转码
# --------------------------------------------------------------------------- #

_locks_guard = threading.Lock()
_locks: dict = {}


def _lock_for(key):
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _cmd_copy(start: float, dur: float, out_path: str):
    """直通：不重新编码，只做封装转换 + 切片（`original-copy`，以及源本身是 H.264 的 `original`）。"""
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        # ---- 快速 seek：-ss 在 -i 之前，落在 start 这个关键帧上 ----
        "-ss", f"{start:.3f}",
        "-i", SOURCE_FILE,
        "-t", f"{dur:.3f}",
        "-c", "copy",
        # ---- 时间戳对齐到全片时间轴（关键！） ----
        "-output_ts_offset", f"{start:.3f}",
        "-muxdelay", "0", "-muxpreload", "0",
        "-f", "mpegts",
        out_path,
    ]


def _cmd_transcode(variant: str, start: float, dur: float, out_path: str):
    """重新编码：统一转 H.264，首帧强制关键帧，切片独立可解码。

    关键帧那句以前写的是 `expr:gte(t,0)` —— `gte(t,0)` 对每一帧都成立，
    结果每片都是 all-intra（实测 144 帧 144 个关键帧）。
    默认改成 `expr:gte(t,n_forced*SEG)`：每片恰好 1 个关键帧且落在首帧。
    两种模式的实测差异见 KEYFRAME_MODE 注释（all-intra 编码快 16%、多花 6% 字节）。

    两种档位共用这条路径：
    * 固定档位（720p/1080p/...）：scale+pad 到固定分辨率，VBV 卡码率上限。
    * `original` 且源不是 H.264（AV1/HEVC）：**不缩放**，用 CRF 尽量贴近源画质。
      这里刻意不用 ABR：这一档的卖点是「源文件画质 + H.264 兼容性」，压到某个
      码率上限就违背初衷了；要严格控码率请用固定档位。
    """
    cfg = VARIANTS[variant]
    if KEYFRAME_MODE == "all":
        kf_expr = "expr:gte(t,0)"
    else:
        kf_expr = f"expr:gte(t,n_forced*{SEGMENT_DURATION:g})"

    # 有 vb 的档位 = 固定档位：缩放 + VBV 卡码率；没有 vb 的 = `original`
    # 重编路径：不缩放、CRF 恒定质量（见 docstring）。
    has_vb = bool(cfg.get("vb"))
    if has_vb:
        vf = (f'scale={cfg["width"]}:{cfg["height"]}:force_original_aspect_ratio=decrease,'
              f'pad={cfg["width"]}:{cfg["height"]}:(ow-iw)/2:(oh-ih)/2')

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        # ---- 快速 seek：-ss 在 -i 之前，只从最近关键帧开始解码 ----
        "-ss", f"{start:.3f}",
        "-t", f"{dur:.3f}",
        "-i", SOURCE_FILE,
        # ---- 视频 ----
        "-c:v", "libx264", "-preset", PRESET, "-profile:v", "main",
        "-pix_fmt", "yuv420p",                   # main profile 要求 8bit，防 10bit 源翻车
    ]
    if has_vb:
        cmd += ["-vf", vf]
    cmd += [
        "-g", str(int(SEGMENT_DURATION * 25)),   # GOP ≈ 一片长度
        "-sc_threshold", "0",
        "-force_key_frames", kf_expr,
    ]
    if has_vb:
        vbv_kbps = int(cfg["vb"][:-1])
        cmd += ["-b:v", cfg["vb"], "-maxrate", cfg["vb"],
                "-bufsize", f"{int(vbv_kbps * VBV_FACTOR)}k"]
    else:
        # 保源分辨率档位：CRF 恒定质量，不做码率上限（见 docstring）
        cmd += ["-crf", str(SOURCE_CRF)]
    cmd += [
        # ---- 音频 ----
        "-c:a", "aac", "-b:a", cfg.get("ab", "160k"), "-ac", "2",
        # ---- 时间戳对齐到全片时间轴（关键！） ----
        "-output_ts_offset", f"{start:.3f}",
        "-muxdelay", "0", "-muxpreload", "0",
        # ---- 输出 MPEG-TS 切片 ----
        "-f", "mpegts",
        out_path,
    ]
    return cmd


def _safe_unlink(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _atomic_publish(tmp_path: str, out_path: str, attempts: int = 3):
    """把 .part 原子改名成正式切片。

    为什么要重试：Windows 上如果同一个缓存目录被两个进程共用（例如旧进程还没退干净，
    或者手滑起了两个实例），改名会因为「另一个程序正在使用此文件」失败。
    这时候退避重试几次；实在不行，只要正式文件已经完整存在，就说明这片的活
    已经被别人干完了 —— 那不是错误。
    """
    last = None
    for i in range(attempts):
        try:
            os.replace(tmp_path, out_path)
            return
        except OSError as exc:                     # pragma: no cover - 依赖时序
            last = exc
            if _is_cached(out_path):
                _safe_unlink(tmp_path)
                return
            time.sleep(0.15 * (i + 1))
    raise last


def _transcode_to(variant: str, index: int, out_path: str, source: str):
    """真正跑 ffmpeg 生成一片（调用方必须已经持有该片的锁）。

    copy / x264 的判断只在 _build_variants() 里做一次（源是 H.264 的 `original`
    在那时就已经被标成 copy 了），这里照着 cfg 走，不再重复探测。
    """
    start, dur = segment_plan(variant)[index]
    cfg = VARIANTS[variant]
    tmp_path = out_path + ".part"

    if cfg.get("copy"):
        cmd, tag = _cmd_copy(start, dur, tmp_path), "copy"
    else:
        cmd, tag = _cmd_transcode(variant, start, dur, tmp_path), "x264"
    t0 = time.time()
    try:
        run(cmd)
        _atomic_publish(tmp_path, out_path)      # 原子落盘，避免半成品被读到
    except Exception:
        _safe_unlink(tmp_path)
        raise
    cost = time.time() - t0

    ORIGIN[(variant, index)] = source
    _record_gen(variant, cost)
    if source == "prefetch":
        _bump("prefetch_generated")
    else:
        _bump("generated_by_request")
    print(f"[gen] {variant} seg{index} ({tag}/{source}) "
          f"start={start:.2f}s dur={dur:.3f}s -> {cost * 1000:.0f}ms")
    return cost


def generate_segment(variant: str, index: int, source: str = "request"):
    """生成（或复用缓存的）某个切片，返回 (路径, 是否直接命中缓存)。

    这是“按需”发生的时刻。两种调用者的等待策略不同：
      * 请求路径：没命中就排队等锁（别人正在转就等它转完，绝不重复转）
      * 预取路径：抢不到锁直接放弃（这片的活已经有人在干了，不值得再占一个 worker）
    """
    out_path = segment_path(variant, index)
    if _is_cached(out_path):
        if source == "request" and ORIGIN.pop((variant, index), None) == "prefetch":
            _bump("prefetch_useful")          # 预取真的被用上了
        return out_path, True

    lock = _lock_for((variant, index))

    if source == "prefetch":
        if not lock.acquire(blocking=False):
            return out_path, False
        try:
            if _is_cached(out_path):
                return out_path, True
            _transcode_to(variant, index, out_path, source)
        finally:
            lock.release()
        return out_path, False

    # ---- 请求路径 ----
    waited = False
    if not lock.acquire(blocking=False):
        waited = True
        lock.acquire()
    try:
        if _is_cached(out_path):
            if ORIGIN.pop((variant, index), None) == "prefetch":
                _bump("prefetch_useful")
            if waited:
                _bump("waited")
            return out_path, True
        _transcode_to(variant, index, out_path, source)
    finally:
        lock.release()
    return out_path, False


# --------------------------------------------------------------------------- #
# 预生成调度器（readme 第 1 条：预生成后几秒的切片）
# --------------------------------------------------------------------------- #


class Prefetcher:
    """后台把「后面几秒」的切片提前转好。

    - 固定线程池（默认按核数给 1~2 个 worker）：预取不能把 CPU 全占了，
      否则用户正在等的那一片反而更慢（实测 2 worker 时单片转码 +31%）
    - 优先队列：优先级小的先做（数字小的更接近播放位置）
    - 去重：排队集合 + 分片锁，同一片不会被排两次
    - seek 感知：新请求到来时，把窗口外的旧预取任务剪掉（用户跳进度条后那些就没用了）
    """

    def __init__(self, workers: int = PREFETCH_WORKERS):
        self.workers = max(0, workers)
        self._heap: list = []
        self._queued: set = set()
        self._cond = threading.Condition()
        self._seq = itertools.count()
        self._started = False
        self.dropped = 0

    # ---- 生命周期 ----
    def start(self):
        with self._cond:
            if self._started or self.workers <= 0:
                return
            self._started = True
        for i in range(self.workers):
            threading.Thread(target=self._worker, name=f"prefetch-{i}", daemon=True).start()
        print(f"[init] prefetch workers={self.workers} ahead={RUNTIME['ahead']}s")

    def pending(self) -> int:
        with self._cond:
            return len(self._heap)

    def ahead_segments(self) -> int:
        """预取窗口有多少片（按秒换算）。"""
        if not RUNTIME["prefetch"] or RUNTIME["ahead"] <= 0:
            return 0
        return max(1, int(math.ceil(RUNTIME["ahead"] / SEGMENT_DURATION - 1e-9)))

    # ---- 投递 ----
    def submit(self, variant: str, index: int, priority: int = 1):
        n = len(segment_plan(variant))
        if index < 0 or index >= n:
            return False
        if _is_cached(segment_path(variant, index)):
            return False
        key = (variant, index)
        with self._cond:
            if key in self._queued:
                return False
            self._queued.add(key)
            heapq.heappush(self._heap, (priority, next(self._seq), variant, index))
            self._cond.notify()
        return True

    def prefetch_after(self, variant: str, index: int):
        """请求了第 index 片 -> 把后面窗口内的片排上。"""
        k = self.ahead_segments()
        if k <= 0:
            return
        self._trim(variant, index, k)
        for j in range(index + 1, index + 1 + k):
            self.submit(variant, j, priority=1 + (j - index))

    def warm(self, variant: str, count: int = 1):
        """起播预热：把最前面的几片先转好。"""
        for j in range(count):
            self.submit(variant, j, priority=0)

    def _trim(self, variant: str, index: int, k: int):
        """把该档位窗口 (index, index+k] 之外的排队任务丢掉。"""
        with self._cond:
            keep, dropped = [], 0
            for item in self._heap:
                _, _, v, i = item
                if v == variant and not (index < i <= index + k):
                    self._queued.discard((v, i))
                    dropped += 1
                else:
                    keep.append(item)
            if dropped:
                heapq.heapify(keep)
                self._heap = keep
                self.dropped += dropped

    # ---- worker ----
    def _worker(self):
        while True:
            with self._cond:
                while not self._heap:
                    self._cond.wait()
                _, _, variant, index = heapq.heappop(self._heap)
                self._queued.discard((variant, index))
            if not RUNTIME["prefetch"]:
                continue                       # 运行中被关掉了，排队的直接作废
            try:
                generate_segment(variant, index, source="prefetch")
            except Exception as exc:           # noqa: BLE001
                print(f"[prefetch] {variant} seg{index} 失败: {exc}")


PREFETCHER: Prefetcher | None = None


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #

PLAYER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lazy HLS · 按需转码演示</title>
<style>
  :root{
    --bg:#080b12;
    --line:rgba(255,255,255,.08);
    --fg:#e9eefb;
    --muted:#8a9ab5;
    --accent:#5aa9ff;
    --ok:#37d67a;
    --warn:#ffb454;
    --err:#ff5f6d;
  }
  *{box-sizing:border-box}
  body{
    margin:0;padding:36px 20px 64px;min-height:100vh;
    font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    color:var(--fg);
    background:
      radial-gradient(1100px 560px at 12% -12%, rgba(60,120,255,.28), transparent 62%),
      radial-gradient(900px 520px at 96% -4%,  rgba(150,80,255,.20), transparent 58%),
      radial-gradient(760px 520px at 50% 120%, rgba(0,200,180,.10), transparent 60%),
      var(--bg);
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:980px;margin:0 auto}

  .head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .brand{display:flex;align-items:center;gap:10px}
  .logo{
    width:32px;height:32px;border-radius:10px;
    display:grid;place-items:center;font-size:13px;color:#cfe6ff;
    background:linear-gradient(150deg,#2d6bd6,#5aa9ff);
    box-shadow:0 8px 24px rgba(90,169,255,.35);
  }
  h1{margin:0;font-size:clamp(19px,3vw,26px);letter-spacing:.3px;font-weight:650}
  .tag{
    font-size:12px;padding:4px 11px;border-radius:999px;
    color:var(--accent);background:rgba(90,169,255,.12);
    border:1px solid rgba(90,169,255,.32);
  }
  .sub{margin:12px 0 22px;color:var(--muted);font-size:14px;line-height:1.7}
  .sub code{
    padding:1px 6px;border-radius:6px;font-size:12.5px;
    background:rgba(255,255,255,.07);border:1px solid var(--line);
  }

  .card{
    padding:14px;border-radius:20px;
    background:linear-gradient(180deg, rgba(24,32,46,.92), rgba(16,22,32,.92));
    border:1px solid var(--line);
    backdrop-filter:blur(14px);
    box-shadow:0 30px 80px rgba(0,0,0,.55);
  }
  .player{
    border-radius:14px;overflow:hidden;background:#000;
    box-shadow:0 0 0 1px var(--line), 0 22px 60px rgba(0,0,0,.6);
  }
  video{display:block;width:100%;aspect-ratio:16/9;background:#000;outline:none}

  .bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-top:14px}
  .levels{display:flex;gap:8px;flex-wrap:wrap}
  button{
    font:inherit;font-size:13px;font-weight:500;color:var(--fg);cursor:pointer;
    padding:7px 14px;border-radius:10px;
    background:rgba(255,255,255,.05);
    border:1px solid rgba(255,255,255,.10);
    transition:background .16s,border-color .16s,color .16s,transform .16s;
  }
  button:hover{background:rgba(255,255,255,.10);transform:translateY(-1px)}
  button:active{transform:translateY(0)}
  button.on{
    background:linear-gradient(180deg, rgba(90,169,255,.32), rgba(90,169,255,.16));
    border-color:rgba(90,169,255,.65);
    color:#d8ebff;
    box-shadow:0 0 0 1px rgba(90,169,255,.25), 0 6px 20px rgba(90,169,255,.18);
  }

  .status{
    margin-left:auto;display:flex;align-items:center;gap:9px;
    font-size:13px;color:var(--muted);white-space:nowrap;
  }
  .dot{
    width:8px;height:8px;border-radius:50%;background:var(--warn);
    box-shadow:0 0 0 4px rgba(255,180,84,.12);
    transition:background .2s,box-shadow .2s;
  }
  .dot.ok{background:var(--ok);box-shadow:0 0 0 4px rgba(55,214,122,.14), 0 0 12px rgba(55,214,122,.7)}
  .dot.err{background:var(--err);box-shadow:0 0 0 4px rgba(255,95,109,.14), 0 0 12px rgba(255,95,109,.7)}

  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
  .chip{
    display:flex;align-items:baseline;gap:6px;
    padding:6px 11px;border-radius:10px;font-size:12px;color:var(--muted);
    background:rgba(255,255,255,.045);border:1px solid var(--line);
  }
  .chip b{font-weight:600;color:var(--fg);font-size:13px;font-variant-numeric:tabular-nums}
  .chip.hot b{color:var(--ok)}
  .chip.warn b{color:var(--warn)}

  .log{
    margin:14px 0 0;padding:12px 14px;height:132px;overflow:auto;
    font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    font-size:12px;line-height:1.65;color:var(--muted);
    background:rgba(0,0,0,.38);
    border:1px solid var(--line);border-radius:12px;
    white-space:pre-wrap;word-break:break-all;
  }
  .log::-webkit-scrollbar{width:8px}
  .log::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:4px}
</style>
</head>
<body>
<div class="wrap">
  <header class="head">
    <div class="brand">
      <span class="logo">▶</span>
      <h1>Lazy HLS</h1>
    </div>
    <span class="tag">按需切片 · Just-In-Time</span>
  </header>
  <p class="sub">
    播放列表即时生成，<code>.ts</code> 切片在被请求时才现场用 ffmpeg 转码并落盘缓存；
    请求某片时会顺手把后面 <code>18s</code> 的切片提前转好（预生成）。
    档位按源文件分辨率生成：<code>original</code> 输出 H.264（源本来就是 H.264 时直接
    <code>-c copy</code>，零 CPU），下面各档是 libx264 重编；源分辨率以上的档位不出。
  </p>

  <main class="card">
    <div class="player">
      <video id="v" controls autoplay muted playsinline></video>
    </div>
    <div class="bar">
      <div class="levels" id="levels"></div>
      <button id="pf" type="button">预取：开</button>
      <button id="rst" type="button">重置统计</button>
      <div class="status"><span class="dot" id="dot"></span><span id="s">初始化…</span></div>
    </div>
    <div class="chips" id="chips"></div>
    <pre class="log" id="log"></pre>
  </main>
</div>

<script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
<script>
(function () {
  var video    = document.getElementById('v');
  var levelsEl = document.getElementById('levels');
  var statusEl = document.getElementById('s');
  var dotEl    = document.getElementById('dot');
  var logEl    = document.getElementById('log');
  var chipsEl  = document.getElementById('chips');
  var pfBtn    = document.getElementById('pf');
  var rstBtn   = document.getElementById('rst');

  var MAX_LOG = 4000;
  function log(msg) {
    var t = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    logEl.textContent = '[' + t + '] ' + msg + '\\n' + logEl.textContent;
    if (logEl.textContent.length > MAX_LOG) {
      logEl.textContent = logEl.textContent.slice(0, MAX_LOG);
    }
  }
  function status(text, kind) {
    statusEl.textContent = text;
    dotEl.className = 'dot' + (kind ? ' ' + kind : '');
  }

  // ---- 服务端指标面板 ----
  function ms(v) {
    if (v === null || v === undefined) return '—';
    return v < 1000 ? Math.round(v) + 'ms' : (v / 1000).toFixed(2) + 's';
  }
  function refreshStats() {
    fetch('/stats', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (s) {
      var pf = s.prefetch || {};
      var lat = s.latency_ms || {};
      pfBtn.textContent = '预取：' + (pf.enabled ? '开 · ' + pf.ahead_seconds + 's' : '关');
      pfBtn.classList.toggle('on', !!pf.enabled);

      var cached = 0, total = 0;
      Object.keys(s.variants || {}).forEach(function (k) {
        cached += s.variants[k].cached;
        total  += s.variants[k].segments;
      });

      var rows = [
        ['切片请求', s.segment_requests, ''],
        ['命中缓存', s.cache_hit, s.cache_hit ? 'hot' : ''],
        ['现场转码', s.generated_by_request, s.generated_by_request ? 'warn' : ''],
        ['排队等待', s.waited, s.waited ? 'warn' : ''],
        ['预取命中', s.prefetch_useful + ' / ' + s.prefetch_generated,
          s.prefetch_useful ? 'hot' : ''],
        ['首片耗时', ms(s.ttff_ms), ''],
        ['平均转码', ms(s.gen_avg_ms), ''],
        ['请求 p95', ms(lat.p95), ''],
        ['预取队列', pf.queued, ''],
        ['已缓存', cached + ' / ' + total, '']
      ];
      chipsEl.innerHTML = rows.map(function (r) {
        return '<span class="chip ' + r[2] + '">' + r[0] + '<b>' + r[1] + '</b></span>';
      }).join('');
    }).catch(function () { /* 服务端刚重启，忽略 */ });
  }
  setInterval(refreshStats, 1000);
  refreshStats();

  pfBtn.onclick = function () {
    var on = pfBtn.classList.contains('on') ? 0 : 1;
    fetch('/control?prefetch=' + on).then(function () {
      log('服务端预取 → ' + (on ? '开' : '关'));
      refreshStats();
    });
  };
  rstBtn.onclick = function () {
    fetch('/control?reset=1').then(function () {
      log('服务端统计已重置');
      refreshStats();
    });
  };

  var SRC = '/master.m3u8';

  if (!window.Hls || !Hls.isSupported()) {
    video.src = SRC;
    status('使用浏览器原生 HLS', 'ok');
    log('hls.js 不可用，回退到原生 <video src>。');
    return;
  }

  var hls = new Hls({
    enableWorker: true,
    lowLatencyMode: false,
    maxBufferLength: 10, // 缓冲区最大长度，单位秒
    debug: true
  });
  var sliceCount = 0;

  function label(lv) {
    if (lv.height) return lv.height + 'p';
    // 没有 RESOLUTION 的档位（original / original-copy）：用 profile 或短 codec 名
    var c = (lv.attrs && lv.attrs.CODECS) || '';
    if (c.indexOf('avc1') === 0) return '原画 H.264';
    if (c.indexOf('av01') === 0) return '原画 AV1';
    if (c.indexOf('hvc1') === 0) return '原画 HEVC';
    return '原画';
  }

  function mkBtn(text) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = text;
    return b;
  }
  function mark(el) {
    Array.prototype.forEach.call(levelsEl.children, function (c) {
      c.classList.toggle('on', c === el);
    });
  }

  function renderLevels(list) {
    levelsEl.innerHTML = '';

    var auto = mkBtn('自动');
    auto.onclick = function () {
      hls.currentLevel = -1;
      mark(auto);
      log('档位 → 自动（ABR）');
    };
    levelsEl.appendChild(auto);

    list.forEach(function (lv, i) {
      var b = mkBtn(label(lv) + ' · ' + (lv.bitrate / 1e6).toFixed(1) + 'M');
      b.onclick = function () {
        hls.currentLevel = i;
        mark(b);
        log('锁定档位 → ' + label(lv));
      };
      levelsEl.appendChild(b);
    });

    mark(levelsEl.firstChild);
  }

  hls.on(Hls.Events.MANIFEST_PARSED, function (_e, data) {
    status('已就绪 · ' + data.levels.length + ' 个档位', 'ok');
    log('master.m3u8 加载完成，发现 ' + data.levels.length + ' 个档位。');
    renderLevels(data.levels);
    // 直通档（原编码）默认不在 master 里，这里给个入口直接切过去 ——
    // AV1/HEVC 装进 MPEG-TS 大部分浏览器解不了，切过去多半会报错，那正是重点
    var raw = mkBtn('原编码直通');
    raw.title = '直接加载 /stream/original-copy/index.m3u8（不进 master.m3u8）';
    raw.onclick = function () {
      log('切到原编码直通档：/stream/original-copy/index.m3u8');
      hls.loadSource('/stream/original-copy/index.m3u8');
      video.play().catch(function () {});
    };
    levelsEl.appendChild(raw);
    video.play().catch(function () {
      log('自动播放被浏览器拦截，请手动点击播放。');
    });
  });

  hls.on(Hls.Events.LEVEL_SWITCHED, function (_e, data) {
    var lv = hls.levels[data.level];
    if (lv) {
      log('切换档位 → ' + label(lv) + '（' + Math.round(lv.bitrate / 1000) + ' kbps）');
    }
  });

  hls.on(Hls.Events.FRAG_LOADED, function (_e, data) {
    sliceCount++;
    var f = data.frag;
    var size = data.payload ? (data.payload.byteLength / 1024).toFixed(0) + ' KB' : '内存缓存';
    log('切片 ' + f.sn + ' 就绪 · ' + f.duration.toFixed(2) + 's · ' + size);
    status('已加载 ' + sliceCount + ' 个切片', 'ok');
  });

  hls.on(Hls.Events.ERROR, function (_e, data) {
    if (data.fatal) {
      status('播放失败 · ' + data.details, 'err');
      log('致命错误：' + data.type + ' / ' + data.details);
    } else {
      log('警告：' + data.details);
    }
  });

  hls.loadSource(SRC);
  hls.attachMedia(video);
})();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"        # 支持长连接，播放器少握手
    server_version = "LazyHLS/1.1"

    def log_message(self, fmt, *args):
        print("[http] %s - %s" % (self.address_string(), fmt % args))

    # ---- 响应助手 ----
    def _send(self, body: bytes, ctype: str, status: int = 200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Cache-Control",
            "no-cache" if "m3u8" in ctype else "public, max-age=31536000",
        )
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, text: str, ctype="application/vnd.apple.mpegurl", status=200):
        self._send(text.encode("utf-8"), ctype, status)

    def _json(self, obj, status=200):
        self._text(json.dumps(obj, ensure_ascii=False, indent=2),
                   "application/json; charset=utf-8", status)

    def _error(self, status, msg):
        self._send(msg.encode(), "text/plain; charset=utf-8", status)

    # ---- 路由 ----
    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                return self._text(PLAYER_HTML, "text/html; charset=utf-8")
            if path in ("/master.m3u8", "/playlist.m3u8"):
                return self._handle_master()
            if path.startswith("/stream/"):
                return self._handle_stream(path[len("/stream/"):])
            if path == "/stats":
                return self._json(stats_snapshot())
            if path == "/control":
                return self._handle_control(parse_qs(parsed.query))
            if path == "/health":
                return self._json({"ok": True})
            return self._error(404, "not found")
        except Exception as exc:                      # noqa: BLE001
            self._error(500, f"internal error: {exc}")

    # ---- 各路由实现 ----
    def _handle_master(self):
        with STATS_LOCK:
            STATS["master_requests"] += 1
            if STATS["first_master_at"] is None:
                STATS["first_master_at"] = time.time()   # TTFF 计时起点
        return self._text(build_master_playlist())

    def _handle_stream(self, rel: str):
        """rel 形如 '720p/index.m3u8' 或 '720p/seg3.ts'"""
        parts = rel.split("/")
        if len(parts) != 2:
            return self._error(404, "bad path")
        variant, name = parts
        if variant not in VARIANTS:
            return self._error(404, f"unknown variant {variant}")

        # --- 播放列表：瞬时返回，完全不转码 ---
        if name == "index.m3u8":
            with STATS_LOCK:
                STATS["playlist_requests"] += 1
            return self._text(build_variant_playlist(variant))

        # --- 切片：按需转码 / 读缓存 ---
        if name.startswith("seg") and name.endswith(".ts"):
            try:
                index = int(name[3:-3])
            except ValueError:
                return self._error(400, "bad segment index")
            if index < 0 or index >= len(segment_plan(variant)):
                return self._error(404, "segment out of range")
            return self._serve_segment(variant, index)

        return self._error(404, "not found")

    def _serve_segment(self, variant: str, index: int):
        t0 = time.time()
        _bump("segment_requests")

        ts_path, _hit = generate_segment(variant, index, source="request")

        with open(ts_path, "rb") as fh:
            body = fh.read()
        self._send(body, "video/mp2t")

        elapsed_ms = (time.time() - t0) * 1000.0
        _record_latency(elapsed_ms)
        with STATS_LOCK:
            STATS["bytes_served"] += len(body)
        if _hit:
            _bump("cache_hit")
        # 首个切片返回 -> 冷启动 TTFF（不含播放器解码/渲染，属于服务端代理指标）
        with STATS_LOCK:
            if STATS["ttff_ms"] is None and STATS["first_master_at"] is not None:
                STATS["ttff_ms"] = round((time.time() - STATS["first_master_at"]) * 1000.0, 1)

        # 预生成：把后面几秒的切片排上
        if PREFETCHER is not None:
            PREFETCHER.prefetch_after(variant, index)

    def _handle_control(self, qs: dict):
        if "prefetch" in qs:
            on = qs["prefetch"][0] not in ("0", "false", "off")
            RUNTIME["prefetch"] = on
            if on and PREFETCHER is not None:
                # 允许用 --prefetch-ahead 0 起来之后再从面板上把预取打开
                if PREFETCHER.workers <= 0:
                    PREFETCHER.workers = PREFETCH_WORKERS
                if not RUNTIME["ahead"]:
                    RUNTIME["ahead"] = PREFETCH_AHEAD_SECONDS
                PREFETCHER.start()
            print(f"[control] prefetch -> {on}")
        if "ahead" in qs:
            try:
                RUNTIME["ahead"] = max(0.0, float(qs["ahead"][0]))
                print(f"[control] ahead -> {RUNTIME['ahead']}s")
            except ValueError:
                return self._error(400, "bad ahead")
        if "clear_cache" in qs:
            n = clear_segment_cache()
            print(f"[control] cleared {n} cached segments")
        if "reset" in qs:
            with STATS_LOCK:
                for k in ("master_requests", "playlist_requests", "segment_requests",
                          "cache_hit", "waited", "generated_by_request",
                          "prefetch_generated", "prefetch_useful", "bytes_served"):
                    STATS[k] = 0
                STATS["gen_seconds"].clear()
                STATS["gen_count"].clear()
                STATS["latency_ms"].clear()
                STATS["ttff_ms"] = None
                STATS["first_master_at"] = None
                STATS["started_at"] = time.time()
            ORIGIN.clear()
            print("[control] stats reset")
        return self._json(stats_snapshot())


# --------------------------------------------------------------------------- #
# 切片方案自检（--selftest）
# --------------------------------------------------------------------------- #


def selftest(files=None) -> bool:
    """检查切片方案：没有空片、首尾连续、总时长对得上、直通档边界都在关键帧上。"""
    global VARIANTS
    if not files:
        files = sorted(glob.glob(os.path.join(MEDIA_DIR, "*.mp4")))
    if not files:
        print(f"[selftest] {MEDIA_DIR} 下没有 mp4")
        return False

    ok_all = True
    for path in files:
        name = os.path.basename(path)
        try:
            duration = probe_duration(path)
        except Exception as exc:                       # noqa: BLE001
            print(f"[selftest] {name}: ffprobe 失败 {exc}")
            ok_all = False
            continue
        info = probe_source_info(path)
        # 每个源都按它自己的分辨率生成档位（和起服务时走同一段代码），
        # 这样自检才覆盖到「4K 源给 4K 档 / 1080p 源不给」这条规则。
        print(f"\n{name}  duration={duration:.6f}s  "
              f"{info.get('vcodec')} {info.get('width')}x{info.get('height')} "
              f"-> original={'copy' if info.get('vcodec') == 'h264' else f'H.264 crf{SOURCE_CRF}'}")
        saved = VARIANTS
        try:
            variants = _build_variants(info, MAX_HEIGHT, ALLOW_UPSCALE)
            for variant, cfg in variants.items():
                plan = plan_for(cfg, path, duration, SEGMENT_DURATION, MIN_TAIL_DURATION)
                plain = plan_for(cfg, path, duration, SEGMENT_DURATION, 0.0)
                problems = []
                if not plan:
                    problems.append("切片方案为空")
                else:
                    total = sum(d for _, d in plan)
                    if abs(total - duration) > 1e-3:
                        problems.append(f"总时长对不上 sum={total:.6f}")
                    if any(d <= 0 for _, d in plan):
                        problems.append("存在非正时长的切片")
                    for a, b in zip(plan, plan[1:]):
                        if abs((a[0] + a[1]) - b[0]) > 1e-3:
                            problems.append("切片时间轴不连续")
                            break
                    durs = [d for _, d in plan]
                    if len(plan) >= 2 and min(durs) < MIN_TAIL_DURATION - 1e-9:
                        problems.append(f"仍存在过短切片 min={min(durs):.6f}s")
                    if min(durs) < 1e-3:
                        problems.append(f"存在空气切片 min={min(durs):.6f}s")
                    if cfg.get("passthrough"):
                        kfs = set(_keyframe_times(path))
                        for start, _ in plan[1:]:
                            if not any(abs(start - k) < 1e-3 for k in kfs):
                                problems.append(f"直通档切片起点 {start:.3f}s 不在关键帧上")
                                break
                tag = "OK  " if not problems else "FAIL"
                if problems:
                    ok_all = False
                print(f"  [{tag}] {variant:<14} 片数={len(plan):4d} "
                      f"(不合并尾片时={len(plain):4d}) "
                      f"最短={min((d for _, d in plan), default=0):.3f}s "
                      f"最长={max((d for _, d in plan), default=0):.3f}s "
                      f"合计={sum(d for _, d in plan):.3f}s"
                      + ("   " + "; ".join(problems) if problems else ""))
        finally:
            VARIANTS = saved
    print("\n[selftest] " + ("全部通过 ✅" if ok_all else "存在问题 ❌"))
    return ok_all


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #


def resolve_source(name: str) -> str:
    """--source 既可以是路径，也可以是 media/ 下的模糊关键字（如 1080 / small / av1）。"""
    if not name:
        return os.path.join(MEDIA_DIR, DEFAULT_SOURCE)
    if os.path.isfile(name):
        return os.path.abspath(name)
    exact = os.path.join(MEDIA_DIR, name)
    if os.path.isfile(exact):
        return exact
    hits = sorted(glob.glob(os.path.join(MEDIA_DIR, f"*{name}*.mp4")))
    if len(hits) == 1:
        return hits[0]
    if not hits:
        avail = "\n  ".join(os.path.basename(p)
                            for p in sorted(glob.glob(os.path.join(MEDIA_DIR, "*.mp4"))))
        raise SystemExit(f"找不到源文件: {name}\nmedia/ 下可选：\n  {avail}")
    print(f"[init] --source '{name}' 匹配到多个，用第一个：{os.path.basename(hits[0])}")
    return hits[0]


def _prep_variants(info: dict):
    """把档位表按源文件定下来：过滤 + 补元数据（BANDWIDTH / CODECS / copy 判定）。

    只在这里做一次，之后所有路径（切片方案、ffmpeg 命令、master.m3u8）都读 VARIANTS。
    BANDWIDTH 和 CODECS 都是**量出来的**而不是猜出来的：码率按 (视频+音频)×1.15 估，
    CODECS 直接编 1 帧从 SPS 里读（见 measure_h264_codec_string）。
    """
    _build_variants(info, MAX_HEIGHT, ALLOW_UPSCALE)

    is_h264_src = (info.get("vcodec") == "h264")
    abr = int(info.get("abr") or 0)
    copy_codecs = None                         # 直通档量出来的 CODECS，original 复用

    for name, cfg in VARIANTS.items():
        if cfg.get("passthrough"):
            # 原样直通：码率就是源的码率，codecs 声明源真实格式
            bw = (info.get("vbr") or 0) + abr
            cfg["bandwidth"] = int((bw or 6_000_000) * 1.15)
            cfg["codecs"] = _measure_variant_codec(name, cfg, info)
            copy_codecs = cfg["codecs"]
            continue

        if cfg.get("target_codec") == "h264":
            # original 档：源是 H.264 就 copy（无损 + 零 CPU），否则重编成 H.264
            cfg["copy"] = is_h264_src
            cfg["ab"] = cfg.get("ab") or "160k"
            if is_h264_src:
                bw = (info.get("vbr") or 0) + abr
                cfg["bandwidth"] = int((bw or 6_000_000) * 1.15)
            else:
                # 重编用 CRF，输出码率不可预知，按源码率 ×1.3 估（CRF18 一般不会超太多）
                est = int(((info.get("vbr") or 0) + max(abr, 128_000)) * 1.3)
                cfg["bandwidth"] = max(est, 1_500_000)
        else:
            # 固定档位：H.264 重编，VBV 卡码率上限
            vb = int(cfg["vb"][:-1])
            ab = int(cfg.get("ab", "0k")[:-1] or 0)
            cfg["bandwidth"] = int((vb + ab) * 1.15 * 1000)

        cfg["codecs"] = _measure_variant_codec(name, cfg, info, copy_codecs)


def _measure_variant_codec(name: str, cfg: dict, info: dict,
                           copy_codecs: str | None = None) -> str:
    """量出该档位切片的 CODECS 字符串。

    三条路：
    * 直通档（`passthrough`）：copy 一片真源读 SPS（源是什么 profile 就是什么）
    * 源是 H.264 的 `original`：它也是 copy，输出和 `original-copy` 一模一样，
      直接复用（`copy_codecs`），不用再跑一遍
    * 重编档：编 1 帧测试图案读 SPS（见 measure_h264_codec_string）
    """
    fallback = h264_codec_string(
        cfg.get("width") or 0, cfg.get("height") or 0, info.get("fps") or 0,
        profile=info.get("profile") or "main",
        level=(info.get("level") or 0) if cfg.get("copy") else 0)
    if not CODEC_PROBE:
        return fallback
    if cfg.get("passthrough"):
        return measure_copy_codec_string(
            _cmd_copy(0.0, SEGMENT_DURATION, "probe.ts"), fallback)
    if cfg.get("copy"):
        return copy_codecs or fallback
    return measure_h264_codec_string(
        _cmd_transcode(name, 0.0, SEGMENT_DURATION, "probe.h264"), "probe.h264",
        info.get("width") or 64, info.get("height") or 64, info.get("fps") or 24,
        fallback)


def _source_codec_string(info: dict) -> str:
    """直通档的 CODECS 声明：源是什么就声明什么（只在 --advertise-copy 时用得上）。

    注意：AV1/HEVC 这里只给出一个粗略的 codec 名，严格来说还差 profile/level
    这些字段（av01.0.08M.08 之类）。因为默认不把这个档位列进 master.m3u8，
    没有播放器会拿它做选择，所以先不做完整映射。
    """
    codec = str(info.get("vcodec") or "")
    if codec == "h264":
        return h264_codec_string(info.get("width") or 0, info.get("height") or 0,
                                 info.get("fps") or 0,
                                 profile=info.get("profile") or "main",
                                 level=info.get("level") or 0)
    return {"av1": "av01", "hevc": "hvc1"}.get(codec, "")


def main():
    global SEGMENT_DURATION, MIN_TAIL_DURATION, SOURCE_FILE, PREFETCHER
    global KEYFRAME_MODE, VBV_FACTOR, PRESET, CACHE_DIR
    global MAX_HEIGHT, ALLOW_UPSCALE, ADVERTISE_COPY, SOURCE_INFO, WARM_VARIANTS
    global CODEC_PROBE

    ap = argparse.ArgumentParser(description="按需（Lazy）HLS 切片服务")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--source", default="",
                    help="源文件路径，或 media/ 下的关键字（如 1080 / small / av1）")
    ap.add_argument("--segment-duration", type=float, default=SEGMENT_DURATION)
    ap.add_argument("--min-tail", type=float, default=MIN_TAIL_DURATION,
                    help="尾片短于该秒数就并入前一片；0 = 关闭（老行为）")
    ap.add_argument("--prefetch-ahead", type=float, default=PREFETCH_AHEAD_SECONDS,
                    help="预生成后面多少秒的切片；0 = 关闭")
    ap.add_argument("--prefetch-workers", type=int, default=PREFETCH_WORKERS)
    ap.add_argument("--vbv-factor", type=float, default=VBV_FACTOR,
                    help="VBV 缓冲倍数（相对视频码率）。1.0 贴标称码率，0.5 片子最小")
    ap.add_argument("--keyframe-mode", choices=("first", "all"), default=KEYFRAME_MODE,
                    help="first=只在片首打关键帧（省流量）；all=all-intra（编码快 16%%、多花 6%% 字节）")
    ap.add_argument("--preset", default=PRESET,
                    help="x264 preset（直接决定 TTFF）：veryfast 均衡，ultrafast 快 29%% 但 SSIM 掉 0.013")
    ap.add_argument("--max-height", type=int, default=0,
                    help="档位高度上限，超过的档位不生成。0 = 跟随源分辨率"
                         "（1080p 源不给 4K 档）")
    ap.add_argument("--allow-upscale", action="store_true",
                    help="允许源分辨率以上的档位（1080p 源也出 4K 档，纯放大）")
    ap.add_argument("--advertise-copy", action="store_true",
                    help="把 original-copy（原编码直通）也列进 master.m3u8。"
                         "默认不列：AV1/HEVC 装进 MPEG-TS 大部分播放器放不出来")
    ap.add_argument("--warm-variants", default=WARM_VARIANTS,
                    help="启动预热哪些档位的第 0 片，逗号分隔；all = 全部；空 = 不预热")
    ap.add_argument("--no-codec-probe", action="store_true",
                    help="不为每个档位编 1 帧量 CODECS，改成按分辨率估算"
                         "（省启动时间，但声明可能和实际流不一致）")
    ap.add_argument("--no-warm", action="store_true",
                    help="启动时不预热任何档位的第一片")
    ap.add_argument("--keep-cache", action="store_true", help="启动时不清空缓存")
    ap.add_argument("--cache-dir", default=CACHE_DIR,
                    help="切片缓存目录。跑两个实例做对照实验时务必分开，"
                         "否则会互相删/抢同一个 .part 文件")
    ap.add_argument("--selftest", action="store_true", help="只检查切片方案，不起服务")
    args = ap.parse_args()

    SEGMENT_DURATION = args.segment_duration
    MIN_TAIL_DURATION = args.min_tail
    KEYFRAME_MODE = args.keyframe_mode
    VBV_FACTOR = args.vbv_factor
    PRESET = args.preset
    MAX_HEIGHT = args.max_height
    ALLOW_UPSCALE = args.allow_upscale
    ADVERTISE_COPY = args.advertise_copy
    CACHE_DIR = os.path.abspath(args.cache_dir)
    SOURCE_FILE = resolve_source(args.source)
    WARM_VARIANTS = args.warm_variants
    CODEC_PROBE = not args.no_codec_probe

    if args.selftest:
        raise SystemExit(0 if selftest() else 1)

    if not os.path.exists(SOURCE_FILE):
        raise SystemExit(f"source not found: {SOURCE_FILE}")

    if args.keep_cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
    else:
        clear_cache()

    RUNTIME["prefetch"] = args.prefetch_ahead > 0 and args.prefetch_workers > 0
    RUNTIME["ahead"] = args.prefetch_ahead

    CACHE["duration"] = probe_duration(SOURCE_FILE)
    SOURCE_INFO = probe_source_info(SOURCE_FILE)
    _prep_variants(SOURCE_INFO)

    print(f"[init] source={os.path.basename(SOURCE_FILE)} "
          f"duration={CACHE['duration']:.6f}s seg={SEGMENT_DURATION:g}s "
          f"min_tail={MIN_TAIL_DURATION:g}s kf={KEYFRAME_MODE} vbv={VBV_FACTOR:g}x "
          f"preset={PRESET}")
    print(f"[init] 源编码={SOURCE_INFO.get('vcodec') or '?'} "
          f"{SOURCE_INFO.get('width')}x{SOURCE_INFO.get('height')} "
          f"{SOURCE_INFO.get('fps'):.2f}fps "
          f"{int((SOURCE_INFO.get('vbr') or 0) / 1000)}kbps  "
          f"-> original 档 = "
          f"{'copy（源本来就是 H.264）' if VARIANTS['original'].get('copy') else f'转 H.264 (crf {SOURCE_CRF})'}")

    t0 = time.time()
    for name, cfg in VARIANTS.items():
        plan = segment_plan(name)          # 触发关键帧扫描（直通档才有）
        kind = "copy" if cfg.get("copy") else "x264"
        res = f'{cfg["width"]}x{cfg["height"]}' if cfg.get("width") else "?"
        durs = [d for _, d in plan]
        hidden = "  (不进 master)" if cfg.get("passthrough") and not ADVERTISE_COPY else ""
        print(f"[init] {name:<14} {kind:<5} {res:<10} bandwidth={cfg['bandwidth']:<9} "
              f"{len(plan):4d} 片  片长 {min(durs):.2f}~{max(durs):.2f}s "
              f"合计 {sum(durs):.3f}s  {cfg.get('codecs') or '-'}{hidden}")
    print(f"[init] 切片方案耗时 {time.time() - t0:.3f}s")

    PREFETCHER = Prefetcher(args.prefetch_workers)
    if RUNTIME["prefetch"]:
        PREFETCHER.start()
        if not args.no_warm:
            names = list(VARIANTS) if args.warm_variants == "all" else [
                n.strip() for n in args.warm_variants.split(",") if n.strip()]
            for name in names:
                if name not in VARIANTS:
                    print(f"[warn] --warm-variants 里的 {name!r} 不是有效档位，跳过")
                    continue
                PREFETCHER.warm(name, 1)   # 起播预热：该档第 0 片

    print(f"[init] http://127.0.0.1:{args.port}/   （/stats 看指标）")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
