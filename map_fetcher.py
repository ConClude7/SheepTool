#!/usr/bin/env python3
"""
从官方 CDN 下载 .map 文件，解析 protobuf，用 map_seed 填充 tile type。

CDN URL 格式：https://cat-match-static.easygame2021.com/maps/{md5}.map
Headers 模拟微信小程序请求，与官方客户端一致。
"""
import json
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

STATIC_MAP_BASE = "https://cat-match-static.easygame2021.com/maps"

# 模拟微信小程序 Mac 客户端 UA（与 sheep-map-cli.js 一致）
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 "
    "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
    "MiniProgramEnv/Mac MacWechat/WMPF MacWechat/3.8.7(0x13080712) "
    "UnifiedPCMacWechat(0xf2641702) XWEB/18788"
)

_CDN_HEADERS = {
    "User-Agent": _USER_AGENT,
    "xweb_xhr": "1",
    "Accept": "*/*",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Referer": "https://servicewechat.com/wx141bfb9b73c970a9/459/page-frame.html",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

TOOLS_DIR  = Path(__file__).parent / "tools"
MAP_TO_JSON = TOOLS_DIR / "map-to-json.js"
DATA_DIR   = Path(__file__).parent / "data"
CACHE_DIR  = DATA_DIR / "maps"
PARSED_DIR = DATA_DIR / "parsed"


# ── 下载 ──────────────────────────────────────────────────────────────────────

def _download_map(md5: str) -> Path:
    """下载 .map 文件到缓存目录（已缓存则直接返回）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_file = CACHE_DIR / f"{md5}.map"
    if out_file.exists():
        print(f"  使用缓存: {out_file.name}")
        return out_file

    url = f"{STATIC_MAP_BASE}/{md5}.map"
    print(f"  下载中: {url}")

    req = urllib.request.Request(url, headers=_CDN_HEADERS)
    # 明确绕过系统代理（mitmproxy 代理此时可能正在运行）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"下载失败 HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"下载失败: {e.reason}  ({url})") from e

    out_file.write_bytes(data)
    print(f"  下载完成: {out_file.name}  ({len(data):,} bytes)")
    return out_file


# ── 解析 ──────────────────────────────────────────────────────────────────────

def _parse_map_file(map_file: Path) -> dict:
    """调用 Node.js map-to-json.js 解析二进制 .map → dict。"""
    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    out_json = PARSED_DIR / (map_file.stem + "_parsed.json")

    r = subprocess.run(
        ["node", str(MAP_TO_JSON), str(map_file), str(out_json)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"地图解析失败:\n{r.stderr.strip()}")

    data = json.loads(out_json.read_text())
    layers = len(data.get("layers", []))
    tiles  = sum(len(v) for v in data.get("levelData", {}).values())
    print(f"  解析完成: levelKey={data.get('levelKey')}  层={layers}  格={tiles}")
    return data


# ── 主入口 ────────────────────────────────────────────────────────────────────

def fetch_and_parse(
    md5_list: list[str],
    map_seed: list[int],
    map_seed_2=None,
    index: int = 1,
) -> dict:
    """
    下载指定索引的地图，解析并注入 map_seed，返回求解器可直接使用的 dict。

    参数：
      md5_list  — 来自 API 响应的 map_md5 数组
      map_seed  — 来自 API 响应的 4 元素种子数组
      map_seed_2 — 可选的第二种子
      index     — 选取第几张地图（0-based），官方通常用 index=1（第二张）

    返回：已注入 map_seed 的地图 dict，solver.py 的 _normalize_map_data 会据此填充 type。
    """
    if not md5_list:
        raise ValueError("md5_list 为空")

    idx = min(index, len(md5_list) - 1)
    md5 = md5_list[idx]

    print(f"正在获取地图 [{idx}] {md5} ...")
    map_file  = _download_map(md5)
    map_data  = _parse_map_file(map_file)

    # 将 seed 注入，供 solver.py 的 _normalize_map_data 填充 type
    map_data["map_seed"]   = map_seed
    map_data["map_seed_2"] = map_seed_2

    return map_data
