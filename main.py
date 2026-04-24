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

运行期间快捷键：  p 暂停/继续   n 下一步   s 结束
"""
import argparse
import json
import sys
from pathlib import Path

DATA_DIR    = Path(__file__).parent / "data"
CONFIG_FILE = Path(__file__).parent / "config.json"

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

    if not isinstance(md5_list, list) or len(md5_list) == 0:
        raise ValueError("JSON 缺少 data.map_md5 数组")
    if not isinstance(seed_list, list) or len(seed_list) != 4:
        raise ValueError("JSON 缺少合法的 data.map_seed（需要 4 个数字）")

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
                f"当前解法是部分解（{solution_steps}/{total_tiles} 步），"
                "继续自动点击可能导致槽位满并失败。"
                "确认继续请输入 yes，其它输入取消："
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
        api_data = read_api_json(args)
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
        print(f"警告：当前仅为部分解（{len(solution)}/{total_tiles} 步），自动执行可能失败。")

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
