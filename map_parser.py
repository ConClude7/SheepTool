#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

TOOLS_DIR = Path(__file__).parent / "tools"
MAP_TO_JSON_JS = TOOLS_DIR / "map-to-json.js"
DATA_DIR = Path(__file__).parent / "data"


def parse_map_file(map_path: str) -> dict:
    map_path = Path(map_path).resolve()
    if not map_path.exists():
        raise FileNotFoundError(f"Map file not found: {map_path}")

    parsed_dir = DATA_DIR / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    output_path = parsed_dir / (map_path.stem + "_parsed.json")

    result = subprocess.run(
        ["node", str(MAP_TO_JSON_JS), str(map_path), str(output_path)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Map parsing failed:\n{result.stderr.strip()}")

    with open(output_path) as f:
        data = json.load(f)

    layer_count = len(data.get("layers", []))
    total_tiles = sum(len(v) for v in data.get("levelData", {}).values())
    print(f"Parsed: levelKey={data.get('levelKey')}  layers={layer_count}  tiles={total_tiles}")
    print(f"  Grid: {data.get('widthNum', '?')}w × {data.get('heightNum', '?')}h")

    return data
