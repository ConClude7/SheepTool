#!/usr/bin/env python3
"""Solve a saved map_info JSON without calibration or clicking."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main import load_config
from map_fetcher import fetch_and_parse
from solver import solve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Solve a saved Sheep map_info JSON")
    parser.add_argument("file", help="JSON file containing map_info data")
    parser.add_argument("--level", type=int, choices=[1, 2], default=2)
    parser.add_argument("--algorithm", default="normal")
    parser.add_argument("--output", help="Output solution JSON path")
    parser.add_argument("--random-attempts", type=int, default=8)
    parser.add_argument("--random-attempt-sec", type=int, default=10)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    api = data.get("data", data)

    seed = (
        api.get("map_seed_real")
        or api.get("real_map_seed")
        or api.get("seed_map")
        or api.get("map_seed")
    )
    if not isinstance(seed, list) or len(seed) != 4 or not any(int(x) for x in seed):
        raise SystemExit("missing real 4-int map seed")

    cfg = load_config()["solver"]
    cfg = {
        **cfg,
        "show_progress": False,
        "random_attempts": args.random_attempts,
        "random_attempt_sec": args.random_attempt_sec,
    }

    level_idx = args.level - 1
    map_data = fetch_and_parse(
        api["map_md5"],
        [int(x) for x in seed],
        api.get("map_seed_2"),
        index=level_idx,
    )
    solution = solve(map_data, cfg, args.algorithm)

    out = Path(args.output or f"data/parsed/solution_level{args.level}_{Path(args.file).stem}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "level": args.level,
                "md5": api["map_md5"][level_idx],
                "map_seed": [int(x) for x in seed],
                "algorithm": args.algorithm,
                "steps": len(solution),
                "solution": solution,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"solution steps={len(solution)} saved={out}")


if __name__ == "__main__":
    main()
