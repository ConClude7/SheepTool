#!/usr/bin/env python3
"""
macOS 窗口探测辅助：
优先使用 Quartz 获取微信窗口信息，避免依赖 System Events 的辅助访问权限。
"""
from __future__ import annotations

import subprocess

try:
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGNullWindowID,
        kCGWindowListOptionOnScreenOnly,
    )
except ImportError:
    CGWindowListCopyWindowInfo = None
    kCGNullWindowID = None
    kCGWindowListOptionOnScreenOnly = None


WECHAT_PROCESS_NAMES = ("WeChat", "微信")


def bring_wechat_to_front():
    """尽量把微信切到前台，不要求辅助访问权限。"""
    for cmd in (
        ["open", "-a", "WeChat"],
        ["osascript", "-e", 'tell application "WeChat" to activate'],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True)
        except FileNotFoundError:
            continue
        if result.returncode == 0:
            return


def get_wechat_window() -> dict | None:
    """
    返回微信前台窗口的位置和大小。
    优先走 Quartz；如果不可用，再退回 System Events。
    """
    return _get_wechat_window_via_quartz() or _get_wechat_window_via_system_events()


def _get_wechat_window_via_quartz() -> dict | None:
    if CGWindowListCopyWindowInfo is None:
        return None

    try:
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
    except Exception:
        return None

    candidates: list[tuple[tuple[int, float, int], dict]] = []
    for window in windows or []:
        owner = window.get("kCGWindowOwnerName")
        bounds = window.get("kCGWindowBounds")
        layer = window.get("kCGWindowLayer", 0)
        if owner not in WECHAT_PROCESS_NAMES or not bounds or layer != 0:
            continue

        width = int(bounds.get("Width", 0))
        height = int(bounds.get("Height", 0))
        if width <= 0 or height <= 0:
            continue

        info = {
            "window_id": int(window.get("kCGWindowNumber", 0)),
            "x": int(bounds.get("X", 0)),
            "y": int(bounds.get("Y", 0)),
            "width": width,
            "height": height,
        }
        candidates.append((_window_sort_key(width, height), info))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _window_sort_key(width: int, height: int) -> tuple[int, float, int]:
    """
    优先选择更像小程序游戏窗口的可见区域：
    - 竖向窗口优先
    - 手机画幅附近优先
    - 同类里再按面积排序
    """
    aspect = height / width
    area = width * height

    portrait = 1 if aspect >= 1.25 else 0
    phone_like = 1 if 320 <= width <= 700 and aspect >= 1.5 else 0
    return (phone_like, portrait, area)


def _get_wechat_window_via_system_events() -> dict | None:
    for process_name in WECHAT_PROCESS_NAMES:
        script = f"""
        tell application "System Events"
            if not (exists process "{process_name}") then return "NOT_RUNNING"
            tell process "{process_name}"
                if (count of windows) = 0 then return "NO_WINDOW"
                set w to front window
                set pos to position of w
                set sz to size of w
                return ((item 1 of pos) as string) & "," & \\
                       ((item 2 of pos) as string) & "," & \\
                       ((item 1 of sz)  as string) & "," & \\
                       ((item 2 of sz)  as string)
            end tell
        end tell
        """
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
        )
        out = result.stdout.strip()
        if result.returncode != 0 or out in ("NOT_RUNNING", "NO_WINDOW"):
            continue

        try:
            x, y, w, h = [int(v.strip()) for v in out.split(",")]
        except (ValueError, IndexError):
            continue
        return {"x": x, "y": y, "width": w, "height": h}

    return None
