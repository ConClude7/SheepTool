#!/usr/bin/env python3
"""
点击执行器：将解法卡牌 ID 转换为屏幕坐标并逐一点击。
支持暂停/继续、单步、结束控制。
"""
import json
import random
import threading
import time
from pathlib import Path

from macos_window import get_wechat_window

PROJECT_DIR = Path(__file__).parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
INSTALL_HINT = (
    f"{VENV_PYTHON} -m pip install -r requirements.txt"
    if VENV_PYTHON.exists()
    else "python3 -m pip install -r requirements.txt"
)

try:
    import pyautogui
    from pynput import keyboard as kb
except ImportError:
    raise ImportError(f"请先安装依赖: {INSTALL_HINT}")

DATA_DIR = PROJECT_DIR / "data"
CALIBRATION_FILE = DATA_DIR / "calibration.json"

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.0   # 自己控制延迟


# ── 校准数据 ──────────────────────────────────────────────────────────────────

def load_calibration() -> dict:
    if not CALIBRATION_FILE.exists():
        raise FileNotFoundError(
            f"未找到校准文件：{CALIBRATION_FILE}\n"
            "请先运行：python main.py calibrate"
        )
    with open(CALIBRATION_FILE) as f:
        return json.load(f)


# ── WeChat 窗口位置 ───────────────────────────────────────────────────────────

def get_wechat_position() -> tuple[int, int]:
    """返回微信窗口当前左上角的屏幕坐标（逻辑像素）。"""
    info = get_wechat_window()
    if not info:
        raise RuntimeError("无法获取微信窗口位置，请确认微信正在运行，并保持窗口可见。")
    return info["x"], info["y"]


# ── 坐标映射 ──────────────────────────────────────────────────────────────────

def _get_map_logical_bounds(map_data: dict) -> tuple[int, int, int, int]:
    """
    返回地图实际使用到的逻辑边界（tile 外接框）：
    left/top 为最小 rolNum/rowNum，
    right/bottom 为最大瓦片右下角（即 max + 8）。

    之前直接按 widthNum/heightNum 的满盘范围映射，会把点位拉到整块 8x10 上；
    但很多关卡真实可用区域会留边，导致系统性偏移。
    """
    level_data = map_data.get("levelData") or {}
    cards = []
    for key in sorted(level_data.keys(), key=int):
        cards.extend(level_data[key])

    if not cards:
        width_units = map_data.get("widthNum", 8) * 8
        height_units = map_data.get("heightNum", 10) * 8
        return 0, 0, width_units, height_units

    min_rol = min(int(card.get("rolNum", 0)) for card in cards)
    min_row = min(int(card.get("rowNum", 0)) for card in cards)
    max_rol = max(int(card.get("rolNum", 0)) for card in cards)
    max_row = max(int(card.get("rowNum", 0)) for card in cards)
    return min_rol, min_row, max_rol + 8, max_row + 8

def card_id_to_coords(card_id: str, calib: dict, map_data: dict,
                      win_x: int, win_y: int) -> tuple[int, int]:
    """
    card_id 格式: "layerNum-rolNum-rowNum"
    rolNum / rowNum 是原始 8x 缩放坐标，瓦片中心为 (+4, +4)。
    """
    parts = card_id.split("-")
    rol_num = int(parts[1])
    row_num = int(parts[2])

    grid = calib["grid_rel"]
    grid_left  = win_x + grid["left"]
    grid_top   = win_y + grid["top"]
    grid_w     = grid["right"]  - grid["left"]
    grid_h     = grid["bottom"] - grid["top"]

    logic_left, logic_top, logic_right, logic_bottom = _get_map_logical_bounds(map_data)
    logic_w = max(logic_right - logic_left, 1)
    logic_h = max(logic_bottom - logic_top, 1)

    sx = int(grid_left + ((rol_num + 4) - logic_left) / logic_w * grid_w)
    sy = int(grid_top  + ((row_num + 4) - logic_top) / logic_h * grid_h)
    return sx, sy


# ── 执行控制器 ────────────────────────────────────────────────────────────────

class ClickController:
    """
    键盘控制（程序运行期间全局监听）：
      p  — 暂停 / 继续
      n  — 执行下一步（暂停状态下）
      s  — 结束
    """

    def __init__(self, delay: float = 0.4, pause_after: int = 0):
        self.delay = delay
        self.pause_after = pause_after

        self._paused    = threading.Event()
        self._paused.set()          # 默认非暂停
        self._stopped   = threading.Event()
        self._step_step = threading.Event()
        self._step_mode = False
        self.click_count = 0

        self._win_pos: tuple[int, int] | None = None
        self._win_refresh_interval = 10  # 每 N 次点击刷新一次窗口位置

        self._listener = kb.Listener(on_press=self._on_key)
        self._listener.daemon = True
        self._listener.start()

    # ── 键盘响应 ──────────────────────────────────────────────────────────────

    def _on_key(self, key):
        try:
            c = key.char
        except AttributeError:
            return
        if c == "p":
            self._toggle_pause()
        elif c == "n":
            self._do_step_once()
        elif c == "s":
            self._do_stop()

    def _toggle_pause(self):
        if self._paused.is_set():
            self._do_pause()
            return

        self._step_mode = False
        self._step_step.set()
        self._paused.set()
        print("[继续]")

    def _do_pause(self):
        if not self._paused.is_set():
            return
        self._paused.clear()
        self._step_mode = False
        print(f"\n[暂停] 已暂停（第 {self.click_count} 步）  p=继续  n=下一步  s=结束")

    def _do_step_once(self):
        if self._paused.is_set():
            self._paused.clear()
        self._step_mode = True
        self._step_step.set()
        print(f"[下一步] 将执行第 {self.click_count + 1} 步")

    def _do_stop(self):
        self._stopped.set()
        self._paused.set()
        self._step_step.set()
        print("\n[退出] 用户终止")

    # ── 等待逻辑 ──────────────────────────────────────────────────────────────

    def _wait(self):
        """在暂停或单步模式下阻塞，直到允许继续。"""
        step_prompt_shown = False
        while True:
            if self._stopped.is_set():
                raise KeyboardInterrupt("用户退出")

            if self._step_mode:
                if not step_prompt_shown:
                    print(f"  [单步 #{self.click_count + 1}] 按 n 执行下一步，p 恢复自动，s 结束...")
                    step_prompt_shown = True
                if self._step_step.is_set():
                    self._step_step.clear()
                    return
            elif self._paused.is_set():
                return

            time.sleep(0.05)

    def _auto_pause_check(self):
        if self.pause_after > 0 and self.click_count > 0 and self.click_count % self.pause_after == 0:
            print(f"\n[自动暂停] 已完成 {self.click_count} 步")
            self._do_pause()

    # ── 窗口位置缓存 ──────────────────────────────────────────────────────────

    def get_win_pos(self) -> tuple[int, int]:
        """缓存并定期刷新微信窗口位置。"""
        if (self._win_pos is None or
                self.click_count % self._win_refresh_interval == 0):
            self._win_pos = get_wechat_position()
        return self._win_pos

    def refresh_win_pos(self):
        """暂停恢复时主动刷新（窗口可能被移动）。"""
        self._win_pos = get_wechat_position()

    # ── 点击 ──────────────────────────────────────────────────────────────────

    def click(self, x: int, y: int, label: str = ""):
        self._wait()
        self._auto_pause_check()

        # 先实际点击，再更新计数和日志，避免“日志先走、点击后到”造成观感错位。
        pyautogui.mouseDown(x, y)
        time.sleep(0.01)
        pyautogui.mouseUp(x, y)

        self.click_count += 1
        print(f"  [{self.click_count:3d}] ({x:4d}, {y:4d})  {label}")
        jitter = random.uniform(-0.1, 0.1)
        time.sleep(max(0.0, self.delay + jitter))

    def stop(self):
        self._stopped.set()
        self._paused.set()
        self._step_step.set()
        if self._listener.is_alive():
            self._listener.stop()

    def enable_step_mode(self):
        self._step_mode = True
        self._paused.clear()
        print("[单步模式] 按 n 执行每一步，p 切换回自动")


# ── 公开执行入口 ──────────────────────────────────────────────────────────────

def execute_solution(
    card_ids: list,
    map_data: dict,
    calib: dict,
    delay: float = 0.4,
    pause_after: int = 0,
    step_mode: bool = False,
):
    """执行完整解法点击序列。"""
    ctrl = ClickController(delay=delay, pause_after=pause_after)

    if step_mode:
        ctrl.enable_step_mode()

    total = len(card_ids)
    print(f"\n=== 开始执行  共 {total} 步 ===")
    print("控制键：  p=暂停/继续  n=下一步  s=结束\n")

    try:
        for card_id in card_ids:
            win_x, win_y = ctrl.get_win_pos()
            x, y = card_id_to_coords(card_id, calib, map_data, win_x, win_y)
            ctrl.click(x, y, label=card_id)

        print(f"\n✓ 全部 {ctrl.click_count} 步完成！")
    except KeyboardInterrupt:
        print(f"\n已停止（完成 {ctrl.click_count}/{total} 步）")
    finally:
        ctrl.stop()
