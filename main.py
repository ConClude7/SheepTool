#!/usr/bin/env python3
"""
SheepTool — 羊了个羊自动化助手

典型流程：
  1. 进入关卡后从抓包/Network 面板复制 API 响应 JSON
  2. python main.py run                    （粘贴 JSON → 解析地图→校准→求解→点击）

完整命令：
  calibrate               校准微信窗口中的牌局区域
  preview                 生成当前窗口的对齐预览图（检查校准是否准确）
  run [选项]              主命令：输入 API JSON → 下载→解析→求解→点击
    --json  JSON字符串    直接传入 API 响应（省去交互粘贴）
    --file  FILE          从文件读取 API 响应
    --level 1|2           运行第几关（默认 2）
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
              "index-ascending", "index-descending"]


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
    print("粘贴后直接回车确认；也可以输入本地文件路径。")
    try:
        value = input("> ").strip()
    except EOFError:
        return ""
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


def _resolve_daily_seed(api_data: dict, raw_seed_request: str) -> dict:
    from scripts.seed_tool import _xor, decode_seed_ack

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
    if not keystream:
        raise ValueError(
            f"本地没有 encryptKeyVersion={version} 的 OFB keystream。\n"
            "需要先抓一次同版本 game_over_ex，并用 scripts/seed_tool.py derive 生成缓存。"
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
    parsed = _parse_json_or_http(raw_map)
    data = parsed.get("data", parsed)
    api_data = _build_api_data(data, data)

    if any(api_data["map_seed"]):
        print("map_info_ex 已包含真实 map_seed，不需要 seed 请求。")
        return api_data

    raw_seed = _paste_one_line(
        "\n[2/2] 请粘贴 /sheep/v1/game/map_info_ex_seed 的 Request（Raw HTTP），"
        "或 seed Response 的十六进制 bytes（或文件路径）："
    )
    return _resolve_daily_seed(api_data, raw_seed)


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


def confirm_before_click(solution_steps: int, total_tiles: int) -> bool:
    """在自动点击前请求用户确认；部分解默认更严格。"""
    is_partial = solution_steps < total_tiles
    try:
        if is_partial:
            answer = input(
                f"当前解法是从第 0 步开始的最佳部分解（{solution_steps}/{total_tiles} 步），"
                "只会点击到这个停点，后续可手动使用道具。"
                "确认开始点击请输入 yes，其它输入取消："
            ).strip().lower()
            return answer == "yes"

        answer = input(
            f"求解已完成（{solution_steps}/{total_tiles} 步），"
            "是否开始自动点击？[y/N] "
        ).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def confirm_preview_ready(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


# ── run 命令 ──────────────────────────────────────────────────────────────────

def cmd_run(args):
    cfg       = load_config()
    delay     = args.delay       if args.delay       is not None else cfg["click_delay"]
    pa        = args.pause_after if args.pause_after is not None else cfg["pause_after"]
    algorithm = args.algorithm   or cfg["algorithm"]
    level_idx = args.level - 1   # 用户传 1 或 2，转为 0-based

    from solver  import solve
    from clicker import execute_solution
    from calibrate import (
        ALIGNMENT_PREVIEW_FILE,
        SOLUTION_PREVIEW_FILE,
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

    # ── 目标关 + 校准点位预览 ──
    target = maps[level_idx]
    total_tiles = sum(len(v) for v in target.get("levelData", {}).values())

    print(f"\n正在校准第 {level_idx+1} 关牌区位置……")
    run_calibration(map_data=target)
    print(f"地图预计点击点位预览图: {ALIGNMENT_PREVIEW_FILE}")
    if not confirm_preview_ready("确认点位预览无误，开始求解？[y/N] "):
        print("已取消求解。")
        return

    # ── 求解目标关 ──
    print(f"\n正在求解第 {level_idx+1} 关……")
    solution = solve(target, cfg["solver"], algorithm)
    is_partial = len(solution) < total_tiles

    # ── 保存解法 ──
    out = DATA_DIR / "parsed" / f"solution_level{level_idx+1}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump({
            "level": level_idx + 1,
            "md5": api_data["map_md5"][level_idx],
            "algorithm": algorithm,
            "is_partial": is_partial,
            "total_tiles": total_tiles,
            "steps": len(solution),
            "solution": solution,
        }, f, indent=2, ensure_ascii=False)
    print(f"解法已保存: {out}")
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

    # ── 执行点击 ──
    if not confirm_before_click(len(solution), total_tiles):
        print("已取消自动点击。")
        return

    execute_solution(
        solution,
        map_data=target,
        calib=calib,
        delay=delay,
        pause_after=pa,
        step_mode=args.step,
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

  # 将 JSON 保存到文件再读取（match_data 含特殊字符时推荐）
  python main.py run --file api_response.json --level 1

  # 交互式粘贴，粘贴完成后按 Ctrl+D
  python main.py run

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
    p = sub.add_parser("run", help="输入 API JSON → 下载→解析→求解→点击")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--json", metavar="JSON",
                     help="直接传入 API 响应 JSON 字符串")
    src.add_argument("--file", metavar="FILE",
                     help="从文件读取 API 响应 JSON")
    src.add_argument("--daily", action="store_true",
                     help="每日关卡交互模式：依次粘贴 map_info_ex Response 和 seed Request")
    p.add_argument("--level", type=int, choices=[1, 2], default=2,
                   help="运行第几关（1 或 2，默认 2）")
    p.add_argument("--delay", type=float, metavar="SEC",
                   help="点击间隔秒数（默认 0.4）")
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
