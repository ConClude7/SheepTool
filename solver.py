#!/usr/bin/env python3
import base64
import os
import queue
import struct
import sys
import time
import multiprocessing as mp
from pathlib import Path

SOLVER_DIR = Path(__file__).parent / "tools" / "solver"
if str(SOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVER_DIR))

from business.SheepSolver import SheepSolver
from core.data.ShuffleHelper import ShuffleHelper


# ── 地图数据预处理 ─────────────────────────────────────────────────────────────

def _shuffle_and_apply(
    map_data: dict,
    missing_cards: list,
    card_list: list,
    seed_list: list,
) -> dict:
    """用 seed_list 洗牌并将 type 写回缺失的牌。

    seed_list 的格式与 ShuffleHelper(seed_list) 一致：
    四个 uint32 整数，内部两两合并为 64-bit seed_1 / seed_2。
    """
    block_type_data = dict(
        sorted(map_data["blockTypeData"].items(), key=lambda x: int(x[0]))
    )
    type_list: list[int] = []
    for t, count in block_type_data.items():
        type_list.extend([int(t)] * count * 3)

    ShuffleHelper(seed_list).shuffle(type_list, len(type_list))
    type_list = list(reversed(type_list))

    if len(type_list) < len(missing_cards):
        raise ValueError(
            f"地图可还原的 tile type 数量不足：需要 {len(missing_cards)}，"
            f"实际只有 {len(type_list)}。"
        )

    for card, t in zip(missing_cards, type_list):
        card["type"] = t

    still_missing = [c["id"] for c in card_list if "type" not in c or c.get("type", 0) == 0]
    if still_missing:
        raise ValueError(
            "地图 type 还原后仍有缺失，前几个缺失项："
            + ", ".join(still_missing[:5])
        )
    return map_data


def _normalize_map_data(map_data: dict) -> dict:
    """用 map_seed 填充缺失的 tile type。"""
    if isinstance(map_data.get("data"), dict):
        map_data = map_data["data"]

    if not isinstance(map_data.get("levelData"), dict):
        raise ValueError("Invalid map data: missing levelData")

    card_list = []
    for key in sorted(map_data["levelData"].keys(), key=int):
        card_list.extend(map_data["levelData"][key])

    # protobuf 对值为 0 的字段不写入，补全默认值
    for card in card_list:
        card.setdefault("rolNum", 0)
        card.setdefault("rowNum", 0)
        card.setdefault("layerNum", 1)
        card.setdefault("moldType", 1)

    if all("type" in c and c.get("type", 0) != 0 for c in card_list):
        return map_data

    missing_cards = [c for c in card_list if "type" not in c or c.get("type", 0) == 0]

    # ── 路径1：map_seed（四元 uint32 数组）────────────────────────────────────────
    # 正常局次由服务端直接下发，四个整数均非零。
    map_seed = map_data.get("map_seed")
    if isinstance(map_seed, list) and len(map_seed) == 4 and any(s != 0 for s in map_seed):
        return _shuffle_and_apply(map_data, missing_cards, card_list, map_seed)

    # ── 路径2：map_seed_2 不能直接当作本地 seed ─────────────────────────────────
    # 官方客户端在 need_seed=true 时会把 map_seed_2 发给
    # /sheep/v1/game/map_info_ex_seed，再由服务端返回真正的 mapSeed 数组。
    # 这个请求还会在 need_wx_encrypt=true 时使用 wx.getUserCryptoManager()
    # 返回的用户密钥做 AES-OFB 加密。直接把 map_seed_2 Base64 解成整数会得到
    # 一组错误 seed，生成的牌面会和游戏实机渲染不一致。
    map_seed_2 = map_data.get("map_seed_2")
    if map_seed_2 and isinstance(map_seed_2, str):
        raise ValueError(
            "地图缺少 tile type，且接口返回 need_seed=true / map_seed 全为 0。\n"
            f"map_seed_2=\"{map_seed_2}\" 需要先通过官方 seed 接口换取真实 mapSeed，"
            "不能直接 Base64 解码作为洗牌种子。"
        )

    raise ValueError(
        "地图缺少 tile type，且 map_seed / map_seed_2 均无法还原。\n"
        "请提供包含 tile type 或有效种子的完整地图数据。"
    )


# ── 单次求解 ──────────────────────────────────────────────────────────────────

def _run_once(map_data: dict, cfg: dict, algorithm: str) -> tuple:
    """运行一次求解，返回 (result_or_None, elapsed, maximum_progress, solver)。"""
    sheep_solver = SheepSolver(cfg, algorithm)
    sheep_solver.load_map_data(map_data)
    start = time.time()
    sheep_solver.solve()
    elapsed = time.time() - start
    sheep_solver.emit_progress_snapshot(force=True)
    return sheep_solver.generate_card_id_result(), elapsed, sheep_solver.get_maximum_progress(), sheep_solver


# ── 多进程 worker ─────────────────────────────────────────────────────────────

_SOLVER_DIR = str(Path(__file__).parent / "tools" / "solver")

def _ensure_solver_path():
    if _SOLVER_DIR not in sys.path:
        sys.path.insert(0, _SOLVER_DIR)

def _worker_fn(args: tuple) -> tuple:
    """随机算法子进程入口：返回 (result, partial, max_progress, elapsed, attempt_num)。"""
    _ensure_solver_path()
    map_data, cfg, attempt_num = args
    result, elapsed, max_progress, solver = _run_once(map_data, cfg, "random")
    partial = solver.generate_best_partial_card_id_result()
    return result, partial, max_progress, elapsed, attempt_num

def _worker_fn_deterministic(args: tuple) -> tuple:
    """确定性算法子进程入口：返回 (result, partial, max_progress, elapsed, algorithm)。"""
    _ensure_solver_path()
    map_data, cfg, algorithm = args
    result, elapsed, max_progress, solver = _run_once(map_data, cfg, algorithm)
    partial = solver.generate_best_partial_card_id_result()
    return result, partial, max_progress, elapsed, algorithm


# ── 确定性算法列表 ────────────────────────────────────────────────────────────

_DETERMINISTIC_ALGORITHMS = [
    "normal",
    "level-top",
    "level-bottom",
    "index-ascending",
    "index-descending",
]


# ── 默认配置 ──────────────────────────────────────────────────────────────────

_DEFAULT_PER_ATTEMPT_SEC = 30
_DEFAULT_MAX_ATTEMPTS    = 30
_DEFAULT_WORKERS         = 0       # 0 = 自动取 cpu_count
_DEFAULT_PARTIAL_ACCEPT  = 0.0     # 0 = 不接受部分解


# ── 实时进度显示 ──────────────────────────────────────────────────────────────

def _format_progress(progress: float) -> str:
    return f"{progress:.1%}"


def _random_attempt_label(attempt_num: int) -> str:
    return f"random#{attempt_num}"


def _colorize(text: str, color: str, enabled: bool) -> str:
    if not enabled:
        return text
    palette = {
        "dim": "2",
        "bold": "1",
        "cyan": "36",
        "blue": "34",
        "yellow": "33",
        "green": "32",
        "red": "31",
        "magenta": "35",
        "gray": "90",
    }
    code = palette.get(color)
    if not code:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


class _LiveProgressDisplay:
    def __init__(self, title: str, labels: list[str], visible_rows: int | None = None):
        self.title = title
        self.labels = list(labels)
        self.total_jobs = len(self.labels)
        self.visible_rows = visible_rows or len(self.labels)
        self.start_time = time.time()
        self.best_progress = 0.0
        self._statuses = {
            label: {
                "current": 0.0,
                "max": 0.0,
                "status": "排队中",
                "elapsed": None,
                "finished_at": None,
                "steps": None,
            }
            for label in self.labels
        }
        self._last_render_line_count = 0
        self._last_render_at = 0.0
        self._supports_ansi = sys.stdout.isatty()

    def update_progress(self, label: str, current: float, maximum: float):
        status = self._statuses.setdefault(label, {
            "current": 0.0,
            "max": 0.0,
            "status": "排队中",
            "elapsed": None,
            "finished_at": None,
            "steps": None,
        })
        status["current"] = current
        status["max"] = max(status["max"], maximum)
        if status["status"] == "排队中":
            status["status"] = "运行中"
        self.best_progress = max(self.best_progress, status["max"])

    def mark_result(
        self,
        label: str,
        *,
        success: bool,
        elapsed: float,
        max_progress: float,
        current_progress: float | None = None,
        steps: int | None = None,
    ):
        status = self._statuses.setdefault(label, {
            "current": 0.0,
            "max": 0.0,
            "status": "排队中",
            "elapsed": None,
            "finished_at": None,
            "steps": None,
        })
        if current_progress is not None:
            status["current"] = current_progress
        status["max"] = max(status["max"], max_progress)
        status["status"] = "成功" if success else "失败"
        status["elapsed"] = elapsed
        status["finished_at"] = time.time()
        status["steps"] = steps
        self.best_progress = max(self.best_progress, status["max"])

    def render(self, force: bool = False):
        now = time.time()
        if not force and now - self._last_render_at < 0.12:
            return
        self._last_render_at = now
        lines = self._build_lines()

        if not self._supports_ansi:
            print("\n".join(lines), flush=True)
            return

        if self._last_render_line_count:
            sys.stdout.write(f"\x1b[{self._last_render_line_count}F")

        max_lines = max(self._last_render_line_count, len(lines))
        for idx in range(max_lines):
            line = lines[idx] if idx < len(lines) else ""
            sys.stdout.write("\x1b[2K" + line + "\n")
        sys.stdout.flush()
        self._last_render_line_count = max_lines

    def close(self):
        self.render(force=True)
        if self._supports_ansi and self._last_render_line_count:
            sys.stdout.write("\n")
            sys.stdout.flush()

    def _build_lines(self) -> list[str]:
        total_elapsed = time.time() - self.start_time
        completed = sum(
            1
            for status in self._statuses.values()
            if status["status"] in {"成功", "失败"}
        )
        visible_labels = self._get_visible_labels()
        best_text = _colorize(_format_progress(self.best_progress), "green", self._supports_ansi)
        lines = [
            _colorize(
                f"求解实时进度 | 总计算时间 {total_elapsed:.1f}s | 最佳进度 {best_text} | 已完成 {completed}/{self.total_jobs}",
                "bold",
                self._supports_ansi,
            ),
            f"- 模式: {_colorize(self.title, 'cyan', self._supports_ansi)}",
        ]

        for label in visible_labels:
            status = self._statuses[label]
            current = _colorize(_format_progress(status["current"]), "blue", self._supports_ansi)
            maximum = _colorize(_format_progress(status["max"]), "green", self._supports_ansi)
            status_text = self._format_status(status["status"])
            extra = f" | 用时 {_colorize(f'{status['elapsed']:.2f}s', 'magenta', self._supports_ansi)}" if status["elapsed"] is not None else ""
            if status["steps"] is not None and status["status"] == "成功":
                extra += f" | 步数 {_colorize(str(status['steps']), 'cyan', self._supports_ansi)}"
            lines.append(
                f"- {_colorize(label, 'yellow', self._supports_ansi)}: 当前 {current} | 最大 {maximum} | 状态 {status_text}{extra}"
            )

        hidden = max(len(self._statuses) - len(visible_labels), 0)
        if hidden > 0:
            lines.append(f"- {_colorize(f'其余 {hidden} 个尝试未展开显示', 'dim', self._supports_ansi)}")
        return lines

    def _get_visible_labels(self) -> list[str]:
        if len(self.labels) <= self.visible_rows:
            return self.labels

        running = [
            label for label in self.labels
            if self._statuses[label]["status"] == "运行中"
        ]
        recent_finished = sorted(
            (
                label for label in self.labels
                if self._statuses[label]["status"] in {"成功", "失败"}
            ),
            key=lambda item: self._statuses[item]["finished_at"] or 0,
            reverse=True,
        )
        queued = [
            label for label in self.labels
            if self._statuses[label]["status"] == "排队中"
        ]

        result: list[str] = []
        for group in (running, recent_finished, queued):
            for label in group:
                if label not in result:
                    result.append(label)
                if len(result) >= self.visible_rows:
                    return result
        return result

    def _format_status(self, status: str) -> str:
        color = {
            "排队中": "gray",
            "运行中": "yellow",
            "成功": "green",
            "失败": "red",
        }.get(status, "dim")
        return _colorize(status, color, self._supports_ansi)


def _drain_progress_queue(progress_queue, display: _LiveProgressDisplay):
    if progress_queue is None:
        return
    while True:
        try:
            event = progress_queue.get_nowait()
        except queue.Empty:
            break
        except Exception:
            break

        if event.get("type") != "progress":
            continue
        display.update_progress(
            event["label"],
            event.get("current_progress", 0.0),
            event.get("maximum_progress", 0.0),
        )


# ── 主入口 ────────────────────────────────────────────────────────────────────

def solve(map_data: dict, solver_config: dict, algorithm: str = "normal") -> list:
    map_data = _normalize_map_data(map_data)

    partial_accept  = solver_config.get("partial_accept", _DEFAULT_PARTIAL_ACCEPT)
    per_attempt_sec = solver_config.get("random_attempt_sec", _DEFAULT_PER_ATTEMPT_SEC)
    max_attempts    = solver_config.get("random_attempts",    _DEFAULT_MAX_ATTEMPTS)
    n_workers       = solver_config.get("random_workers",     _DEFAULT_WORKERS)

    # 探针：获取格子总数
    probe = SheepSolver(solver_config, algorithm)
    probe.load_map_data(map_data)
    total = probe._card_count

    if algorithm == "random":
        return _solve_random_parallel(
            map_data, solver_config, total,
            per_attempt_sec=per_attempt_sec,
            max_attempts=max_attempts,
            n_workers=n_workers,
            partial_accept=partial_accept,
        )

    # ── 所有确定性算法并行启动，取最先找到的解 ──
    result = _solve_deterministic_parallel(
        map_data, solver_config, total, partial_accept=partial_accept,
    )
    if result is not None:
        return result

    # 全部失败，切换到 random 多进程重启
    print("  所有确定性算法均未找到完整解，自动切换 random 多进程重启……")
    return _solve_random_parallel(
        map_data, solver_config, total,
        per_attempt_sec=per_attempt_sec,
        max_attempts=max_attempts,
        n_workers=n_workers,
        partial_accept=partial_accept,
    )


# ── 确定性算法并行 ────────────────────────────────────────────────────────────

def _solve_deterministic_parallel(
    map_data: dict,
    solver_config: dict,
    total: int,
    partial_accept: float = _DEFAULT_PARTIAL_ACCEPT,
) -> list | None:
    """并行运行所有确定性算法，返回最先找到的完整解；全部失败返回 None。"""
    n_algos = len(_DETERMINISTIC_ALGORITHMS)
    n_workers = min(n_algos, os.cpu_count() or 4)
    show_progress = solver_config.get("show_progress", True)

    attempt_cfg = {**solver_config, "show_progress": False}

    print(
        f"求解 {total} 个格子（{n_algos} 种确定性算法并行，{n_workers} 进程）……"
    )
    overall_start = time.time()

    best_partial: list | None = None
    best_partial_progress: float = 0.0

    if not show_progress:
        tasks = [(map_data, attempt_cfg, algo) for algo in _DETERMINISTIC_ALGORITHMS]
        with mp.Pool(processes=n_workers) as pool:
            for result, partial, max_progress, elapsed, algorithm in pool.imap_unordered(
                _worker_fn_deterministic, tasks
            ):
                total_elapsed = time.time() - overall_start

                if result is not None:
                    pool.terminate()
                    print(
                        f"  算法 {algorithm} 求解成功，单次 {elapsed:.2f}s，"
                        f"总耗时 {total_elapsed:.2f}s — 共 {len(result)} 步"
                    )
                    return result

                if partial and max_progress > best_partial_progress:
                    best_partial_progress = max_progress
                    best_partial = partial

                print(
                    f"  算法 {algorithm} 未找到完整解（{elapsed:.2f}s，进度 {max_progress:.1%}）",
                    flush=True,
                )

                if partial_accept > 0 and best_partial_progress >= partial_accept:
                    pool.terminate()
                    print(
                        f"  进度 {best_partial_progress:.1%} ≥ partial_accept={partial_accept:.0%}，"
                        f"采用部分解（{len(best_partial)} 步），总耗时 {total_elapsed:.2f}s"
                    )
                    return best_partial
        return None

    with mp.Manager() as manager:
        progress_queue = manager.Queue()
        tasks = [
            (
                map_data,
                {
                    **attempt_cfg,
                    "progress_queue": progress_queue,
                    "progress_label": algo,
                    "progress_emit_interval": 0.2,
                },
                algo,
            )
            for algo in _DETERMINISTIC_ALGORITHMS
        ]
        display = _LiveProgressDisplay(
            title=f"确定性并行 x{n_algos}",
            labels=list(_DETERMINISTIC_ALGORITHMS),
        )

        with mp.Pool(processes=n_workers) as pool:
            iterator = pool.imap_unordered(_worker_fn_deterministic, tasks)
            remaining = len(tasks)
            while remaining > 0:
                _drain_progress_queue(progress_queue, display)
                try:
                    result, partial, max_progress, elapsed, algorithm = iterator.next(timeout=0.2)
                except mp.TimeoutError:
                    display.render()
                    continue

                remaining -= 1
                total_elapsed = time.time() - overall_start

                if partial and max_progress > best_partial_progress:
                    best_partial_progress = max_progress
                    best_partial = partial

                display.mark_result(
                    algorithm,
                    success=result is not None,
                    elapsed=elapsed,
                    max_progress=1.0 if result is not None else max_progress,
                    current_progress=1.0 if result is not None else 0.0,
                    steps=len(result) if result is not None else None,
                )
                _drain_progress_queue(progress_queue, display)
                display.render(force=True)

                if result is not None:
                    pool.terminate()
                    display.close()
                    print(
                        f"  算法 {algorithm} 求解成功，单次 {elapsed:.2f}s，"
                        f"总耗时 {total_elapsed:.2f}s — 共 {len(result)} 步"
                    )
                    return result

                if partial_accept > 0 and best_partial_progress >= partial_accept:
                    pool.terminate()
                    display.close()
                    print(
                        f"  进度 {best_partial_progress:.1%} ≥ partial_accept={partial_accept:.0%}，"
                        f"采用部分解（{len(best_partial)} 步），总耗时 {total_elapsed:.2f}s"
                    )
                    return best_partial

        display.close()

    return None


# ── 并行随机重启 ──────────────────────────────────────────────────────────────

def _solve_random_parallel(
    map_data: dict,
    solver_config: dict,
    total: int,
    per_attempt_sec: int  = _DEFAULT_PER_ATTEMPT_SEC,
    max_attempts: int     = _DEFAULT_MAX_ATTEMPTS,
    n_workers: int        = _DEFAULT_WORKERS,
    partial_accept: float = _DEFAULT_PARTIAL_ACCEPT,
) -> list:
    n_workers = n_workers or (os.cpu_count() or 4)
    n_workers = min(n_workers, max_attempts)
    show_progress = solver_config.get("show_progress", True)

    attempt_cfg = {
        **solver_config,
        "show_progress":   False,
        "time_limit":      per_attempt_sec,
        "expect_progress": {"time": -1, "percentage": -1},
    }

    print(
        f"求解 {total} 个格子（算法=random，{n_workers} 进程并行，"
        f"每次最长 {per_attempt_sec}s，最多 {max_attempts} 次"
        + (f"，partial_accept={partial_accept:.0%}" if partial_accept > 0 else "")
        + "）……"
    )
    overall_start = time.time()

    best_partial: list | None = None
    best_partial_progress: float = 0.0

    if not show_progress:
        tasks = [(map_data, attempt_cfg, i) for i in range(1, max_attempts + 1)]
        with mp.Pool(processes=n_workers) as pool:
            for result, partial, max_progress, elapsed, attempt_num in pool.imap_unordered(_worker_fn, tasks):
                total_elapsed = time.time() - overall_start

                if result is not None:
                    pool.terminate()
                    print(f"  第 {attempt_num} 次尝试成功，单次 {elapsed:.2f}s，"
                          f"总耗时 {total_elapsed:.2f}s — 共 {len(result)} 步")
                    return result

                if partial and max_progress > best_partial_progress:
                    best_partial_progress = max_progress
                    best_partial = partial

                print(f"  第 {attempt_num} 次失败（{elapsed:.2f}s，进度 {max_progress:.1%}）", flush=True)

                if partial_accept > 0 and best_partial_progress >= partial_accept:
                    pool.terminate()
                    print(f"  进度 {best_partial_progress:.1%} ≥ partial_accept={partial_accept:.0%}，"
                          f"采用部分解（{len(best_partial)} 步），总耗时 {total_elapsed:.2f}s")
                    return best_partial
    else:
        with mp.Manager() as manager:
            progress_queue = manager.Queue()
            labels = [_random_attempt_label(i) for i in range(1, max_attempts + 1)]
            tasks = [
                (
                    map_data,
                    {
                        **attempt_cfg,
                        "progress_queue": progress_queue,
                        "progress_label": _random_attempt_label(i),
                        "progress_emit_interval": 0.2,
                    },
                    i,
                )
                for i in range(1, max_attempts + 1)
            ]
            display = _LiveProgressDisplay(
                title=f"random 并行 x{n_workers}",
                labels=labels,
                visible_rows=min(max_attempts, max(n_workers + 2, 6)),
            )

            with mp.Pool(processes=n_workers) as pool:
                iterator = pool.imap_unordered(_worker_fn, tasks)
                remaining = len(tasks)
                while remaining > 0:
                    _drain_progress_queue(progress_queue, display)
                    try:
                        result, partial, max_progress, elapsed, attempt_num = iterator.next(timeout=0.2)
                    except mp.TimeoutError:
                        display.render()
                        continue

                    remaining -= 1
                    total_elapsed = time.time() - overall_start
                    label = _random_attempt_label(attempt_num)

                    if partial and max_progress > best_partial_progress:
                        best_partial_progress = max_progress
                        best_partial = partial

                    display.mark_result(
                        label,
                        success=result is not None,
                        elapsed=elapsed,
                        max_progress=1.0 if result is not None else max_progress,
                        current_progress=1.0 if result is not None else 0.0,
                        steps=len(result) if result is not None else None,
                    )
                    _drain_progress_queue(progress_queue, display)
                    display.render(force=True)

                    if result is not None:
                        pool.terminate()
                        display.close()
                        print(f"  第 {attempt_num} 次尝试成功，单次 {elapsed:.2f}s，"
                              f"总耗时 {total_elapsed:.2f}s — 共 {len(result)} 步")
                        return result

                    if partial_accept > 0 and best_partial_progress >= partial_accept:
                        pool.terminate()
                        display.close()
                        print(f"  进度 {best_partial_progress:.1%} ≥ partial_accept={partial_accept:.0%}，"
                              f"采用部分解（{len(best_partial)} 步），总耗时 {total_elapsed:.2f}s")
                        return best_partial

            display.close()

    raise RuntimeError(
        f"随机重启 {max_attempts} 次后仍未找到解法"
        f"（最高进度 {best_partial_progress:.1%}，总耗时 {time.time() - overall_start:.1f}s）。\n"
        "可在 config.json 中调大 random_attempts / random_attempt_sec，"
        "或设置 partial_accept（如 0.8）以接受部分解。"
    )
