#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = BENCH_ROOT / "cases"
STATIC_INFO_PATH = BENCH_ROOT.parent / "config_files" / "static_info.json"
DEFAULT_GRID = "0,0.05,0.1,0.2,0.3,0.4,0.5,0.6,0.8,1.0,1.2,1.5,2.0"
DEFAULT_BASE_GRID = "0,0.00002,0.00005,0.00008,0.0001,0.00015,0.0002,0.0003,0.0005"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(case_dir: Path, case_pattern: str, limit: int | None) -> list[dict[str, Any]]:
    paths = sorted(path for path in case_dir.glob(case_pattern) if path.name != "manifest.json")
    if limit is not None:
        paths = paths[:limit]
    return [load_json(path) for path in paths]


def parse_grid(value: str) -> list[float]:
    result = []
    for item in value.split(","):
        item = item.strip()
        if item:
            result.append(float(item))
    if not result:
        raise RuntimeError("weight grid must contain at least one value")
    return result


def normalize_platform(platform: str | None) -> str | None:
    if platform in {"RK3588", "ATLAS_L", "ATLAS_H"}:
        return platform
    if platform == "Atlas 200DK":
        return "ATLAS_L"
    if platform == "Orange Pi aipro":
        return "ATLAS_H"
    return platform


def is_excluded(node: dict[str, Any], constraints: dict[str, Any]) -> bool:
    excluded = set((constraints or {}).get("exclude_nodes", []))
    return node.get("node_id") in excluded or node.get("platform") in excluded


def is_required(node: dict[str, Any], constraints: dict[str, Any]) -> bool:
    required = set((constraints or {}).get("require_nodes", []))
    require_node = (constraints or {}).get("require_node")
    if require_node:
        required.add(require_node)
    if not required:
        return True
    return node.get("node_id") in required or node.get("platform") in required


def candidate_rows(case: dict[str, Any], static_info: dict[str, Any]) -> list[dict[str, Any]]:
    task_type = case.get("task_type")
    constraints = case.get("constraints", {})
    rows = []
    for node in case.get("nodes", []):
        if not node.get("online", False):
            continue
        if is_excluded(node, constraints) or not is_required(node, constraints):
            continue
        profile = node.get("latency_profile", {})
        if profile.get("model") != task_type:
            continue
        e2e_ms = profile.get("e2e_ms")
        if not isinstance(e2e_ms, (int, float)) or e2e_ms < 0:
            continue
        platform = normalize_platform(node.get("platform"))
        device_info = static_info.get(task_type, {}).get(platform)
        if not isinstance(device_info, dict):
            continue
        overhead = device_info.get("taskOverhead", {}) if isinstance(device_info, dict) else {}
        base_latency_ms = overhead.get("base_latency_ms", 0)
        if not isinstance(base_latency_ms, (int, float)):
            base_latency_ms = 0
        rows.append({
            "node_id": node.get("node_id"),
            "base_latency_ms": float(base_latency_ms),
            "cpu": node.get("cpu_used", 1),
            "mem": node.get("mem_used", 1),
            "npu": node.get("npu_used", 1),
            "e2e_ms": float(e2e_ms),
        })
    return rows


def build_eval_items(cases: list[dict[str, Any]], static_info: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for case in cases:
        candidates = candidate_rows(case, static_info)
        expected_ete = case.get("ground_truth", {}).get("ete_time")
        if not candidates or not isinstance(expected_ete, (int, float)):
            continue
        items.append({
            "task_type": case.get("task_type"),
            "expected_node": case.get("ground_truth", {}).get("node_id"),
            "expected_ete": float(expected_ete),
            "candidates": candidates,
        })
    return items


def evaluate_weights(
    eval_items: list[dict[str, Any]],
    base_latency_weight: float,
    cpu_weight: float,
    mem_weight: float,
    npu_weight: float,
) -> dict[str, Any]:
    total = 0
    passed = 0
    predicted_sum = 0.0
    expected_sum = 0.0
    by_task: dict[str, dict[str, Any]] = {}

    for item in eval_items:
        candidates = item["candidates"]
        ranked = []
        for candidate in candidates:
            score = (
                base_latency_weight * candidate["base_latency_ms"]
                + cpu_weight * candidate["cpu"]
                + mem_weight * candidate["mem"]
                + npu_weight * candidate["npu"]
            )
            ranked.append((score, candidate["node_id"], candidate))
        ranked.sort(key=lambda item: (item[0], item[1] or ""))
        selected = ranked[0][2]
        expected_node = item["expected_node"]
        expected_ete = item["expected_ete"]

        total += 1
        if selected["node_id"] == expected_node:
            passed += 1
        predicted_sum += selected["e2e_ms"]
        expected_sum += expected_ete

        task_type = item["task_type"]
        task_item = by_task.setdefault(task_type, {"total": 0, "passed": 0, "predicted_sum": 0.0, "expected_sum": 0.0})
        task_item["total"] += 1
        if selected["node_id"] == expected_node:
            task_item["passed"] += 1
        task_item["predicted_sum"] += selected["e2e_ms"]
        task_item["expected_sum"] += expected_ete

    return {
        "base_latency_weight": base_latency_weight,
        "cpu_weight": cpu_weight,
        "mem_weight": mem_weight,
        "npu_weight": npu_weight,
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0,
        "average_expected_ete_ms": round(expected_sum / total, 3) if total else None,
        "average_predicted_ete_ms": round(predicted_sum / total, 3) if total else None,
        "average_latency_regret_ms": round((predicted_sum - expected_sum) / total, 3) if total else None,
        "task_average_ete_ms": {
            task: {
                "average_expected_ete_ms": round(item["expected_sum"] / item["total"], 3) if item["total"] else None,
                "average_predicted_ete_ms": round(item["predicted_sum"] / item["total"], 3) if item["total"] else None,
                "average_latency_regret_ms": round((item["predicted_sum"] - item["expected_sum"]) / item["total"], 3)
                if item["total"] else None,
            }
            for task, item in sorted(by_task.items())
        },
        "by_task": {
            task: {
                "total": item["total"],
                "passed": item["passed"],
                "accuracy": round(item["passed"] / item["total"], 4) if item["total"] else 0,
                "average_expected_ete_ms": round(item["expected_sum"] / item["total"], 3) if item["total"] else None,
                "average_predicted_ete_ms": round(item["predicted_sum"] / item["total"], 3) if item["total"] else None,
                "average_latency_regret_ms": round((item["predicted_sum"] - item["expected_sum"]) / item["total"], 3)
                if item["total"] else None,
            }
            for task, item in sorted(by_task.items())
        },
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "base_latency_weight",
        "cpu_weight",
        "mem_weight",
        "npu_weight",
        "total",
        "passed",
        "accuracy",
        "average_expected_ete_ms",
        "average_predicted_ete_ms",
        "average_latency_regret_ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row[key] for key in fields} for row in rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Grid-search scheduler resource weights against EdgeAgentBench ETE.")
    parser.add_argument("--case-dir", default=str(CASE_DIR))
    parser.add_argument("--case-pattern", default="*.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--base-grid", default=DEFAULT_BASE_GRID)
    parser.add_argument("--cpu-grid", default=DEFAULT_GRID)
    parser.add_argument("--mem-grid", default=DEFAULT_GRID)
    parser.add_argument("--npu-grid", default=DEFAULT_GRID)
    parser.add_argument("--output", default=str(BENCH_ROOT / "results" / "weight_search_results.csv"))
    parser.add_argument("--summary-output")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    cases = load_cases(Path(args.case_dir), args.case_pattern, args.limit)
    static_info = load_json(STATIC_INFO_PATH)
    eval_items = build_eval_items(cases, static_info)
    base_grid = parse_grid(args.base_grid)
    cpu_grid = parse_grid(args.cpu_grid)
    mem_grid = parse_grid(args.mem_grid)
    npu_grid = parse_grid(args.npu_grid)

    rows = []
    for base_latency_weight in base_grid:
        for cpu_weight in cpu_grid:
            for mem_weight in mem_grid:
                for npu_weight in npu_grid:
                    if base_latency_weight == 0 and cpu_weight == 0 and mem_weight == 0 and npu_weight == 0:
                        continue
                    rows.append(evaluate_weights(
                        eval_items,
                        base_latency_weight,
                        cpu_weight,
                        mem_weight,
                        npu_weight,
                    ))

    rows.sort(key=lambda row: (
        row["average_predicted_ete_ms"] if row["average_predicted_ete_ms"] is not None else float("inf"),
        -row["accuracy"],
        row["base_latency_weight"],
        row["cpu_weight"],
        row["mem_weight"],
        row["npu_weight"],
    ))
    best = rows[0] if rows else {}
    output_path = Path(args.output)
    summary_output_path = Path(args.summary_output) if args.summary_output else output_path.with_suffix(".summary.json")
    summary = {
        "case_count": len(cases),
        "eval_case_count": len(eval_items),
        "grid_size": len(rows),
        "best": best,
        "top": rows[:args.top],
        "output": str(output_path),
        "summary_output": str(summary_output_path),
    }

    write_csv(output_path, rows)
    summary_output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
