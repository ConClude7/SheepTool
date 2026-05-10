#!/usr/bin/env python3
"""
SheepTool — 羊了个羊自动化助手

典型流程：
  1. 进入关卡后从抓包/Network 面板复制 API 响应 JSON
  2. python main.py run                    （粘贴 JSON → 解析地图→求解第二关→点击第一关→确认点击第二关）

完整命令：
  calibrate               校准微信窗口中的牌局区域
  preview                 生成当前窗口的对齐预览图（检查校准是否准确）
  run [选项]              主命令：输入 API JSON → 下载→解析→求解→点击
    --json  JSON字符串    直接传入 API 响应（省去交互粘贴）
    --file  FILE          从文件读取 API 响应
    --level 1|2           单关模式：运行第几关（默认 2；默认 run 会自动串两关）
    --delay SEC           点击间隔，默认 0.4s
    --pause-after N       每 N 步自动暂停
    --step                单步模式（每步按 n 确认）
    --algorithm MODE      求解算法（默认 normal）

求解期间快捷键：  s 停止求解并采用当前最佳部分解
点击期间快捷键：  p 暂停/继续   n 下一步   s 结束
"""
import argparse
import json
import re
import ssl
import sys
import urllib.request
from pathlib import Path

DATA_DIR    = Path(__file__).parent / "data"
CONFIG_FILE = Path(__file__).parent / "config.json"
KEYSTREAM_DIR = DATA_DIR / "keystreams"

DEFAULT_CONFIG = {
    "click_delay": 0.4,
    "first_click_delay": 0.0,
    "start_delay": 3.0,
    "warmup_steps": 5,
    "warmup_click_delay": 0.35,
    "pause_after": 0,
    "algorithm":   "normal",
    "solver": {
        "show_progress":      True,
        "solve_first":        0.8,
        "time_limit":         -1,
        "expect_progress":    {"time": -1, "percentage": 0.80},
        "random_attempts":    30,
        "random_attempt_sec": 30,
        "random_workers":     0,
        "partial_accept":     0.0,
        "manual_stop":        True,
    },
}

ALGORITHMS = ["normal", "random", "level-top", "level-bottom",
              "index-ascending", "index-descending",
              "triple-greedy", "mrv"]


def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            loaded = json.load(f)
        cfg = DEFAULT_CONFIG.copy()
        cfg.update({k: v for k, v in loaded.items() if k != "solver"})
        cfg["solver"] = {**DEFAULT_CONFIG["solver"], **loaded.get("solver", {})}
        return cfg
    return DEFAULT_CONFIG.copy()


# ── API JSON 读取 ─────────────────────────────────────────────────────────────

def read_api_json(args) -> dict:
    """从多种来源读取 API 响应，返回解析后的 dict。

    优先级：--json > --file > 交互式粘贴
    """
    if getattr(args, "json", None):
        raw = args.json
    elif getattr(args, "file", None):
        raw = Path(args.file).read_text(encoding="utf-8")
    else:
        raw = _paste_json_interactively()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败：{e}") from e

    # 支持直接粘贴整个响应，或只粘贴 data 部分
    data = parsed.get("data", parsed)
    if not isinstance(data, dict):
        raise ValueError("JSON 缺少 data 对象")

    return _build_api_data(data, data)


def _build_api_data(data: dict, _orig: dict) -> dict:
    """从已解析的 data 字段中提取并校验必要字段。"""
    md5_list  = data.get("map_md5")
    seed_list = data.get("map_seed")
    seed_2    = data.get("map_seed_2")
    real_seed = (
        data.get("map_seed_real")
        or data.get("real_map_seed")
        or data.get("seed_map")
    )

    if not isinstance(md5_list, list) or len(md5_list) == 0:
        raise ValueError("JSON 缺少 data.map_md5 数组")
    if not isinstance(seed_list, list) or len(seed_list) != 4:
        raise ValueError("JSON 缺少合法的 data.map_seed（需要 4 个数字）")

    if isinstance(real_seed, list) and len(real_seed) == 4:
        try:
            normalized_real_seed = [int(s) for s in real_seed]
        except (TypeError, ValueError):
            normalized_real_seed = None
        if normalized_real_seed and any(s != 0 for s in normalized_real_seed):
            seed_list = normalized_real_seed

    # map_seed 全零时必须有 map_seed_2 兜底（solver.py 负责解码）
    if all(int(s) == 0 for s in seed_list) and not seed_2:
        raise ValueError(
            "data.map_seed 全为 0 且无 map_seed_2，无法还原 tile type。\n"
            "请确认抓包的是游戏开始请求（map_info_ex / game_start）。"
        )

    return {
        "map_md5":    md5_list,
        "map_seed":   [int(s) for s in seed_list],
        "map_seed_2": seed_2,
    }


def _paste_json_interactively() -> str:
    """提示用户粘贴 JSON，读到 EOF（Ctrl+D）后返回。

    不再逐行读取——match_data 含有被终端误判为换行的 Unicode 控制字符，
    逐行 input() 会在字符串中途断开导致 JSON 解析失败。
    改为一次性读完标准输入，让用户粘贴后手动按 Ctrl+D 结束。

    提示：如果粘贴始终出错，建议改用文件方式：
      1. 将 JSON 保存到 api.json
      2. python main.py run --file api.json
    """
    print("请粘贴 API 响应 JSON，粘贴完成后按 Ctrl+D（macOS/Linux）结束输入：")
    print("  提示：若粘贴失败，请改用  python main.py run --file <api.json>")
    try:
        return sys.stdin.read()
    except KeyboardInterrupt:
        return ""


def _paste_one_line(title: str) -> str:
    """读取一行抓包文本或文件路径。"""
    print(title)
    print("粘贴后直接回车确认；也可以输入本地文件/Raw 抓包目录路径。")
    try:
        value = input("> ").strip()
    except EOFError:
        return ""
    path = Path(value).expanduser()
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    return value


def _as_existing_dir(value: str) -> Path | None:
    path = Path(value).expanduser()
    if path.exists() and path.is_dir():
        return path
    return None


def _find_raw_capture_file(folder: Path, endpoint: str, direction: str) -> Path:
    direction = direction.lower()
    matches = [
        p for p in folder.iterdir()
        if p.is_file()
        and endpoint in p.name
        and not (endpoint == "map_info_ex" and "map_info_ex_seed" in p.name)
        and direction in p.name.lower()
    ]
    if not matches:
        raise ValueError(f"Raw 抓包目录里找不到 {endpoint} 的 {direction} 文件: {folder}")
    matches.sort(key=lambda p: (len(p.name), p.name))
    return matches[0]


def _find_optional_raw_capture_file(folder: Path, endpoint: str, direction: str) -> Path | None:
    direction = direction.lower()
    matches = [
        p for p in folder.iterdir()
        if p.is_file()
        and endpoint in p.name
        and direction in p.name.lower()
    ]
    matches.sort(key=lambda p: (len(p.name), p.name))
    return matches[0] if matches else None


def _load_daily_capture_dir(folder: Path) -> tuple[str, str | None, str | None]:
    map_resp = _find_raw_capture_file(folder, "map_info_ex", "response")
    seed_requests = [
        p for p in folder.iterdir()
        if p.is_file()
        and "map_info_ex_seed" in p.name
        and "request" in p.name.lower()
    ]
    seed_request = sorted(seed_requests, key=lambda p: (len(p.name), p.name))[0] if seed_requests else None
    game_over_request = _find_optional_raw_capture_file(folder, "game_over_ex", "request")
    raw_map = map_resp.read_text(encoding="utf-8", errors="replace").strip()
    raw_seed = (
        seed_request.read_text(encoding="utf-8", errors="replace").strip()
        if seed_request else None
    )
    raw_game_over = (
        game_over_request.read_text(encoding="utf-8", errors="replace").strip()
        if game_over_request else None
    )
    print(f"已从 Raw 目录读取 map_info_ex Response: {map_resp.name}", flush=True)
    if seed_request:
        print(f"已从 Raw 目录读取 map_info_ex_seed Request: {seed_request.name}", flush=True)
    if game_over_request:
        print(f"已从 Raw 目录读取 game_over_ex Request: {game_over_request.name}", flush=True)
    return raw_map, raw_seed, raw_game_over


def _read_game_over_capture(value: str) -> str:
    folder = _as_existing_dir(value)
    if folder:
        request_file = _find_optional_raw_capture_file(folder, "game_over_ex", "request")
        if not request_file:
            raise ValueError(f"Raw 抓包目录里找不到 game_over_ex Request: {folder}")
        print(f"已从 Raw 目录读取 game_over_ex Request: {request_file.name}", flush=True)
        return request_file.read_text(encoding="utf-8", errors="replace").strip()
    path = Path(value).expanduser()
    if path.exists() and path.is_file():
        return path.read_text(encoding="utf-8", errors="replace").strip()
    return value


def _split_http_body(raw: str) -> str:
    if "\r\n\r\n" in raw:
        return raw.split("\r\n\r\n", 1)[1].strip()
    if "\n\n" in raw:
        return raw.split("\n\n", 1)[1].strip()
    return raw.strip()


def _parse_json_or_http(raw: str) -> dict:
    body = _split_http_body(raw)
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise ValueError(f"抓包 JSON 解析失败：{e}") from e


def _looks_like_hex_bytes(raw: str) -> bool:
    compact = re.sub(r"\s+", "", raw).strip()
    return bool(compact) and len(compact) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", compact) is not None


def _parse_hex_bytes(raw: str) -> bytes:
    return bytes.fromhex(re.sub(r"\s+", "", raw).strip())


def _parse_http_headers(raw: str) -> tuple[str, dict[str, str], str]:
    head = raw
    body = ""
    if "\r\n\r\n" in raw:
        head, body = raw.split("\r\n\r\n", 1)
    elif "\n\n" in raw:
        head, body = raw.split("\n\n", 1)
    lines = [line.rstrip("\r") for line in head.splitlines() if line.strip()]
    if not lines:
        raise ValueError("seed 请求为空")
    request_line = lines[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return request_line, headers, body.strip()


def _load_keystream(version: int) -> bytes | None:
    candidates = [
        KEYSTREAM_DIR / f"ofb_v{version}.bin",
        DATA_DIR / f"ofb_keystream_v{version}.bin",
    ]
    candidates.extend(sorted(DATA_DIR.glob(f"ofb_keystream_v{version}_*.bin")))
    for path in candidates:
        if path.exists():
            data = path.read_bytes()
            if path.parent != KEYSTREAM_DIR:
                _save_keystream_cache(version, data)
            return data
    return None


def _iter_cached_keystreams() -> list[tuple[int, bytes]]:
    found: dict[int, bytes] = {}
    for path in sorted(KEYSTREAM_DIR.glob("ofb_v*.bin")) + sorted(DATA_DIR.glob("ofb_keystream_v*.bin")):
        match = re.search(r"(?:ofb_v|ofb_keystream_v)(\d+)", path.name)
        if not match or not path.exists():
            continue
        version = int(match.group(1))
        data = path.read_bytes()
        if version not in found or len(data) > len(found[version]):
            found[version] = data
    return sorted(found.items())


def _save_keystream_cache(version: int, data: bytes) -> Path:
    KEYSTREAM_DIR.mkdir(parents=True, exist_ok=True)
    out = KEYSTREAM_DIR / f"ofb_v{version}.bin"
    if not out.exists() or len(data) > out.stat().st_size:
        out.write_bytes(data)
    return out


def _decode_seed_response_with_keystreams(seed_response: bytes) -> tuple[int, object]:
    from scripts.seed_tool import _xor, decode_seed_ack

    errors: list[str] = []
    for version, keystream in _iter_cached_keystreams():
        if len(keystream) < len(seed_response):
            errors.append(f"v{version}: keystream 太短")
            continue
        try:
            ack = decode_seed_ack(_xor(seed_response, keystream))
        except Exception as exc:
            errors.append(f"v{version}: {exc}")
            continue
        if ack.code == 1 and len(ack.map_seed) == 4:
            return version, ack
        errors.append(f"v{version}: code={ack.code}, map_seed={ack.map_seed}")

    detail = "; ".join(errors) if errors else "没有找到任何 keystream 缓存"
    raise ValueError("seed 响应 hex 无法用本地 keystream 解出完整 mapSeed：" + detail)


def _replay_seed_request(raw_request: str) -> bytes:
    request_line, headers, body = _parse_http_headers(raw_request)
    match = re.match(r"POST\s+(\S+)", request_line)
    if not match:
        raise ValueError("请粘贴 map_info_ex_seed 的 POST 请求")
    path = match.group(1)
    host = headers.get("host", "cat-match.easygame2021.com")
    url = f"https://{host}{path}"

    keep_headers = {
        "Content-Type": headers.get("content-type", "application/json"),
        "b": headers.get("b", ""),
        "t": headers.get("t", ""),
        "Referer": headers.get("referer", ""),
        "User-Agent": headers.get("user-agent", ""),
        "xweb_xhr": headers.get("xweb_xhr", "1"),
    }
    keep_headers = {k: v for k, v in keep_headers.items() if v}

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=keep_headers,
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
    )
    with opener.open(req, timeout=30) as resp:
        return resp.read()


def _try_save_keystream_from_game_over(raw_game_over_request: str, expected_version: int) -> bytes | None:
    from scripts.seed_tool import _game_over_plaintext_from_request, _xor

    version, cipher, plain = _game_over_plaintext_from_request(raw_game_over_request)
    if version != expected_version:
        print(
            f"忽略 game_over_ex：版本是 v{version}，当前 seed 需要 v{expected_version}。",
            flush=True,
        )
        return None
    keystream = _xor(cipher, plain)
    out = _save_keystream_cache(version, keystream)
    print(f"已自动生成 v{version} keystream: {out}", flush=True)
    return keystream


def _resolve_daily_seed(
    api_data: dict,
    raw_seed_request: str,
    raw_game_over_request: str | None = None,
) -> dict:
    from scripts.seed_tool import (
        _xor,
        decode_seed_ack,
        derive_keystream_from_request,
    )

    if _looks_like_hex_bytes(raw_seed_request):
        seed_response = _parse_hex_bytes(raw_seed_request)
        version, ack = _decode_seed_response_with_keystreams(seed_response)
        out_resp = DATA_DIR / f"seed_response_v{version}_latest.bin"
        out_resp.write_bytes(seed_response)
        return _apply_seed_ack(api_data, ack)

    seed_req = _parse_json_or_http(raw_seed_request)
    version = int(seed_req.get("encryptKeyVersion", 0))
    info = seed_req.get("info")
    if not version or not info:
        raise ValueError("seed 请求缺少 encryptKeyVersion 或 info")

    keystream = _load_keystream(version)
    if not keystream and raw_game_over_request:
        keystream = _try_save_keystream_from_game_over(raw_game_over_request, version)
    if not keystream and raw_game_over_request is None and sys.stdin.isatty():
        raw_game_over = _paste_one_line(
            f"\n本地没有 v{version} keystream。"
            "可粘贴同版本 /sheep/v1/game/game_over_ex 的 Raw 目录或 Request，"
            "直接回车则跳过："
        )
        if raw_game_over:
            keystream = _try_save_keystream_from_game_over(
                _read_game_over_capture(raw_game_over),
                version,
            )

    if not keystream:
        prefix_note = ""
        seed_2 = api_data.get("map_seed_2")
        if isinstance(seed_2, str) and seed_2:
            try:
                prefix_len = len(derive_keystream_from_request(info, seed_2))
                prefix_note = (
                    f"\n这次 seed Request 只能推出 {prefix_len} 字节前缀，"
                    "seed Response 通常需要 38 字节才能解出完整 mapSeed。"
                )
            except ValueError:
                prefix_note = ""
        raise ValueError(
            f"本地没有 encryptKeyVersion={version} 的完整 OFB keystream。"
            f"{prefix_note}\n"
            "需要先抓一次同版本 game_over_ex，然后运行：\n"
            "python3 scripts/seed_tool.py derive-game-over '<Raw...folder>'"
        )

    if len(keystream) < 37:
        raise ValueError(f"keystream 只有 {len(keystream)} 字节，不足以解 seed 响应")

    seed_response = _replay_seed_request(raw_seed_request)
    out_resp = DATA_DIR / f"seed_response_v{version}_latest.bin"
    out_resp.write_bytes(seed_response)

    plain = _xor(seed_response, keystream)
    ack = decode_seed_ack(plain)
    if ack.code != 1 or len(ack.map_seed) != 4:
        raise ValueError(
            "seed 响应解密后不是完整成功结果："
            + json.dumps({"code": ack.code, "map_seed": ack.map_seed}, ensure_ascii=False)
        )

    return _apply_seed_ack(api_data, ack)


def _apply_seed_ack(api_data: dict, ack) -> dict:
    api_data = dict(api_data)
    api_data["map_seed"] = [int(x) for x in ack.map_seed]
    api_data["map_seed_2"] = ack.map_seed_2 or api_data.get("map_seed_2")

    saved = DATA_DIR / "daily_latest_with_real_seed.json"
    saved.write_text(json.dumps(api_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已解出真实 map_seed: {api_data['map_seed']}")
    print(f"已保存带真实 seed 的 JSON: {saved}")
    return api_data


def read_daily_api_data() -> dict:
    raw_map = _paste_one_line(
        "\n[1/2] 请粘贴 /sheep/v1/game/map_info_ex 的 Response JSON（或文件路径）："
    )
    capture_dir = _as_existing_dir(raw_map)
    raw_seed_from_dir = None
    raw_game_over_from_dir = None
    if capture_dir:
        raw_map, raw_seed_from_dir, raw_game_over_from_dir = _load_daily_capture_dir(capture_dir)

    parsed = _parse_json_or_http(raw_map)
    data = parsed.get("data", parsed)
    api_data = _build_api_data(data, data)

    if any(api_data["map_seed"]):
        print("map_info_ex 已包含真实 map_seed，不需要 seed 请求。")
        return api_data

    raw_seed = raw_seed_from_dir or _paste_one_line(
        "\n[2/2] 请粘贴 /sheep/v1/game/map_info_ex_seed 的 Request（Raw HTTP），"
        "或 seed Response 的十六进制 bytes（或文件路径）："
    )
    return _resolve_daily_seed(api_data, raw_seed, raw_game_over_from_dir)


# ── 地图下载 + 信息展示 ───────────────────────────────────────────────────────

def fetch_both_maps(api_data: dict) -> dict[int, dict]:
    """
    下载并解析 map_md5[0] 和 map_md5[1]，返回 {0: map_data, 1: map_data}。
    每张地图都注入 map_seed，供 solver._normalize_map_data 填充 type。
    """
    from map_fetcher import fetch_and_parse

    md5_list  = api_data["map_md5"]
    map_seed  = api_data["map_seed"]
    map_seed_2 = api_data.get("map_seed_2")
    count     = min(len(md5_list), 2)

    maps: dict[int, dict] = {}
    for i in range(count):
        print(f"\n── 第 {i+1} 关 ──────────────────────")
        maps[i] = fetch_and_parse(md5_list, map_seed, map_seed_2, index=i)

    return maps


def _print_map_summary(maps: dict[int, dict]):
    print("\n┌─ 地图概览 " + "─" * 38)
    for i, md in maps.items():
        tiles = sum(len(v) for v in md.get("levelData", {}).values())
        print(f"│  第 {i+1} 关  {md.get('levelKey','?')!s:>10}  "
              f"层={len(md.get('layers',[]))}  格={tiles}")
    print("└" + "─" * 49)


def confirm_before_click(solution_steps: int, total_tiles: int) -> str:
    """在自动点击前请求用户确认；返回 y/n/r 三种动作。"""
    is_partial = solution_steps < total_tiles
    try:
        if is_partial:
            answer = input(
                f"当前解法是从第 0 步开始的最佳部分解（{solution_steps}/{total_tiles} 步），"
                "只会点击到这个停点，后续可手动使用道具。"
                "是否开始自动点击？[y/n/r]（r=重新校准） "
            ).strip().lower()
            return answer if answer in {"y", "n", "r"} else "n"

        answer = input(
            f"求解已完成（{solution_steps}/{total_tiles} 步），"
            "是否开始自动点击？[y/n/r]（r=重新校准） "
        ).strip().lower()
    except EOFError:
        return "n"
    return answer if answer in {"y", "n", "r"} else "n"


def prepare_click_calibration(
    *,
    label: str,
    map_data: dict,
    solution: list[str],
    total_tiles: int,
    preview_path: Path,
) -> dict | None:
    """确认自动点击；输入 r 时重新校准并重建点击预览。"""
    from calibrate import ALIGNMENT_PREVIEW_FILE, load_calibration, run_calibration

    calib = load_calibration()
    while True:
        action = confirm_before_click(len(solution), total_tiles)
        if action == "y":
            return calib
        if action == "n":
            return None

        print(f"\n正在重新校准{label}牌区位置……")
        run_calibration(map_data=map_data)
        print(f"{label}地图预计点击点位预览图: {ALIGNMENT_PREVIEW_FILE}")
        calib = load_calibration()
        export_click_preview(label, calib, map_data, solution, preview_path)


def confirm_preview_ready(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def wait_for_next_level() -> bool:
    """等待用户进入第二关后输入 n。"""
    try:
        answer = input("\n第一关点击完成后，请手动进入第二关；准备好后输入 n 生成第二关点击预览：").strip().lower()
    except EOFError:
        return False
    return answer == "n"


def _iter_level_cards(map_data: dict) -> list[dict]:
    cards: list[dict] = []
    for key in sorted((map_data.get("levelData") or {}).keys(), key=int):
        cards.extend(map_data["levelData"][key])
    return cards


def build_first_level_click_sequence(map_data: dict) -> list[str]:
    """第一关通常不用求解，直接按稳定顺序点完全部格子。"""
    cards = _iter_level_cards(map_data)

    def sort_key(card: dict) -> tuple[int, int, int, str]:
        card_id = str(card.get("id", "0-0-0"))
        parts = card_id.split("-")
        try:
            layer = int(parts[0])
        except (ValueError, IndexError):
            layer = int(card.get("layerNum", 0) or 0)
        return (
            -layer,
            int(card.get("rowNum", 0) or 0),
            int(card.get("rolNum", 0) or 0),
            card_id,
        )

    return [str(card["id"]) for card in sorted(cards, key=sort_key) if card.get("id")]


def save_solution_file(
    *,
    level: int,
    md5: str,
    algorithm: str,
    solution: list[str],
    total_tiles: int,
    is_partial: bool,
    mode: str,
) -> Path:
    out = DATA_DIR / "parsed" / f"solution_level{level}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump({
            "level": level,
            "md5": md5,
            "algorithm": algorithm,
            "mode": mode,
            "is_partial": is_partial,
            "total_tiles": total_tiles,
            "steps": len(solution),
            "solution": solution,
        }, f, indent=2, ensure_ascii=False)
    print(f"解法已保存: {out}")
    return out


def export_click_preview(label: str, calib: dict, map_data: dict, solution: list[str], output_path: Path):
    from calibrate import export_solution_preview_from_current_window

    try:
        preview = export_solution_preview_from_current_window(
            calib["grid_rel"],
            map_data,
            solution,
            output_path=output_path,
            highlight_count=10,
        )
        print(f"{label}点击预览图: {preview}")
    except RuntimeError as e:
        print(f"警告：无法生成{label}点击预览图：{e}")


# ── run 命令 ──────────────────────────────────────────────────────────────────

def cmd_run(args):
    cfg       = load_config()
    delay     = args.delay       if args.delay       is not None else cfg["click_delay"]
    first_delay = (
        args.first_delay
        if args.first_delay is not None
        else cfg.get("first_click_delay", delay)
    )
    start_delay = (
        args.start_delay
        if args.start_delay is not None
        else cfg.get("start_delay", 3.0)
    )
    warmup_steps = (
        args.warmup_steps
        if args.warmup_steps is not None
        else cfg.get("warmup_steps", 5)
    )
    warmup_delay = (
        args.warmup_delay
        if args.warmup_delay is not None
        else cfg.get("warmup_click_delay", 0.35)
    )
    pa        = args.pause_after if args.pause_after is not None else cfg["pause_after"]
    algorithm = args.algorithm   or cfg["algorithm"]
    level_idx = args.level - 1   # 用户传 1 或 2，转为 0-based

    from solver  import solve
    from clicker import execute_solution
    from calibrate import (
        ALIGNMENT_PREVIEW_FILE,
        SOLUTION_PREVIEW_FILE,
        DATA_DIR as CALIBRATE_DATA_DIR,
        export_solution_preview_from_current_window,
        load_calibration,
        run_calibration,
    )

    # ── 读取 API JSON ──
    try:
        api_data = read_daily_api_data() if args.daily else read_api_json(args)
    except (ValueError, OSError) as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)

    md5_count = len(api_data["map_md5"])
    if level_idx >= md5_count:
        print(f"错误：--level {args.level} 超出范围（该响应共 {md5_count} 张地图）",
              file=sys.stderr)
        sys.exit(1)

    # ── 下载并解析两关 ──
    maps = fetch_both_maps(api_data)
    _print_map_summary(maps)

    # ── 单关模式：只有一张地图，或用户明确运行第一关时沿用旧流程 ──
    if len(maps) < 2 or args.level == 1:
        _run_single_level(
            args=args,
            api_data=api_data,
            maps=maps,
            cfg=cfg,
            delay=delay,
            pause_after=pa,
            algorithm=algorithm,
            level_idx=level_idx,
            start_delay=start_delay,
            warmup_steps=warmup_steps,
            warmup_delay=warmup_delay,
        )
        return

    # ── 默认两关流程：先求解第二关，再点击第一关，最后确认点击第二关 ──
    first = maps[0]
    target = maps[level_idx]
    first_total_tiles = sum(len(v) for v in first.get("levelData", {}).values())
    second_total_tiles = sum(len(v) for v in target.get("levelData", {}).values())

    print(f"\n正在求解第 {level_idx+1} 关……")
    solution = solve(target, cfg["solver"], algorithm)
    is_partial = len(solution) < second_total_tiles

    save_solution_file(
        level=level_idx + 1,
        md5=api_data["map_md5"][level_idx],
        algorithm=algorithm,
        solution=solution,
        total_tiles=second_total_tiles,
        is_partial=is_partial,
        mode="solved",
    )
    if is_partial:
        print(
            f"提示：第二关当前为从第 0 步开始的部分解（{len(solution)}/{second_total_tiles} 步），"
            "自动点击会停在这里，不会继续猜后续步骤。"
        )

    print("\n现在处理第一关。请保持微信窗口在第一关牌面。")
    run_calibration(map_data=first)
    print(f"第一关地图预计点击点位预览图: {ALIGNMENT_PREVIEW_FILE}")

    calib = load_calibration()
    first_clicks = build_first_level_click_sequence(first)
    save_solution_file(
        level=1,
        md5=api_data["map_md5"][0],
        algorithm="click-all",
        solution=first_clicks,
        total_tiles=first_total_tiles,
        is_partial=len(first_clicks) < first_total_tiles,
        mode="click_all",
    )
    export_click_preview(
        "第一关",
        calib,
        first,
        first_clicks,
        CALIBRATE_DATA_DIR / "solution_preview_level1.png",
    )

    calib = prepare_click_calibration(
        label="第一关",
        map_data=first,
        solution=first_clicks,
        total_tiles=first_total_tiles,
        preview_path=CALIBRATE_DATA_DIR / "solution_preview_level1.png",
    )
    if calib is None:
        print("已取消第一关自动点击。")
        return

    execute_solution(
        first_clicks,
        map_data=first,
        calib=calib,
        delay=first_delay,
        pause_after=pa,
        step_mode=args.step,
        start_delay=start_delay,
        warmup_steps=warmup_steps,
        warmup_delay=warmup_delay,
    )

    if not wait_for_next_level():
        print("未进入第二关点击预览，已停止。")
        return

    print("\n正在重新校准第二关牌区位置……")
    run_calibration(map_data=target)
    print(f"第二关地图预计点击点位预览图: {ALIGNMENT_PREVIEW_FILE}")
    calib = load_calibration()

    export_click_preview(
        "第二关",
        calib,
        target,
        solution,
        SOLUTION_PREVIEW_FILE,
    )

    calib = prepare_click_calibration(
        label="第二关",
        map_data=target,
        solution=solution,
        total_tiles=second_total_tiles,
        preview_path=SOLUTION_PREVIEW_FILE,
    )
    if calib is None:
        print("已取消第二关自动点击。")
        return

    execute_solution(
        solution,
        map_data=target,
        calib=calib,
        delay=delay,
        pause_after=pa,
        step_mode=args.step,
        start_delay=start_delay,
        warmup_steps=warmup_steps,
        warmup_delay=warmup_delay,
    )


def _run_single_level(
    *,
    args,
    api_data: dict,
    maps: dict[int, dict],
    cfg: dict,
    delay: float,
    pause_after: int,
    algorithm: str,
    level_idx: int,
    start_delay: float,
    warmup_steps: int,
    warmup_delay: float | None,
):
    from solver  import solve
    from clicker import execute_solution
    from calibrate import (
        ALIGNMENT_PREVIEW_FILE,
        SOLUTION_PREVIEW_FILE,
        export_solution_preview_from_current_window,
        load_calibration,
        run_calibration,
    )

    target = maps[level_idx]
    total_tiles = sum(len(v) for v in target.get("levelData", {}).values())

    print(f"\n正在校准第 {level_idx+1} 关牌区位置……")
    run_calibration(map_data=target)
    print(f"地图预计点击点位预览图: {ALIGNMENT_PREVIEW_FILE}")
    if not confirm_preview_ready("确认点位预览无误，开始求解？[y/N] "):
        print("已取消求解。")
        return

    print(f"\n正在求解第 {level_idx+1} 关……")
    solution = solve(target, cfg["solver"], algorithm)
    is_partial = len(solution) < total_tiles

    save_solution_file(
        level=level_idx + 1,
        md5=api_data["map_md5"][level_idx],
        algorithm=algorithm,
        solution=solution,
        total_tiles=total_tiles,
        is_partial=is_partial,
        mode="solved",
    )
    if is_partial:
        print(
            f"提示：当前为从第 0 步开始的部分解（{len(solution)}/{total_tiles} 步），"
            "自动点击会停在这里，不会继续猜后续步骤。"
        )

    calib = load_calibration()
    try:
        solution_preview = export_solution_preview_from_current_window(
            calib["grid_rel"],
            target,
            solution,
            output_path=SOLUTION_PREVIEW_FILE,
            highlight_count=10,
        )
        print(f"前 10 步预计点击预览图: {solution_preview}")
    except RuntimeError as e:
        print(f"警告：无法生成前 10 步预览图：{e}")

    calib = prepare_click_calibration(
        label=f"第 {level_idx+1} 关",
        map_data=target,
        solution=solution,
        total_tiles=total_tiles,
        preview_path=SOLUTION_PREVIEW_FILE,
    )
    if calib is None:
        print("已取消自动点击。")
        return

    execute_solution(
        solution,
        map_data=target,
        calib=calib,
        delay=delay,
        pause_after=pause_after,
        step_mode=args.step,
        start_delay=start_delay,
        warmup_steps=warmup_steps,
        warmup_delay=warmup_delay,
    )


# ── calibrate 命令 ────────────────────────────────────────────────────────────

def cmd_calibrate(_args):
    from calibrate import run_calibration
    run_calibration()


def cmd_preview(_args):
    from calibrate import export_alignment_preview_from_current_window

    preview_path = export_alignment_preview_from_current_window()
    print(f"对齐预览图已生成: {preview_path}")


# ── 参数解析 ──────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sheeptool",
        description="SheepTool — 羊了个羊自动化助手",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例：

  # 首次校准（把微信调到游戏界面再运行）
  python main.py calibrate

  # 默认串两关：粘贴/读取数据后先求解第二关，再点击第一关，最后确认点击第二关
  python main.py run --file api_response.json

  # 交互式粘贴，粘贴完成后按 Ctrl+D
  python main.py run

  # 单独跑第一关时使用单关旧流程
  python main.py run --file api_response.json --level 1

  # 直接传入 JSON 字符串
  python main.py run --json '{"err_code":0,"data":{...}}'

  # 单步模式（每步按 n 确认），更快点击
  python main.py run --step --delay 0.25

  # 每 10 步暂停
  python main.py run --pause-after 10
        """,
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # calibrate
    sub.add_parser("calibrate", help="校准微信窗口中的牌局区域")
    sub.add_parser("preview", help="生成当前窗口的对齐预览图")

    # run
    p = sub.add_parser("run", help="输入 API JSON → 下载→解析→求解第二关→点击两关")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--json", metavar="JSON",
                     help="直接传入 API 响应 JSON 字符串")
    src.add_argument("--file", metavar="FILE",
                     help="从文件读取 API 响应 JSON")
    src.add_argument("--daily", action="store_true",
                     help="每日关卡交互模式：依次粘贴 map_info_ex Response 和 seed Request")
    p.add_argument("--level", type=int, choices=[1, 2], default=2,
                   help="运行第几关。默认 2 会串联第一关点击和第二关求解；1 使用单关流程")
    p.add_argument("--delay", type=float, metavar="SEC",
                   help="点击间隔秒数（默认 0.4）")
    p.add_argument("--first-delay", type=float, dest="first_delay", metavar="SEC",
                   help="第一关点击间隔秒数，单独配置，可设为 0 或负数表示尽量不等待")
    p.add_argument("--start-delay", type=float, dest="start_delay", metavar="SEC",
                   help="确认开始后倒计时秒数，期间会先激活微信窗口（默认 3）")
    p.add_argument("--warmup-steps", type=int, dest="warmup_steps", metavar="N",
                   help="开头使用慢速保护的步数（默认 5）")
    p.add_argument("--warmup-delay", type=float, dest="warmup_delay", metavar="SEC",
                   help="开头保护步的最小点击间隔秒数（默认 0.35）")
    p.add_argument("--pause-after", type=int, dest="pause_after", metavar="N",
                   help="每 N 步自动暂停")
    p.add_argument("--step", action="store_true",
                   help="单步模式（每步需按 n 确认）")
    p.add_argument("--algorithm", choices=ALGORITHMS, metavar="MODE",
                   help=f"求解算法（默认 normal）。可选：{', '.join(ALGORITHMS)}")

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()
    try:
        {
            "calibrate": cmd_calibrate,
            "preview": cmd_preview,
            "run": cmd_run,
        }[args.command](args)
    except FileNotFoundError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(0)


if __name__ == "__main__":
    main()
