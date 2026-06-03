#!/usr/bin/env python3
import argparse
import json
import time
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BENCH_ROOT.parent
CASE_DIR = BENCH_ROOT / "cases"
DEFAULT_OUTPUT = REPO_ROOT / "config_files" / "dispatch_history.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def percent(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value) * 100, 3) if value <= 1 else round(float(value), 3)


def resource_key(resource_percent: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        resource_percent.get("cpu_percent"),
        resource_percent.get("mem_percent"),
        resource_percent.get("npu_percent"),
    )


def build_history(case_dir: Path, case_pattern: str) -> dict[str, Any]:
    timestamp_ms = int(time.time() * 1000)
    by_task: dict[str, dict[str, list[dict[str, Any]]]] = {}
    seen: dict[tuple[str, str, tuple[Any, Any, Any]], dict[str, Any]] = {}

    for path in sorted(case_dir.glob(case_pattern)):
        if path.name == "manifest.json":
            continue
        case = load_json(path)
        task_type = case.get("task_type")
        if not task_type:
            continue
        for node in case.get("nodes", []):
            if not node.get("online", False):
                continue
            profile = node.get("latency_profile", {})
            ete_ms = profile.get("e2e_ms")
            if not isinstance(ete_ms, (int, float)) or ete_ms < 0:
                continue
            device_type = node.get("platform") or node.get("device_type") or node.get("node_id")
            target_global_id = node.get("node_id")
            if not device_type or not target_global_id:
                continue
            resource_percent = {
                "cpu_percent": percent(node.get("cpu_used")),
                "mem_percent": percent(node.get("mem_used")),
                "npu_percent": percent(node.get("npu_used")),
            }
            key = (task_type, device_type, resource_key(resource_percent))
            record = {
                "timestamp_ms": timestamp_ms,
                "task_type": task_type,
                "target_global_id": target_global_id,
                "device_type": device_type,
                "resource_percent": resource_percent,
                "ete_ms": round(float(ete_ms), 3),
            }
            existing = seen.get(key)
            if existing is not None:
                existing.update(record)
                continue
            seen[key] = record
            by_task.setdefault(task_type, {}).setdefault(device_type, []).append(record)

    return {"version": 2, "by_task": by_task}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build dispatch_history.json from EdgeAgentBench cases.")
    parser.add_argument("--case-dir", default=str(CASE_DIR))
    parser.add_argument("--case-pattern", default="*.json")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    history = build_history(Path(args.case_dir), args.case_pattern)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    count = sum(
        len(records)
        for by_device in history["by_task"].values()
        for records in by_device.values()
    )
    print(json.dumps({
        "output": str(output),
        "task_count": len(history["by_task"]),
        "record_count": count,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
