#!/usr/bin/env python3
"""
按需（Lazy / Just-In-Time）HLS 服务端测试，研究更好的实现方式
=====================================

核心思路
--------
传统做法：先 `ffmpeg -f hls` 把整个视频切成 n 个 .ts 再对外提供播放列表。
本方案：**播放列表（m3u8）是纯元数据，可以先算出来立刻返回**，
        而每个 .ts 切片等到客户端真正请求它时才用 ffmpeg 现场转码生成，并落盘缓存。

    1. 客户端 GET /master.m3u8            -> 直接返回 master 播放列表（瞬时）
    2. 客户端 GET /stream/720p/index.m3u8 -> ffprobe 拿时长 -> 算出切片数 -> 返回播放列表（<50ms）
    3. 客户端 GET /stream/720p/seg3.ts    -> 未命中缓存则 ffmpeg -ss/-t 只转这 6 秒 -> 返回并缓存
    4. 客户端 GET /stream/720p/seg3.ts    -> 命中缓存 -> 直接读文件返回（毫秒级）

这样首帧时间只取决于“第一个切片”的转码时间，而不是整个视频的转码时间。

档位说明
--------
* `original`：原画直通档位，`-c copy` 不重新编码，画质/码率与源文件完全一致；
              切片边界按源文件真实关键帧切分（copy 模式无法强制关键帧，只能用现成的），
              所以 EXTINF 用的是实际时长而不是固定 6s。
* `720p` / `360p`：重新编码档位，固定 6s 一片，首帧强制关键帧，可独立解码。

关键工程细节
------------
* `-ss` 放在 `-i` 之前 = 快速 seek（跳到目标位置之前最近的关键帧），比解码整段快几个数量级。
* `-output_ts_offset <start>` 把该切片的 PTS 偏移到全片时间轴上的正确位置，
  否则每个切片的时间戳都从 0 开始，播放器进度条/音画同步会乱。
* 每个切片是独立的 ffmpeg 进程、输出第一帧即关键帧，所以切片可独立解码、可随机 seek。
* 磁盘缓存 + 每切片一把锁：并发请求同一片时不会重复转码（缓存击穿保护）。

运行
----
    python3 server.py                 # 默认端口 8080
    python3 server.py --port 9000

然后用 curl / ffplay / 浏览器打开 http://localhost:8080/ 验证。
"""

import argparse
import json
import math
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import shutil

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

ROOT = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(ROOT, "media")
CACHE_DIR = os.path.join(ROOT, "cache")

# 单片时长（秒）。真实业务一般 4~10s，太短请求数爆炸，太长首帧慢。
SEGMENT_DURATION = 6

# 输入源。
# SOURCE_FILE = os.path.join(MEDIA_DIR, "sample.mp4")
# 'sample-big [av1 1920x1080 23m36s 376.6MB].mp4'     'sample-medium [h264 960x400 47s 21.9MB].mp4'
# 'sample-medium [h264 1920x1080 10m0s 122.0MB].mp4'  'sample-small [h264 854x480 52s 4.2MB].mp4'
# SOURCE_FILE = os.path.join(MEDIA_DIR, 'sample-big [av1 1920x1080 23m36s 376.6MB].mp4')
SOURCE_FILE = os.path.join(MEDIA_DIR, 'sample-medium [h264 960x400 47s 21.9MB].mp4')
# SOURCE_FILE = os.path.join(MEDIA_DIR, 'sample-small [h264 854x480 52s 4.2MB].mp4')
# SOURCE_FILE = os.path.join(MEDIA_DIR, 'sample-medium [h264 1920x1080 10m0s 122.0MB].mp4')


# 多码率档位。每个档位各自一条独立的切片/播放列表/缓存目录。
#   copy=True  -> 原画直通，不重新编码
#   width/height 在启动时由 ffprobe 自动填充（见 _fill_original_variant）
VARIANTS = {
    "original": {
        "copy": True,
        "bandwidth": 6_000_000,
    },
    "720p": {
        "width": 1280, "height": 720, "vb": "2500k", "ab": "128k",
        "bandwidth": 3_000_000, "codecs": "avc1.4d401f,mp4a.40.2",
    },
    "360p": {
        "width": 640, "height": 360, "vb": "800k", "ab": "96k",
        "bandwidth": 1_000_000, "codecs": "avc1.4d401e,mp4a.40.2",
    },
}

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
    """读源文件的分辨率与码率，用来给 original 档位填元数据。"""
    info = {"width": 0, "height": 0, "vbr": 0, "abr": 0}

    try:
        out = run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,bit_rate",
            "-of", "json", path,
        ])
        st = (json.loads(out).get("streams") or [{}])[0]
        info["width"] = int(st.get("width") or 0)
        info["height"] = int(st.get("height") or 0)
        info["vbr"] = int(st.get("bit_rate") or 0)
    except Exception as exc:                       # noqa: BLE001
        print(f"[warn] probe video stream failed: {exc}")

    try:
        out = run([
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=bit_rate",
            "-of", "json", path,
        ])
        st = (json.loads(out).get("streams") or [{}])[0]
        info["abr"] = int(st.get("bit_rate") or 0)
    except Exception:                              # noqa: BLE001
        pass

    return info


def variant_dir(variant: str) -> str:
    d = os.path.join(CACHE_DIR, variant)
    os.makedirs(d, exist_ok=True)
    return d


def segment_path(variant: str, index: int) -> str:
    return os.path.join(variant_dir(variant), f"seg{index}.ts")


# --------------------------------------------------------------------------- #
# 切片规划（时间轴：播放列表与转码共用同一份，保证严格一致）
# --------------------------------------------------------------------------- #


def segment_count(duration: float) -> int:
    # 向上取整：30s / 6s = 5 片
    return max(1, int(duration // SEGMENT_DURATION) + (1 if duration % SEGMENT_DURATION else 0))


def segment_range(duration: float, index: int):
    """返回第 index 片的 (起始秒, 时长秒)。最后一片可能更短。"""
    start = index * SEGMENT_DURATION
    remaining = duration - start
    return start, min(SEGMENT_DURATION, remaining)


def _keyframe_times(path: str):
    """拿视频关键帧的 pts（秒）。原画直通档位必须按真实关键帧切。"""
    try:
        out = run([
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time",
            "-of", "csv=p=0",
            path,
        ])
    except Exception as exc:                       # noqa: BLE001
        print(f"[warn] ffprobe keyframes failed: {exc}")
        return []

    times = []
    for line in out.decode("utf-8", "ignore").splitlines():
        token = line.strip().rstrip(",").strip()
        if not token:
            continue
        try:
            times.append(float(token))
        except ValueError:
            continue
    return sorted(times)


def _copy_plan(duration: float):
    """原画档位（-c copy）的切片方案：边界必须落在关键帧上。

    否则每个切片都从「目标时间之前最近的关键帧」开始，相邻切片会重叠，
    EXTINF 也对不上，播放器时间轴直接乱掉。
    """
    kfs = CACHE.get("keyframes")
    if kfs is None:
        kfs = _keyframe_times(SOURCE_FILE)
        CACHE["keyframes"] = kfs
        print(f"[init] 源文件关键帧 {len(kfs)} 个")

    pts = sorted({0.0, float(duration)}
                 | {round(t, 4) for t in kfs if 0 < t < duration - 1e-3})
    if len(pts) < 2:
        return [(0.0, float(duration))]

    plan = []
    i = 0
    while i < len(pts) - 1:
        j = i + 1
        # 从 pts[i] 开始，往后找到第一个「离它 >= SEGMENT_DURATION」的关键帧
        while j < len(pts) - 1 and (pts[j] - pts[i]) < SEGMENT_DURATION - 1e-6:
            j += 1
        plan.append((pts[i], pts[j] - pts[i]))
        i = j
    return plan


def segment_plan(variant: str):
    """返回该档位的 [(start, dur), ...]。惰性计算 + 缓存。"""
    key = ("plan", variant)
    cached = CACHE.get(key)
    if cached is not None:
        return cached

    duration = CACHE["duration"]
    cfg = VARIANTS[variant]

    if cfg.get("copy"):
        plan = _copy_plan(duration)
    else:
        n = segment_count(duration)
        plan = [segment_range(duration, i) for i in range(n)]

    CACHE[key] = plan
    return plan


# --------------------------------------------------------------------------- #
# 播放列表生成（瞬时，不碰转码）
# --------------------------------------------------------------------------- #


def build_variant_playlist(variant: str) -> str:
    """构造媒体播放列表（media playlist）。这里就已经列出了全部分片，无需转码。"""
    plan = segment_plan(variant)
    target = max(1, math.ceil(max(d for _, d in plan) - 1e-6))

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
        bw = cfg["bandwidth"]
        attrs = [f"BANDWIDTH={bw}", f"AVERAGE-BANDWIDTH={bw // 2}"]
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
# 切片按需转码
# --------------------------------------------------------------------------- #

_locks_guard = threading.Lock()
_locks: dict[tuple[str, int], threading.Lock] = {}


def _lock_for(key):
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _cmd_copy(start: float, dur: float, out_path: str):
    """原画直通：不重新编码，只做封装转换 + 切片。"""
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
    """重新编码：统一转 H.264，首帧强制关键帧，切片独立可解码。"""
    cfg = VARIANTS[variant]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        # ---- 快速 seek：-ss 在 -i 之前，只从最近关键帧开始解码 ----
        "-ss", f"{start:.3f}",
        "-t", f"{dur:.3f}",
        "-i", SOURCE_FILE,
        # ---- 视频 ----
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
        "-vf", f'scale={cfg["width"]}:{cfg["height"]}:force_original_aspect_ratio=decrease,'
               f'pad={cfg["width"]}:{cfg["height"]}:(ow-iw)/2:(oh-ih)/2',
        "-g", str(int(SEGMENT_DURATION * 25)),   # GOP ≈ 一片长度
        "-sc_threshold", "0",
        "-force_key_frames", "expr:gte(t,0)",    # 强制第一帧为关键帧
        "-b:v", cfg["vb"], "-maxrate", cfg["vb"],
        "-bufsize", str(int(cfg["vb"][:-1]) * 2) + "k",
        # ---- 音频 ----
        "-c:a", "aac", "-b:a", cfg["ab"], "-ac", "2",
        # ---- 时间戳对齐到全片时间轴（关键！） ----
        "-output_ts_offset", f"{start:.3f}",
        "-muxdelay", "0", "-muxpreload", "0",
        # ---- 输出 MPEG-TS 切片 ----
        "-f", "mpegts",
        out_path,
    ]


def generate_segment(variant: str, index: int) -> str:
    """生成（或复用缓存的）某个切片，返回 .ts 文件路径。这是"按需"发生的时刻。"""
    out_path = segment_path(variant, index)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
        return out_path                       # 命中缓存：0 成本

    start, dur = segment_plan(variant)[index]
    cfg = VARIANTS[variant]

    # 每个分片一把锁：并发请求同一片时只转一次，其余等待复用结果（防缓存击穿）
    with _lock_for((variant, index)):
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path                   # 双重检查，别的线程可能已转好

        tmp_path = out_path + ".part"
        if cfg.get("copy"):
            cmd = _cmd_copy(start, dur, tmp_path)
            tag = "copy"
        else:
            cmd = _cmd_transcode(variant, start, dur, tmp_path)
            tag = "x264"

        t0 = time.time()
        run(cmd)
        os.replace(tmp_path, out_path)        # 原子落盘，避免半成品被读到
        print(f"[transcode] {variant} seg{index} ({tag}) "
              f"start={start:.2f}s dur={dur:.2f}s -> {time.time() - t0:.2f}s")
    return out_path


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
    播放列表即时生成，<code>.ts</code> 切片在被请求时才现场用 ffmpeg 转码并落盘缓存。
    原画档位走 <code>-c copy</code> 直通，不消耗 CPU。
  </p>

  <main class="card">
    <div class="player">
      <video id="v" controls autoplay muted playsinline></video>
    </div>
    <div class="bar">
      <div class="levels" id="levels"></div>
      <div class="status"><span class="dot" id="dot"></span><span id="s">初始化…</span></div>
    </div>
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
    server_version = "LazyHLS/1.0"

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

    def _error(self, status, msg):
        self._send(msg.encode(), "text/plain; charset=utf-8", status)

    # ---- 路由 ----
    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    def _route(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._text(PLAYER_HTML, "text/html; charset=utf-8")
            if path in ("/master.m3u8", "/playlist.m3u8"):
                return self._text(build_master_playlist())
            if path.startswith("/stream/"):
                return self._handle_stream(path[len("/stream/"):])
            if path == "/health":
                return self._text('{"ok":true}', "application/json")
            return self._error(404, "not found")
        except Exception as exc:                      # noqa: BLE001
            self._error(500, f"internal error: {exc}")

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
            return self._text(build_variant_playlist(variant))

        # --- 切片：按需转码 / 读缓存 ---
        if name.startswith("seg") and name.endswith(".ts"):
            try:
                index = int(name[3:-3])
            except ValueError:
                return self._error(400, "bad segment index")
            if index < 0 or index >= len(segment_plan(variant)):
                return self._error(404, "segment out of range")
            ts_path = generate_segment(variant, index)
            with open(ts_path, "rb") as fh:
                body = fh.read()
            return self._send(body, "video/mp2t")

        return self._error(404, "not found")


CACHE = {}   # 进程内的小缓存：source 时长 / 切片方案 / 关键帧（真实项目可放 Redis）


def _fill_original_variant():
    """给 original 档位补上分辨率与带宽元数据（master.m3u8 里要用）。"""
    cfg = VARIANTS["original"]
    info = probe_source_info(SOURCE_FILE)
    cfg["width"] = info.get("width", 0)
    cfg["height"] = info.get("height", 0)

    bw = (info.get("vbr") or 0) + (info.get("abr") or 0)
    if bw <= 0:
        bw = 6_000_000
    cfg["bandwidth"] = int(bw * 1.15)             # 留 15% 余量


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    clear_cache()

    if not os.path.exists(SOURCE_FILE):
        raise SystemExit(f"source not found: {SOURCE_FILE}")

    CACHE["duration"] = probe_duration(SOURCE_FILE)
    _fill_original_variant()

    print(f"[init] source={os.path.basename(SOURCE_FILE)} "
          f"duration={CACHE['duration']:.2f}s")
    local_time = time.time()
    for name, cfg in VARIANTS.items():
        kind = "copy" if cfg.get("copy") else "x264"
        res = f'{cfg["width"]}x{cfg["height"]}' if cfg.get("width") else "?"
        print(f"[init] {name:<9} {kind:<5} {res:<10} "
              f"bandwidth={cfg['bandwidth']} -> {len(segment_plan(name))} 片")
    print(f"[init] 关键帧时间点耗时：{time.time() - local_time}s")
    print(f"[init] http://127.0.0.1:{args.port}/")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()