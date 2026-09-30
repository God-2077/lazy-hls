#!/usr/bin/env python3
"""
Lazy HLS 基准脚本：像播放器一样拉流，量出「冷启动 / 预生成」的真实差别
=====================================================================

它做的事：
    1. （可选）`/control?reset=1&clear_cache=1` 把服务端缓存和统计清干净 -> 冷启动
    2. GET /master.m3u8                 -> 记加载耗时
    3. GET /stream/<档位>/index.m3u8     -> 记加载耗时、解析 EXTINF
    4. 按「播放器缓冲水位」逐片拉取 .ts   -> 记每片延迟、TTFF、卡顿
    5. GET /stats                       -> 把服务端视角的指标也带上

判据（比单看平均延迟有意义）：
    * TTFF：从请求 master.m3u8 到第一个切片收完，播放器要等这么久才出画面
    * 卡顿：第 i 片必须在「第 0 片到达时刻 + 前 i 片总时长」之前到达，
            晚到多少就算卡多少 —— 这才是用户能感觉到的指标
    * 预取命中率：服务端 /stats 里的 prefetch_useful / prefetch_generated

用法：
    python tools/bench.py --cold                      # 冷启动，预取开着
    python tools/bench.py --cold --variant 360p
    python tools/bench.py --cold --label after --json bench/after.json
    python tools/bench.py                             # 热缓存再播一遍
"""

import argparse
import http.client
import json
import os
import statistics
import sys
import time
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# HTTP 客户端（复用一个 keep-alive 连接，跟播放器行为一致）
# --------------------------------------------------------------------------- #


class Client:
    def __init__(self, base: str):
        u = urlparse(base)
        self.host = u.hostname or "127.0.0.1"
        self.port = u.port or 80
        self.prefix = u.path.rstrip("/")
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=300)

    def _connect(self):
        if self.conn.sock is None:
            self.conn.connect()

    def get(self, path: str):
        """返回 (body_bytes, 毫秒)。连接断了就重连一次。"""
        for attempt in (1, 2):
            try:
                self._connect()
                t0 = time.perf_counter()
                self.conn.request("GET", self.prefix + path)
                resp = self.conn.getresponse()
                body = resp.read()
                ms = (time.perf_counter() - t0) * 1000.0
                if resp.status != 200:
                    raise RuntimeError(f"{path} -> HTTP {resp.status}: {body[:200]!r}")
                return body, ms
            except (http.client.HTTPException, OSError):
                try:
                    self.conn.close()
                except Exception:                       # noqa: BLE001
                    pass
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=300)
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def close(self):
        try:
            self.conn.close()
        except Exception:                               # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# 播放列表解析
# --------------------------------------------------------------------------- #


def parse_master(text: str):
    """返回 [(uri, {BANDWIDTH: int, RESOLUTION: '1280x720'}), ...]"""
    out, attrs = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = {}
            for part in line[len("#EXT-X-STREAM-INF:"):].split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip()] = v.strip().strip('"')
            continue
        if line and not line.startswith("#") and attrs is not None:
            out.append((line, attrs))
            attrs = None
    return out


def parse_media(text: str):
    """返回 [(uri, 时长秒), ...]"""
    out, dur = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            dur = float(line[len("#EXTINF:"):].split(",")[0])
        elif line and not line.startswith("#"):
            out.append((line, dur if dur is not None else 0.0))
            dur = None
    return out


def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def run(args):
    base = args.url.rstrip("/")
    c = Client(base)

    # ---- 0. 重置服务端状态（老版本没有 /control，容忍失败）----
    ctl = "/control?reset=1"
    if args.cold:
        ctl += "&clear_cache=1"
    if args.prefetch is not None:
        ctl += f"&prefetch={1 if args.prefetch else 0}"
    if args.ahead is not None:
        ctl += f"&ahead={args.ahead}"
    try:
        c.get(ctl)
    except RuntimeError as exc:
        print(f"[bench] 提示：服务端不支持 /control（{exc}），跳过重置；"
              f"冷启动请重启服务端")

    # ---- 1. master ----
    t_start = time.perf_counter()
    master, t_master = c.get("/master.m3u8")
    variants = parse_master(master.decode("utf-8", "ignore"))
    if not variants:
        raise SystemExit("master.m3u8 里没有档位")

    pick = args.variant
    if pick == "auto":
        pick = max(variants, key=lambda v: int(v[1].get("BANDWIDTH", 0)))[0].split("/")[-2]
    names = [uri.split("/")[-2] for uri, _ in variants]
    if pick not in names:
        raise SystemExit(f"档位 {pick} 不存在，可选：{names}")
    uri = variants[names.index(pick)][0]

    # ---- 2. 档位播放列表 ----
    playlist, t_playlist = c.get("/" + uri.lstrip("/"))
    segs = parse_media(playlist.decode("utf-8", "ignore"))
    if args.limit:
        segs = segs[:args.limit]
    if not segs:
        raise SystemExit("播放列表里没有切片")

    print(f"[bench] {base}  档位={pick}  片数={len(segs)}  "
          f"总时长={sum(d for _, d in segs):.3f}s  "
          f"{'冷启动(已清缓存)' if args.cold else '热缓存'}  "
          f"prefetch={args.prefetch if args.prefetch is not None else 'default'}")

    # ---- 3. 逐片拉取（同时模拟播放器的缓冲水位）----
    base_dir = os.path.dirname(uri)
    lat, sizes, arrivals, durs = [], [], [], []
    ttff_ms = None
    stalls = 0
    stall_ms = 0.0
    min_buffer = None
    played = 0.0          # 已经“播”掉的媒体秒数
    buffered_end = 0.0    # 已经下到手的媒体位置（秒）
    started = False       # 首片到手才开始播放

    def advance(dt):
        """推进 dt 秒：播放器以 1x 消费缓冲，追上下载进度就是卡顿。

        返回 (是否卡顿, 卡了多久秒)。
        """
        nonlocal played, stalls, stall_ms
        if not started or dt <= 0:
            return False, 0.0
        room = buffered_end - played
        if dt <= room + 1e-9:
            played += dt
            return False, 0.0
        stall = dt - room
        stalls += 1
        stall_ms += stall * 1000.0
        played = buffered_end
        return True, stall

    def note_buffer():
        nonlocal min_buffer
        level = buffered_end - played
        min_buffer = level if min_buffer is None else min(min_buffer, level)

    for i, (name, dur) in enumerate(segs):
        body, ms = c.get(f"/{base_dir}/{name}")
        now = time.perf_counter()
        if i == 0:
            ttff_ms = (now - t_start) * 1000.0
            started = True                 # 首片到手，画面出来，播放开始
        else:
            advance(ms / 1000.0)           # 下载这一片的这段时间里，播放器在播

        lat.append(ms)
        sizes.append(len(body))
        arrivals.append(now)
        durs.append(dur)
        buffered_end += dur
        note_buffer()

        # 缓冲超过目标水位就“等播放”，避免无限抢跑（贴近 hls.js 的 maxBufferLength）
        if args.buffer_ahead > 0 and i + 1 < len(segs):
            excess = buffered_end - played - args.buffer_ahead
            if excess > 0:
                time.sleep(excess)
                advance(excess)
                note_buffer()

    wall_s = time.perf_counter() - t_start
    try:
        stats_body, _ = c.get("/stats")
        stats = json.loads(stats_body.decode("utf-8"))
    except (RuntimeError, ValueError):
        stats = {}                       # 老版本服务端没有 /stats
    c.close()

    total_bytes = sum(sizes)
    total_media = sum(durs)
    result = {
        "label": args.label or ("cold" if args.cold else "warm"),
        "variant": pick,
        "segments": len(segs),
        "media_seconds": round(total_media, 3),
        "wall_seconds": round(wall_s, 3),
        "master_ms": round(t_master, 2),
        "playlist_ms": round(t_playlist, 2),
        "ttff_ms": round(ttff_ms or 0.0, 1),
        "first_segment_ms": round(lat[0], 1),
        "seg_ms": {
            "min": round(min(lat), 1),
            "p50": round(pct(lat, 50), 1),
            "p95": round(pct(lat, 95), 1),
            "max": round(max(lat), 1),
            "mean": round(statistics.fmean(lat), 1),
        },
        "stalls": stalls,
        "stall_ms": round(stall_ms, 1),
        "min_buffer_s": round(min_buffer, 2) if min_buffer is not None else None,
        "bytes": total_bytes,
        "mbps": round(total_bytes * 8 / total_media / 1e6, 3) if total_media else 0.0,
        "server": {
            "segment_requests": stats.get("segment_requests"),
            "cache_hit": stats.get("cache_hit"),
            "waited": stats.get("waited"),
            "generated_by_request": stats.get("generated_by_request"),
            "prefetch_generated": stats.get("prefetch_generated"),
            "prefetch_useful": stats.get("prefetch_useful"),
            "gen_avg_ms": stats.get("gen_avg_ms"),
            "ttff_ms": stats.get("ttff_ms"),
            "latency_p95": (stats.get("latency_ms") or {}).get("p95"),
        },
    }

    print(f"[bench] master={result['master_ms']}ms  playlist={result['playlist_ms']}ms  "
          f"TTFF={result['ttff_ms']}ms  首片={result['first_segment_ms']}ms")
    print(f"[bench] 切片延迟 min/p50/p95/max = "
          f"{result['seg_ms']['min']}/{result['seg_ms']['p50']}/"
          f"{result['seg_ms']['p95']}/{result['seg_ms']['max']} ms   "
          f"卡顿 {stalls} 次 / {result['stall_ms']}ms   "
          f"最低缓冲 {result['min_buffer_s']}s   平均码率 {result['mbps']} Mbps")
    s = result["server"]
    print(f"[bench] 服务端：请求 {s['segment_requests']}  命中 {s['cache_hit']}  "
          f"排队等待 {s['waited']}  现转 {s['generated_by_request']}  "
          f"预取 {s['prefetch_useful']}/{s['prefetch_generated']}  "
          f"平均转码 {s['gen_avg_ms']}ms")
    print(f"[bench] 走完全部切片墙钟 {result['wall_seconds']}s")

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"[bench] 结果写入 {args.json}")
    return result


def main():
    ap = argparse.ArgumentParser(description="Lazy HLS 基准脚本")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--variant", default="720p", help="档位名，或 auto（最高码率）")
    ap.add_argument("--cold", action="store_true", help="先清空服务端缓存（冷启动）")
    ap.add_argument("--limit", type=int, default=0, help="只拉前 N 片（0 = 全部）")
    ap.add_argument("--buffer-ahead", type=float, default=10.0,
                    help="模拟播放器缓冲水位（秒），0 = 全速抢跑")
    ap.add_argument("--prefetch", type=int, choices=(0, 1), default=None,
                    help="强制开关服务端预取")
    ap.add_argument("--ahead", type=float, default=None, help="覆盖预取窗口秒数")
    ap.add_argument("--label", default="", help="这次运行的名字")
    ap.add_argument("--json", default="", help="把结果写到这个 json")
    args = ap.parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
