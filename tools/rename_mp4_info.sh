#!/bin/bash
# rename_mp4_info.sh
# 在含 MP4 的目录里执行： bash rename_mp4_info.sh
# 预览（不实际改名）：    DRY_RUN=1 bash rename_mp4_info.sh

shopt -s nullglob nocaseglob   # 没匹配到不报错；*.mp4 也能匹配 .MP4

for f in *.mp4; do
    # 已经处理过的（名字里带 " ["）跳过，避免重复叠加
    case "$f" in
        *" ["*) echo "跳过（已处理）: $f"; continue ;;
    esac

    base="${f%.*}"
    ext="${f##*.}"

    codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
            -of default=nw=1:nk=1 "./$f" | tr -d '\r')
    width=$(ffprobe -v error -select_streams v:0 -show_entries stream=width \
            -of default=nw=1:nk=1 "./$f" | tr -d '\r')
    height=$(ffprobe -v error -select_streams v:0 -show_entries stream=height \
            -of default=nw=1:nk=1 "./$f" | tr -d '\r')
    duration=$(ffprobe -v error -show_entries format=duration \
            -of default=nw=1:nk=1 "./$f" | tr -d '\r')
    size=$(stat -c %s "./$f")

    if [ -z "$codec" ] || [ -z "$duration" ]; then
        echo "跳过（无法读取）: $f"
        continue
    fi

    # 秒 -> 1h23m45s / 23m45s / 45s
    dur_fmt=$(awk -v d="$duration" 'BEGIN{
        d = int(d + 0.5);
        h = int(d/3600); m = int((d%3600)/60); s = d%60;
        if (h > 0)      printf "%dh%dm%ds", h, m, s;
        else if (m > 0) printf "%dm%ds", m, s;
        else            printf "%ds", s;
    }')

    # 字节 -> 人类可读
    size_fmt=$(awk -v b="$size" 'BEGIN{
        if (b >= 1073741824)   printf "%.2fGB", b/1073741824;
        else if (b >= 1048576) printf "%.1fMB", b/1048576;
        else                   printf "%.0fKB", b/1024;
    }')

    newname="${base} [${codec} ${width}x${height} ${dur_fmt} ${size_fmt}].${ext}"

    if [ "$f" = "$newname" ]; then
        echo "无需改动: $f"
        continue
    fi

    if [ -e "$newname" ]; then
        echo "目标已存在，跳过: $newname"
        continue
    fi

    if [ -n "$DRY_RUN" ]; then
        echo "[预览] $f  ->  $newname"
    else
        mv -- "$f" "$newname" && echo "重命名: $f  ->  $newname"
    fi
done