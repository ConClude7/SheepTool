#!/usr/bin/env python3
"""
mitmproxy 插件：拦截羊了个羊游戏 API，提取地图 MD5 + 种子。
写入 data/captured.json，由主进程轮询读取。

不要直接运行此文件，由 proxy_manager.py 通过 mitmdump -s intercept.py 加载。
"""
import json
import time
import urllib.parse
from pathlib import Path

# 写入目标（与 SheepTool 项目目录一致）
CAPTURE_FILE = Path(__file__).parent / "data" / "captured.json"

# 拦截目标
TARGET_HOST = "cat-match.easygame2021.com"

# 所有可能携带 map_md5 + map_seed 的路径后缀
MAP_INFO_PATHS = [
    "sheep/v1/game/map_info_ex",        # 每日关卡
    "sheep/v1/game/topic/game_start",   # 话题关卡
    "sheep/v1/game/tag/game/start",     # 标签关卡
    "sheep/v1/game/world/game_start",   # 世界关卡
]


def _is_target(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != TARGET_HOST:
        return False
    return any(parsed.path.endswith(p) for p in MAP_INFO_PATHS)


def _extract(response_bytes: bytes) -> dict | None:
    try:
        body = json.loads(response_bytes)
        data = body.get("data") or body
        md5_list  = data.get("map_md5")
        seed_list = data.get("map_seed")
        if not md5_list or seed_list is None:
            return None
        # map_seed 可能是字符串型数字
        seed_ints = [int(x) for x in seed_list]
        seed_2    = data.get("map_seed_2")

        # map_seed 全零时必须有 map_seed_2，否则无法还原（记录但跳过）
        if all(s == 0 for s in seed_ints) and not seed_2:
            print("[SheepTool] 警告：map_seed 全零且无 map_seed_2，跳过本次捕获。", flush=True)
            return None

        return {
            "map_md5":    md5_list,
            "map_seed":   seed_ints,
            "map_seed_2": seed_2,
            "timestamp":  time.time(),
        }
    except Exception:
        return None


class SheepInterceptor:
    def response(self, flow):
        if not _is_target(flow.request.url):
            return
        capture = _extract(flow.response.content)
        if not capture:
            return

        CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CAPTURE_FILE, "w") as f:
            json.dump(capture, f, ensure_ascii=False, indent=2)

        md5_count = len(capture["map_md5"])
        target = capture["map_md5"][1] if md5_count > 1 else capture["map_md5"][0]
        print(
            f"[SheepTool] 拦截成功！共 {md5_count} 张地图，目标 MD5: {target}",
            flush=True,
        )


addons = [SheepInterceptor()]
