#!/usr/bin/env python3
"""
校准工具：截图微信窗口，让用户框选牌局区域，保存偏移量。
运行方式：python main.py calibrate
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from macos_window import bring_wechat_to_front, get_wechat_window

PROJECT_DIR = Path(__file__).parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "bin" / "python"
INSTALL_HINT = (
    f"{VENV_PYTHON} -m pip install -r requirements.txt"
    if VENV_PYTHON.exists()
    else "python3 -m pip install -r requirements.txt"
)

try:
    import tkinter as tk
    from tkinter import messagebox
except ModuleNotFoundError:
    tk = None
    messagebox = None

try:
    import pyautogui
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print(f"缺少依赖，请先运行: {INSTALL_HINT}")
    sys.exit(1)

if tk is not None:
    try:
        from PIL import ImageTk
    except ImportError:
        print(f"缺少依赖，请先运行: {INSTALL_HINT}")
        sys.exit(1)
else:
    ImageTk = None

DATA_DIR = PROJECT_DIR / "data"
CALIBRATION_FILE = DATA_DIR / "calibration.json"
ALIGNMENT_PREVIEW_FILE = DATA_DIR / "alignment_preview.png"
SOLUTION_PREVIEW_FILE = DATA_DIR / "solution_preview.png"
DEFAULT_WIDTH_NUM = 8
DEFAULT_HEIGHT_NUM = 10


def show_notification(title: str, message: str):
    """在 macOS 上显示通知，方便无 GUI 模式下提示下一步。"""
    safe_title = title.replace('"', '\\"')
    safe_message = message.replace('"', '\\"')
    script = (
        f'display notification "{safe_message}" '
        f'with title "{safe_title}"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)


def save_calibration(window_info: dict, left: int, top: int, right: int, bottom: int):
    calibration = {
        "window": window_info,
        "grid_rel": {"left": left, "top": top, "right": right, "bottom": bottom},
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(calibration, f, indent=2, ensure_ascii=False)

    print(f"校准已保存: {CALIBRATION_FILE}")
    print(f"  grid_rel: left={left} top={top} right={right} bottom={bottom}")


def load_calibration() -> dict:
    if not CALIBRATION_FILE.exists():
        raise FileNotFoundError(
            f"未找到校准文件：{CALIBRATION_FILE}\n"
            "请先运行：python main.py calibrate"
        )
    with open(CALIBRATION_FILE) as f:
        return json.load(f)


def _iter_map_cards(map_data: dict) -> list[dict]:
    level_data = map_data.get("levelData") or {}
    cards: list[dict] = []
    for key in sorted(level_data.keys(), key=int):
        cards.extend(level_data[key])
    return cards


def _get_map_logical_bounds(map_data: dict) -> tuple[int, int, int, int]:
    cards = _iter_map_cards(map_data)
    if not cards:
        return 0, 0, DEFAULT_WIDTH_NUM * 8, DEFAULT_HEIGHT_NUM * 8

    min_rol = min(int(card.get("rolNum", 0)) for card in cards)
    min_row = min(int(card.get("rowNum", 0)) for card in cards)
    max_rol = max(int(card.get("rolNum", 0)) for card in cards)
    max_row = max(int(card.get("rowNum", 0)) for card in cards)
    return min_rol, min_row, max_rol + 8, max_row + 8


def _load_latest_preview_map_data() -> dict | None:
    """
    优先读取最近一次求解对应的地图，用它来画真实点位。
    这样预览图会和实际点击使用同一套逻辑边界。
    """
    parsed_dir = DATA_DIR / "parsed"
    solution_files = sorted(parsed_dir.glob("solution_level*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for solution_path in solution_files:
        try:
            solution_data = json.loads(solution_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        md5 = solution_data.get("md5")
        if not md5:
            continue

        map_path = parsed_dir / f"{md5}_parsed.json"
        if not map_path.exists():
            continue

        try:
            return json.loads(map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

    return None


def _load_latest_solution_and_map() -> tuple[dict | None, dict | None]:
    """
    返回最近一次求解的 (solution_data, map_data)。
    """
    parsed_dir = DATA_DIR / "parsed"
    solution_files = sorted(
        parsed_dir.glob("solution_level*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for solution_path in solution_files:
        try:
            solution_data = json.loads(solution_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        md5 = solution_data.get("md5")
        if not md5:
            continue

        map_path = parsed_dir / f"{md5}_parsed.json"
        if not map_path.exists():
            continue

        try:
            map_data = json.loads(map_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        return solution_data, map_data

    return None, None


def _card_id_to_preview_coords(
    card_id: str,
    grid_rel: dict,
    map_data: dict,
) -> tuple[int, int]:
    parts = card_id.split("-")
    rol_num = int(parts[1])
    row_num = int(parts[2])

    left = int(grid_rel["left"])
    top = int(grid_rel["top"])
    right = int(grid_rel["right"])
    bottom = int(grid_rel["bottom"])
    grid_w = max(right - left, 1)
    grid_h = max(bottom - top, 1)

    logic_left, logic_top, logic_right, logic_bottom = _get_map_logical_bounds(map_data)
    logic_w = max(logic_right - logic_left, 1)
    logic_h = max(logic_bottom - logic_top, 1)

    px = int(left + ((rol_num + 4) - logic_left) / logic_w * grid_w)
    py = int(top + ((row_num + 4) - logic_top) / logic_h * grid_h)
    return px, py


def _clamp_point(value: int, limit: int) -> int:
    if limit <= 0:
        return 0
    return max(0, min(value, limit - 1))


def export_alignment_preview(
    window_info: dict,
    grid_rel: dict,
    output_path: Path | None = None,
    map_data: dict | None = None,
    solution_steps: list[str] | None = None,
    highlight_steps: list[str] | None = None,
    screenshot: Image.Image | None = None,
    width_num: int = DEFAULT_WIDTH_NUM,
    height_num: int = DEFAULT_HEIGHT_NUM,
) -> Path:
    """
    生成一张带对齐框的窗口截图，方便人工确认牌区映射是否准确。
    """
    output_path = output_path or ALIGNMENT_PREVIEW_FILE
    screenshot = (screenshot or capture_window_image(window_info)).convert("RGBA")

    left = int(grid_rel["left"])
    top = int(grid_rel["top"])
    right = int(grid_rel["right"])
    bottom = int(grid_rel["bottom"])
    width, height = screenshot.size

    overlay = Image.new("RGBA", screenshot.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rectangle((0, 0, width, height), fill=(0, 0, 0, 82))
    overlay_draw.rectangle((left, top, right, bottom), fill=(0, 0, 0, 0))
    screenshot = Image.alpha_composite(screenshot, overlay)

    draw = ImageDraw.Draw(screenshot)
    label_bg = (20, 20, 20, 210)
    label_fg = (255, 255, 255, 255)
    rect_fg = (255, 230, 0, 255)
    cross_fg = (0, 255, 136, 255)
    corner_fg = (255, 92, 92, 255)
    guide_fg = (255, 255, 255, 72)
    point_fg = (64, 220, 255, 255)
    click_fg = (64, 220, 255, 255)
    click_outline_fg = (255, 255, 255, 255)
    highlight_fg = (220, 38, 38, 255)
    highlight_fill_fg = (220, 38, 38, 78)
    click_text_fg = (255, 255, 255, 255)
    font = ImageFont.load_default()

    draw.rectangle((left, top, right, bottom), outline=rect_fg, width=4)

    grid_w = max(right - left, 1)
    grid_h = max(bottom - top, 1)
    if map_data is not None:
        cards = _iter_map_cards(map_data)
        logic_left, logic_top, logic_right, logic_bottom = _get_map_logical_bounds(map_data)
        unique_rols = sorted({int(card.get("rolNum", 0)) for card in cards})
        unique_rows = sorted({int(card.get("rowNum", 0)) for card in cards})
        point_pairs = sorted({(int(card.get("rolNum", 0)), int(card.get("rowNum", 0))) for card in cards})
        preview_mode = "predicted-map-points"
    else:
        logic_left = 0
        logic_top = 0
        logic_right = width_num * 8
        logic_bottom = height_num * 8
        unique_rols = []
        unique_rows = []
        point_pairs = []
        preview_mode = "calibration-box"

    logic_w = max(logic_right - logic_left, 1)
    logic_h = max(logic_bottom - logic_top, 1)

    def map_x(rol_num: int) -> int:
        return int(left + ((rol_num + 4) - logic_left) / logic_w * grid_w)

    def map_y(row_num: int) -> int:
        return int(top + ((row_num + 4) - logic_top) / logic_h * grid_h)

    has_solution_points = bool(map_data is not None and solution_steps)

    if point_pairs and not has_solution_points:
        for rol_num in unique_rols:
            x = map_x(rol_num)
            draw.line((x, top, x, bottom), fill=guide_fg, width=1)
        for row_num in unique_rows:
            y = map_y(row_num)
            draw.line((left, y, right, y), fill=guide_fg, width=1)

        for rol_num, row_num in point_pairs:
            px = map_x(rol_num)
            py = map_y(row_num)
            draw.ellipse((px - 2, py - 2, px + 2, py + 2), fill=point_fg)

    click_points: list[tuple[int, int]] = []
    if map_data is not None and solution_steps:
        preview_mode = "predicted-click-points"
        for card_id in solution_steps:
            try:
                click_points.append(_card_id_to_preview_coords(card_id, grid_rel, map_data))
            except Exception:
                continue

    if click_points:
        for px, py in sorted(set(click_points)):
            draw.ellipse(
                (px - 3, py - 3, px + 3, py + 3),
                fill=click_fg,
                outline=click_outline_fg,
            )

    highlight_points: list[tuple[int, int]] = []
    if map_data is not None and highlight_steps:
        for card_id in highlight_steps:
            try:
                highlight_points.append(_card_id_to_preview_coords(card_id, grid_rel, map_data))
            except Exception:
                continue

    if highlight_points:
        preview_mode = "solution-first-steps"
        for idx, (px, py) in enumerate(highlight_points, start=1):
            draw.ellipse(
                (px - 8, py - 8, px + 8, py + 8),
                fill=highlight_fill_fg,
                outline=highlight_fg,
                width=3,
            )
            label = str(idx)
            label_x = _clamp_point(px + 9, width)
            label_y = _clamp_point(py - 11, height)
            bbox = draw.textbbox((label_x, label_y), label, font=font)
            draw.rounded_rectangle(
                (bbox[0] - 3, bbox[1] - 2, bbox[2] + 3, bbox[3] + 2),
                radius=3,
                fill=(18, 18, 18, 218),
            )
            draw.text(
                (label_x, label_y),
                label,
                fill=click_text_fg,
                font=font,
            )

    cx = (left + right) // 2
    cy = (top + bottom) // 2
    cross_half = 12
    draw.line((cx - cross_half, cy, cx + cross_half, cy), fill=cross_fg, width=3)
    draw.line((cx, cy - cross_half, cx, cy + cross_half), fill=cross_fg, width=3)

    for px, py in ((left, top), (right, bottom)):
        draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=corner_fg)

    label_text = (
        f"WeChat window: {window_info['width']}x{window_info['height']}\n"
        f"grid_rel: left={left} top={top} right={right} bottom={bottom}\n"
        f"grid_size: {max(right - left, 0)}x{max(bottom - top, 0)}\n"
        f"preview_mode: {preview_mode}\n"
        + (
            f"logical_bounds: {logic_left},{logic_top} -> {logic_right},{logic_bottom}\n"
            f"point_count: {len(point_pairs)}\n"
            f"predicted_click_points: {len(set(click_points)) if click_points else len(point_pairs)}\n"
            f"highlight_steps: {len(highlight_points)}"
            if point_pairs else
            "point_overlay: disabled (need solved map data)"
        )
    )
    bbox = draw.multiline_textbbox((0, 0), label_text, font=font, spacing=4)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = 14
    text_y = 14
    draw.rounded_rectangle(
        (text_x - 8, text_y - 8, text_x + text_w + 8, text_y + text_h + 8),
        radius=10,
        fill=label_bg,
    )
    draw.multiline_text(
        (text_x, text_y),
        label_text,
        fill=label_fg,
        font=font,
        spacing=4,
    )

    corner_labels = [
        ("TL", left + 8, top + 8),
        ("BR", right - 26, bottom - 18),
    ]
    for text, px, py in corner_labels:
        draw.text(
            (_clamp_point(px, width), _clamp_point(py, height)),
            text,
            fill=label_fg,
            font=font,
        )

    if point_pairs and not has_solution_points:
        for i, rol_num in enumerate(unique_rols):
            text = str(rol_num)
            px = map_x(rol_num) - (4 if rol_num < 10 else 7)
            py = top - 16
            draw.text(
                (_clamp_point(px, width), _clamp_point(py, height)),
                text,
                fill=point_fg,
                font=font,
            )
            if i >= 14:
                break
        for i, row_num in enumerate(unique_rows):
            text = str(row_num)
            px = left - 14
            py = map_y(row_num) - 4
            draw.text(
                (_clamp_point(px, width), _clamp_point(py, height)),
                text,
                fill=point_fg,
                font=font,
            )
            if i >= 14:
                break

    output_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot.convert("RGB").save(output_path)
    return output_path


def export_latest_click_preview(
    window_info: dict,
    grid_rel: dict,
    output_path: Path | None = None,
    screenshot: Image.Image | None = None,
) -> Path:
    solution_data, map_data = _load_latest_solution_and_map()
    solution_steps = solution_data.get("solution") if solution_data else None
    return export_alignment_preview(
        window_info,
        grid_rel,
        output_path,
        map_data=map_data,
        solution_steps=solution_steps,
        screenshot=screenshot,
    )


def export_solution_preview(
    window_info: dict,
    grid_rel: dict,
    map_data: dict,
    solution_steps: list[str],
    output_path: Path | None = None,
    screenshot: Image.Image | None = None,
    highlight_count: int = 10,
) -> Path:
    return export_alignment_preview(
        window_info,
        grid_rel,
        output_path or SOLUTION_PREVIEW_FILE,
        map_data=map_data,
        solution_steps=solution_steps,
        highlight_steps=solution_steps[:highlight_count],
        screenshot=screenshot,
    )


def export_solution_preview_from_current_window(
    grid_rel: dict,
    map_data: dict,
    solution_steps: list[str],
    output_path: Path | None = None,
    highlight_count: int = 10,
) -> Path:
    window_info = get_wechat_window()
    if not window_info:
        raise RuntimeError("未找到微信窗口，请先打开微信并保持游戏窗口可见。")
    return export_solution_preview(
        window_info,
        grid_rel,
        map_data,
        solution_steps,
        output_path=output_path,
        highlight_count=highlight_count,
    )


def export_alignment_preview_from_current_window(output_path: Path | None = None) -> Path:
    """
    按当前微信窗口位置重新截图，并套用已保存的 grid_rel 导出预览图。
    """
    calibration = load_calibration()
    window_info = get_wechat_window()
    if not window_info:
        raise RuntimeError("未找到微信窗口，请先打开微信并保持游戏窗口可见。")
    return export_latest_click_preview(window_info, calibration["grid_rel"], output_path)


def validate_region(left: int, top: int, right: int, bottom: int):
    if right - left < 20 or bottom - top < 20:
        raise ValueError("框选区域太小，请重试。")


def validate_point_in_window(point: tuple[int, int], window_info: dict, label: str):
    px, py = point
    wx = window_info["x"]
    wy = window_info["y"]
    ww = window_info["width"]
    wh = window_info["height"]
    if not (wx <= px <= wx + ww and wy <= py <= wy + wh):
        raise ValueError(
            f"{label} 不在微信窗口内：x={px} y={py}，"
            f"窗口范围是 x={wx}..{wx + ww} y={wy}..{wy + wh}"
        )


def _color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2])


def _find_intervals(values: list[int], threshold: int) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    start: int | None = None
    for i, value in enumerate(values):
        if value >= threshold and start is None:
            start = i
        elif value < threshold and start is not None:
            intervals.append((start, i - 1))
            start = None
    if start is not None:
        intervals.append((start, len(values) - 1))
    return intervals


def _merge_intervals(intervals: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    if not intervals:
        return []

    merged: list[list[int]] = [[intervals[0][0], intervals[0][1]]]
    for start, end in intervals[1:]:
        if start - merged[-1][1] - 1 <= gap:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _count_non_background_in_row(
    img: Image.Image,
    y: int,
    x1: int,
    x2: int,
    background: tuple[int, int, int],
    color_threshold: int,
) -> int:
    count = 0
    for x in range(x1, x2 + 1):
        if _color_distance(img.getpixel((x, y)), background) > color_threshold:
            count += 1
    return count


def _expand_vertical_edge(
    img: Image.Image,
    start_y: int,
    step: int,
    x1: int,
    x2: int,
    background: tuple[int, int, int],
    color_threshold: int,
    edge_threshold: int,
    limit_y: int,
    max_gap: int = 3,
) -> int:
    y = start_y
    edge = start_y - step
    misses = 0
    while 0 <= y < img.size[1]:
        if step < 0 and y < limit_y:
            break
        if step > 0 and y > limit_y:
            break

        count = _count_non_background_in_row(
            img, y, x1, x2, background, color_threshold
        )
        if count >= edge_threshold:
            edge = y
            misses = 0
        else:
            misses += 1
            if misses > max_gap:
                break
        y += step
    return edge


def detect_grid_region(screenshot: Image.Image) -> tuple[int, int, int, int] | None:
    """
    从游戏截图中自动识别牌区边界。
    思路：
    1. 先估计草地背景色；
    2. 按行统计“明显偏离背景色”的像素数量，找到牌区所在的纵向带；
    3. 再在该纵向带内按列统计，得到横向范围。
    """
    img = screenshot.convert("RGB")
    width, height = img.size

    samples: list[tuple[int, int, int]] = []
    for y in range(int(height * 0.12), int(height * 0.72), max(12, height // 30)):
        for x in range(int(width * 0.08), int(width * 0.92), max(12, width // 24)):
            samples.append(img.getpixel((x, y)))
    background = max(set(samples), key=samples.count)

    row_start = int(height * 0.08)
    row_end = int(height * 0.82)
    col_start = int(width * 0.08)
    col_end = int(width * 0.92)
    color_threshold = 55

    row_counts: list[int] = []
    for y in range(row_start, row_end):
        count = 0
        for x in range(col_start, col_end):
            if _color_distance(img.getpixel((x, y)), background) > color_threshold:
                count += 1
        row_counts.append(count)

    row_threshold = max(80, int(width * 0.18))
    row_intervals = _find_intervals(row_counts, row_threshold)

    board_rows: list[tuple[int, int]] = []
    top_exclude_y = int(height * 0.22)
    bottom_exclude_y = int(height * 0.74)
    for start, end in row_intervals:
        y1 = start + row_start
        y2 = end + row_start
        # 只排除完全落在顶部标题区里的段，不误伤真正从更高位置开始的牌区
        if y2 < top_exclude_y:
            continue
        if y1 > bottom_exclude_y:
            continue
        board_rows.append((y1, y2))

    merged_rows = _merge_intervals(board_rows, int(height * 0.06))
    if not merged_rows:
        return None

    top, bottom = max(merged_rows, key=lambda interval: interval[1] - interval[0])

    x_scan_start = int(width * 0.05)
    x_scan_end = int(width * 0.95)
    col_counts: list[int] = []
    for x in range(x_scan_start, x_scan_end):
        count = 0
        for y in range(top, bottom + 1):
            if _color_distance(img.getpixel((x, y)), background) > color_threshold:
                count += 1
        col_counts.append(count)

    col_threshold = max(25, int((bottom - top + 1) * 0.18))
    col_intervals = _find_intervals(col_counts, col_threshold)

    board_cols: list[tuple[int, int]] = []
    for start, end in col_intervals:
        x1 = start + x_scan_start
        x2 = end + x_scan_start
        if x1 < int(width * 0.08):
            continue
        if x2 > int(width * 0.92):
            continue
        board_cols.append((x1, x2))

    merged_cols = _merge_intervals(board_cols, int(width * 0.04))
    if not merged_cols:
        return None

    left, right = max(merged_cols, key=lambda interval: interval[1] - interval[0])

    edge_threshold = max(80, int((right - left + 1) * 0.22))
    top = _expand_vertical_edge(
        img,
        start_y=top - 1,
        step=-1,
        x1=left,
        x2=right,
        background=background,
        color_threshold=color_threshold,
        edge_threshold=edge_threshold,
        limit_y=int(height * 0.16),
    )
    bottom = _expand_vertical_edge(
        img,
        start_y=bottom + 1,
        step=1,
        x1=left,
        x2=right,
        background=background,
        color_threshold=color_threshold,
        edge_threshold=edge_threshold,
        limit_y=int(height * 0.74),
    )

    try:
        validate_region(left, top, right, bottom)
    except ValueError:
        return None
    return left, top, right, bottom


def try_auto_calibration(window_info: dict, screenshot: Image.Image, map_data: dict | None = None) -> bool:
    region = detect_grid_region(screenshot)
    if not region:
        return False

    left, top, right, bottom = region
    save_calibration(window_info, left, top, right, bottom)
    grid_rel = {"left": left, "top": top, "right": right, "bottom": bottom}
    if map_data is None:
        preview_path = export_latest_click_preview(window_info, grid_rel, screenshot=screenshot)
    else:
        preview_path = export_alignment_preview(
            window_info,
            grid_rel,
            map_data=map_data,
            screenshot=screenshot,
        )
    width = right - left
    height = bottom - top
    print("✓ 已自动校准！")
    print(
        f"网格区域（相对窗口）：左={left} 上={top} 右={right} 下={bottom} "
        f"尺寸={width}x{height}px"
    )
    print(f"预览图已保存: {preview_path}")
    return True


def capture_window_image(window_info: dict) -> Image.Image:
    """
    优先按窗口 id 截图，避免多屏环境下按区域截图失效。
    如果没有 window_id，再退回 pyautogui 的 region 截图。
    """
    window_id = window_info.get("window_id")
    if window_id:
        fd, tmp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            result = subprocess.run(
                ["screencapture", "-l", str(window_id), "-x", tmp_path],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and os.path.exists(tmp_path):
                image = Image.open(tmp_path).convert("RGB")
                image.load()
                if image.size[0] > window_info["width"] and image.size[1] > window_info["height"]:
                    dx = max((image.size[0] - window_info["width"]) // 2, 0)
                    dy = max((image.size[1] - window_info["height"]) // 2, 0)
                    image = image.crop((
                        dx,
                        dy,
                        dx + window_info["width"],
                        dy + window_info["height"],
                    ))
                return image
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    # 某些情况下按 window_id 截图会失败，退回到整屏截图后按窗口坐标裁切。
    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        result = subprocess.run(
            ["screencapture", "-x", tmp_path],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
            image = Image.open(tmp_path).convert("RGB")
            image.load()
            x = max(int(window_info["x"]), 0)
            y = max(int(window_info["y"]), 0)
            right = min(x + int(window_info["width"]), image.size[0])
            bottom = min(y + int(window_info["height"]), image.size[1])
            if right > x and bottom > y:
                return image.crop((x, y, right, bottom))
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return pyautogui.screenshot(region=(
        window_info["x"],
        window_info["y"],
        window_info["width"],
        window_info["height"],
    ))


def countdown_capture_point(label: str, seconds: int = 5) -> tuple[int, int]:
    show_notification("SheepTool 校准", f"{seconds} 秒后记录{label}，请把鼠标移到对应位置")
    print(f"{label}：{seconds} 秒后自动记录当前鼠标位置...")
    for remain in range(seconds, 0, -1):
        print(f"  {remain}...", end="\r", flush=True)
        time.sleep(1)
    x, y = pyautogui.position()
    print(f"  已记录 {label}: x={x} y={y}      ")
    return x, y


def run_headless_calibration(window_info: dict, map_data: dict | None = None):
    print("检测到当前 Python 不支持 Tk，切换到无界面校准模式。")
    print("接下来会自动记录两次鼠标位置，请保持微信窗口不要移动。")
    print("先把鼠标移到牌局区域左上角，记录完成后再移到右下角。")

    bring_wechat_to_front()
    time.sleep(0.6)
    screenshot = capture_window_image(window_info)

    top_left_abs = countdown_capture_point("左上角")
    show_notification("SheepTool 校准", "左上角已记录，请移动到牌局区域右下角")
    time.sleep(1.0)
    bottom_right_abs = countdown_capture_point("右下角")

    validate_point_in_window(top_left_abs, window_info, "左上角")
    validate_point_in_window(bottom_right_abs, window_info, "右下角")

    left = min(top_left_abs[0], bottom_right_abs[0]) - window_info["x"]
    top = min(top_left_abs[1], bottom_right_abs[1]) - window_info["y"]
    right = max(top_left_abs[0], bottom_right_abs[0]) - window_info["x"]
    bottom = max(top_left_abs[1], bottom_right_abs[1]) - window_info["y"]

    validate_region(left, top, right, bottom)
    save_calibration(window_info, left, top, right, bottom)
    grid_rel = {"left": left, "top": top, "right": right, "bottom": bottom}
    if map_data is None:
        preview_path = export_latest_click_preview(window_info, grid_rel, screenshot=screenshot)
    else:
        preview_path = export_alignment_preview(
            window_info,
            grid_rel,
            map_data=map_data,
            screenshot=screenshot,
        )

    width = right - left
    height = bottom - top
    print("✓ 校准完成！")
    print(
        f"网格区域（相对窗口）：左={left} 上={top} 右={right} 下={bottom} "
        f"尺寸={width}x{height}px"
    )
    print(f"预览图已保存: {preview_path}")
    show_notification("SheepTool 校准", "校准完成，结果已经保存")


# ── 校准 GUI ─────────────────────────────────────────────────────────────────

class CalibrationApp:
    CROSSHAIR_R = 7
    RECT_COLOR = "#ffff00"
    DOT_COLORS = ["#00ff88", "#ff4466"]

    def __init__(self, window_info: dict, screenshot: Image.Image, map_data: dict | None = None):
        self.window_info = window_info
        self.original = screenshot
        self.map_data = map_data
        self.clicks: list[tuple[int, int]] = []
        self.scale = 1.0

        self.root = tk.Tk()
        self.root.title("SheepTool 校准")
        self.root.configure(bg="#1e1e1e")
        self._build_ui()
        self._render_image()

    def _build_ui(self):
        # 顶部提示条
        self.header = tk.Label(
            self.root,
            text="第 1 步：点击牌局区域的【左上角】",
            font=("Helvetica", 15, "bold"),
            fg="#ffffff", bg="#2d2d2d",
            pady=8,
        )
        self.header.pack(fill=tk.X)

        self.subheader = tk.Label(
            self.root,
            text="将鼠标移到牌面网格的最左上角，然后点击",
            font=("Helvetica", 11),
            fg="#aaaaaa", bg="#2d2d2d",
            pady=4,
        )
        self.subheader.pack(fill=tk.X)

        # 画布
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        iw, ih = self.original.size
        self.scale = min((sw - 80) / iw, (sh - 160) / ih, 1.0)
        cw, ch = int(iw * self.scale), int(ih * self.scale)

        self.canvas = tk.Canvas(
            self.root, width=cw, height=ch,
            cursor="crosshair", bg="#000000",
            highlightthickness=0,
        )
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)

        # 底部按钮
        bar = tk.Frame(self.root, bg="#1e1e1e")
        bar.pack(fill=tk.X, pady=6)
        tk.Button(bar, text="重新校准", command=self._reset,
                  bg="#444", fg="white", relief=tk.FLAT,
                  padx=12, pady=4).pack(side=tk.LEFT, padx=8)
        tk.Button(bar, text="退出", command=self.root.destroy,
                  bg="#444", fg="white", relief=tk.FLAT,
                  padx=12, pady=4).pack(side=tk.RIGHT, padx=8)

        self.coord_label = tk.Label(
            bar, text="", font=("Courier", 10),
            fg="#888888", bg="#1e1e1e",
        )
        self.coord_label.pack(side=tk.LEFT, padx=12)

    def _render_image(self):
        iw, ih = self.original.size
        dw, dh = int(iw * self.scale), int(ih * self.scale)
        display = self.original.resize((dw, dh), Image.LANCZOS)
        self._tk_img = ImageTk.PhotoImage(display)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._tk_img)
        self._redraw_markers()

    def _redraw_markers(self):
        for i, (rx, ry) in enumerate(self.clicks):
            cx = int(rx * self.scale)
            cy = int(ry * self.scale)
            r = self.CROSSHAIR_R
            color = self.DOT_COLORS[i]
            self.canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                    fill=color, outline="white", width=2)
            label = "TL" if i == 0 else "BR"
            self.canvas.create_text(cx + r + 6, cy, text=label,
                                    fill=color, font=("Helvetica", 9, "bold"),
                                    anchor=tk.W)
        if len(self.clicks) == 2:
            p1, p2 = self.clicks
            x1, y1 = int(p1[0] * self.scale), int(p1[1] * self.scale)
            x2, y2 = int(p2[0] * self.scale), int(p2[1] * self.scale)
            self.canvas.create_rectangle(x1, y1, x2, y2,
                                         outline=self.RECT_COLOR, width=2,
                                         dash=(6, 3))

    def _on_motion(self, event):
        rx = int(event.x / self.scale)
        ry = int(event.y / self.scale)
        self.coord_label.config(text=f"窗口内坐标  x={rx}  y={ry}")

    def _on_click(self, event):
        if len(self.clicks) >= 2:
            return
        rx, ry = int(event.x / self.scale), int(event.y / self.scale)
        self.clicks.append((rx, ry))

        if len(self.clicks) == 1:
            self.header.config(text="第 2 步：点击牌局区域的【右下角】")
            self.subheader.config(text="将鼠标移到牌面网格的最右下角，然后点击")
        else:
            self._finish()

        self._redraw_markers()

    def _finish(self):
        p1, p2 = self.clicks
        left   = min(p1[0], p2[0])
        top    = min(p1[1], p2[1])
        right  = max(p1[0], p2[0])
        bottom = max(p1[1], p2[1])

        try:
            validate_region(left, top, right, bottom)
        except ValueError:
            messagebox.showwarning("区域太小", "框选区域太小，请重试。")
            self._reset()
            return

        save_calibration(self.window_info, left, top, right, bottom)
        grid_rel = {"left": left, "top": top, "right": right, "bottom": bottom}
        if self.map_data is None:
            preview_path = export_latest_click_preview(
                self.window_info,
                grid_rel,
                screenshot=self.original,
            )
        else:
            preview_path = export_alignment_preview(
                self.window_info,
                grid_rel,
                map_data=self.map_data,
                screenshot=self.original,
            )
        width = right - left
        height = bottom - top
        self.header.config(text="✓ 校准完成！")
        self.subheader.config(
            text=f"网格区域：左={left} 上={top} 右={right} 下={bottom}  "
                 f"（{width}×{height} px）"
        )
        messagebox.showinfo(
            "校准完成",
            f"已保存校准数据！\n\n"
            f"网格区域（相对窗口）：\n"
            f"  左={left}  上={top}  右={right}  下={bottom}\n"
            f"  尺寸: {width} × {height} px\n"
            f"  预览图: {preview_path}",
        )

    def _reset(self):
        self.clicks.clear()
        self.header.config(text="第 1 步：点击牌局区域的【左上角】")
        self.subheader.config(text="将鼠标移到牌面网格的最左上角，然后点击")
        self._render_image()

    def run(self):
        self.root.mainloop()


# ── 入口 ─────────────────────────────────────────────────────────────────────

def run_calibration(map_data: dict | None = None):
    print("正在查找微信窗口...")
    bring_wechat_to_front()
    time.sleep(0.4)  # 等待窗口切到前台

    window_info = get_wechat_window()
    if not window_info:
        print("错误：未找到微信窗口。请先打开微信并进入游戏关卡，然后重试。")
        sys.exit(1)

    print(f"微信窗口：x={window_info['x']} y={window_info['y']} "
          f"w={window_info['width']} h={window_info['height']}")
    print("正在截图...")
    screenshot = capture_window_image(window_info)

    print("正在尝试自动识别牌区...")
    if try_auto_calibration(window_info, screenshot, map_data=map_data):
        return

    print("自动识别失败，切换到手动校准。")
    if tk is None or ImageTk is None:
        try:
            run_headless_calibration(window_info, map_data=map_data)
        except ValueError as e:
            print(f"错误：{e}")
            sys.exit(1)
        return

    print("请在弹出的窗口中框选牌局区域。")
    app = CalibrationApp(window_info, screenshot, map_data=map_data)
    app.run()


if __name__ == "__main__":
    run_calibration()
