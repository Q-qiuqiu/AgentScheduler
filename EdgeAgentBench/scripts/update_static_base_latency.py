#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BENCH_ROOT.parent
DEFAULT_PROFILE_PATH = REPO_ROOT / "config_files" / "latency_profiles.json"
DEFAULT_STATIC_INFO_PATH = REPO_ROOT / "config_files" / "static_info.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resource_score(record: dict[str, Any]) -> float:
    return sum(
        record.get(key, 100.0) if isinstance(record.get(key), (int, float)) else 100.0
        for key in ("avg_cpu_percent", "avg_mem_percent", "avg_npu_percent")
    )


def select_base_latency_records(profile: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for record in profile.get("records", []):
        task_type = record.get("task_type")
        device_type = record.get("device_type")
        e2e_ms = record.get("e2e_ms")
        if not task_type or not device_type or not isinstance(e2e_ms, (int, float)) or e2e_ms < 0:
            continue
        key = (task_type, device_type)
        current = selected.get(key)
        record_key = (resource_score(record), e2e_ms)
        current_key = (resource_score(current), current.get("e2e_ms")) if current else None
        if current is None or record_key < current_key:
            selected[key] = record
    return selected


def update_static_info(static_info: dict[str, Any], selected: dict[tuple[str, str], dict[str, Any]]) -> int:
    updated = 0
    for (task_type, device_type), record in selected.items():
        device_info = static_info.get(task_type, {}).get(device_type)
        if not isinstance(device_info, dict):
            continue
        overhead = device_info.setdefault("taskOverhead", {})
        if not isinstance(overhead, dict):
            continue
        overhead["base_latency_ms"] = round(float(record["e2e_ms"]), 3)
        updated += 1
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Write low-resource base latency into static_info taskOverhead.")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_PATH))
    parser.add_argument("--static-info", default=str(DEFAULT_STATIC_INFO_PATH))
    args = parser.parse_args()

    profile_path = Path(args.profile)
    static_info_path = Path(args.static_info)
    profile = load_json(profile_path)
    static_info = load_json(static_info_path)
    selected = select_base_latency_records(profile)
    updated = update_static_info(static_info, selected)
    static_info_path.write_text(json.dumps(static_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"updated": updated, "static_info": str(static_info_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
