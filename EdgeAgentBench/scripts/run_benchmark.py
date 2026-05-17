#!/usr/bin/env python3
import argparse
import csv
import json
import random
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = BENCH_ROOT / "cases"
STATIC_INFO_PATH = BENCH_ROOT.parent / "config_files" / "static_info.json"

TASK_TO_MODEL = {
    "object_detection": "YoloV5",
    "image_classification": "MobileNet",
    "semantic_segmentation": "deeplabv3",
    "text_classification": "Bert",
}

EXECUTION_TASKS = set(TASK_TO_MODEL)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(case_dir: Path, case_pattern: str) -> list[dict[str, Any]]:
    paths = sorted(path for path in case_dir.glob(case_pattern) if path.name != "manifest.json")
    cases = []
    for path in paths:
        case = load_json(path)
        case["_case_file"] = str(path)
        cases.append(case)
    return cases


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


def normalize_platform(platform: str | None) -> str | None:
    if platform in {"RK3588", "ATLAS_L", "ATLAS_H"}:
        return platform
    if platform == "Atlas 200DK":
        return "ATLAS_L"
    if platform == "Orange Pi aipro":
        return "ATLAS_H"
    return platform


def load_state(case: dict[str, Any]) -> dict[str, Any]:
    return load_json(BENCH_ROOT / case["state_path"])


def load_static_info() -> dict[str, Any]:
    return load_json(STATIC_INFO_PATH)


def candidate_nodes(case: dict[str, Any], state: dict[str, Any], honor_constraints: bool) -> list[dict[str, Any]]:
    task = case["expected_task"]
    constraints = case.get("constraints", {}) if honor_constraints else {}
    candidates = []
    for node in state.get("nodes", []):
        if not node.get("online", False):
            continue
        if task not in node.get("support_tasks", []):
            continue
        if is_excluded(node, constraints):
            continue
        if not is_required(node, constraints):
            continue
        candidates.append(node)
    return candidates


def ground_truth_prediction(case: dict[str, Any]) -> dict[str, Any]:
    gt = case["ground_truth"]
    return {
        "selected_node": gt.get("selected_node"),
        "selected_platform": gt.get("selected_platform"),
        "score": gt.get("oracle_score"),
        "eligible_nodes": gt.get("eligible_nodes", []),
    }


def case_oracle_prediction(case: dict[str, Any], state: dict[str, Any], honor_constraints: bool) -> dict[str, Any]:
    weights = case["oracle"]["weights"]
    task = case["expected_task"]
    ranked = []
    for node in candidate_nodes(case, state, honor_constraints):
        cold_start = 0 if task in node.get("ready_containers", []) else 1
        score = (
            weights["npu_used"] * node.get("npu_used", 1)
            + weights["cpu_used"] * node.get("cpu_used", 1)
            + weights["mem_used"] * node.get("mem_used", 1)
            + weights["cold_start_penalty"] * cold_start
        )
        ranked.append((round(score, 4), node))
    ranked.sort(key=lambda item: (item[0], item[1].get("node_id", "")))
    if not ranked:
        return {"selected_node": None, "selected_platform": None, "score": None, "eligible_nodes": []}
    score, node = ranked[0]
    return {
        "selected_node": node.get("node_id"),
        "selected_platform": node.get("platform"),
        "score": score,
        "eligible_nodes": [item[1].get("node_id") for item in ranked],
    }


def feasible_scheduler_candidates(
    case: dict[str, Any],
    state: dict[str, Any],
    static_info: dict[str, Any],
    honor_constraints: bool,
) -> list[dict[str, Any]]:
    task = case["expected_task"]
    model_name = TASK_TO_MODEL[task]
    model_profile = static_info.get(model_name, {})
    candidates = []

    for node in candidate_nodes(case, state, honor_constraints):
        platform = normalize_platform(node.get("platform"))
        profile = model_profile.get(platform)
        if not profile:
            continue

        overhead = profile.get("taskOverhead", {})
        cold_start = task not in node.get("ready_containers", [])
        cpu_after = node.get("cpu_used", 1)
        mem_after = node.get("mem_used", 1)
        npu_after = node.get("npu_used", 1)
        if cold_start:
            cpu_after += overhead.get("cpu_usage", 0)
            mem_after += overhead.get("mem_usage", 0)
            npu_after += overhead.get("xpu_usage", 0)

        if cpu_after > 1 or mem_after > 1 or npu_after > 1:
            continue

        candidates.append({
            "node": node,
            "detail": {
                "cpu_after": round(cpu_after, 4),
                "mem_after": round(mem_after, 4),
                "npu_after": round(npu_after, 4),
                "cold_start": cold_start,
                "model_name": model_name,
                "device_type": platform,
            },
        })
    return candidates


def scheduler_prediction(
    case: dict[str, Any],
    state: dict[str, Any],
    static_info: dict[str, Any],
    honor_constraints: bool,
) -> dict[str, Any]:
    ranked = []

    for item in feasible_scheduler_candidates(case, state, static_info, honor_constraints):
        node = item["node"]
        detail = item["detail"]
        # Mirrors gateway scheduleDeviceByTType: choose min CPU + XPU + 0.1 * MEM
        # among feasible devices.
        score = round(detail["cpu_after"] + detail["npu_after"] + 0.1 * detail["mem_after"], 4)
        ranked.append((score, node, detail))

    ranked.sort(key=lambda item: (item[0], item[1].get("node_id", "")))
    if not ranked:
        return {"selected_node": None, "selected_platform": None, "score": None, "eligible_nodes": []}
    score, node, detail = ranked[0]
    return {
        "selected_node": node.get("node_id"),
        "selected_platform": node.get("platform"),
        "score": score,
        "eligible_nodes": [item[1].get("node_id") for item in ranked],
        "detail": detail,
    }


def random_prediction(
    case: dict[str, Any],
    state: dict[str, Any],
    static_info: dict[str, Any],
    honor_constraints: bool,
    rng: random.Random,
) -> dict[str, Any]:
    candidates = feasible_scheduler_candidates(case, state, static_info, honor_constraints)
    candidates.sort(key=lambda item: item["node"].get("node_id", ""))
    if not candidates:
        return {"selected_node": None, "selected_platform": None, "score": None, "eligible_nodes": []}

    selected = rng.choice(candidates)
    node = selected["node"]
    return {
        "selected_node": node.get("node_id"),
        "selected_platform": node.get("platform"),
        "score": None,
        "eligible_nodes": [item["node"].get("node_id") for item in candidates],
        "detail": selected["detail"],
    }


def round_robin_prediction(
    case: dict[str, Any],
    state: dict[str, Any],
    static_info: dict[str, Any],
    honor_constraints: bool,
    runner_state: dict[str, Any],
) -> dict[str, Any]:
    candidates = feasible_scheduler_candidates(case, state, static_info, honor_constraints)
    if not candidates:
        return {"selected_node": None, "selected_platform": None, "score": None, "eligible_nodes": []}

    candidate_by_id = {item["node"].get("node_id"): item for item in candidates}
    node_order = runner_state.setdefault("round_robin_node_order", [
        "RK3588_1",
        "RK3588_2",
        "ATLAS_L",
        "ATLAS_H",
    ])
    index = runner_state.setdefault("round_robin_index", 0)

    selected = None
    selected_index = index
    for offset in range(len(node_order)):
        candidate_index = (index + offset) % len(node_order)
        node_id = node_order[candidate_index]
        if node_id in candidate_by_id:
            selected = candidate_by_id[node_id]
            selected_index = candidate_index
            break

    if selected is None:
        candidates.sort(key=lambda item: item["node"].get("node_id", ""))
        selected = candidates[0]

    runner_state["round_robin_index"] = (selected_index + 1) % len(node_order)
    node = selected["node"]
    return {
        "selected_node": node.get("node_id"),
        "selected_platform": node.get("platform"),
        "score": None,
        "eligible_nodes": [item["node"].get("node_id") for item in candidates],
        "detail": selected["detail"],
    }


def evaluate_case(
    case: dict[str, Any],
    policy: str,
    static_info: dict[str, Any],
    honor_constraints: bool,
    runner_state: dict[str, Any],
) -> dict[str, Any]:
    gt = case["ground_truth"]
    expected_task = case["expected_task"]
    should_schedule = gt.get("should_schedule", False)

    if not should_schedule:
        return {
            "case_id": case["case_id"],
            "expected_task": expected_task,
            "status": "PASS",
            "reason": "resource_query",
            "should_schedule": False,
            "expected_node": None,
            "predicted_node": None,
            "expected_tools": ",".join(gt.get("expected_tools", [])),
            "predicted_score": None,
            "eligible_nodes": "",
        }

    state = load_state(case)
    expected_node = gt.get("selected_node")
    if policy == "ground_truth":
        prediction = ground_truth_prediction(case)
    elif policy == "case_oracle":
        prediction = case_oracle_prediction(case, state, honor_constraints)
    elif policy == "scheduler":
        prediction = scheduler_prediction(case, state, static_info, honor_constraints)
    elif policy == "random":
        prediction = random_prediction(
            case,
            state,
            static_info,
            honor_constraints,
            runner_state["rng"],
        )
    elif policy == "round_robin":
        prediction = round_robin_prediction(
            case,
            state,
            static_info,
            honor_constraints,
            runner_state,
        )
    else:
        raise RuntimeError(f"unsupported policy: {policy}")

    predicted_node = prediction.get("selected_node")
    passed = predicted_node == expected_node
    return {
        "case_id": case["case_id"],
        "expected_task": expected_task,
        "status": "PASS" if passed else "FAIL",
        "reason": "" if passed else "selected_node_mismatch",
        "should_schedule": True,
        "expected_node": expected_node,
        "predicted_node": predicted_node,
        "expected_tools": ",".join(gt.get("expected_tools", [])),
        "predicted_score": prediction.get("score"),
        "eligible_nodes": ",".join(prediction.get("eligible_nodes", [])),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "case_id",
        "expected_task",
        "status",
        "reason",
        "should_schedule",
        "expected_node",
        "predicted_node",
        "expected_tools",
        "predicted_score",
        "eligible_nodes",
    ]
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row["status"] == "PASS")
    scheduled = [row for row in rows if row["should_schedule"]]
    scheduled_passed = sum(1 for row in scheduled if row["status"] == "PASS")
    by_task: dict[str, dict[str, int]] = {}
    for row in rows:
        item = by_task.setdefault(row["expected_task"], {"total": 0, "passed": 0})
        item["total"] += 1
        if row["status"] == "PASS":
            item["passed"] += 1
    return {
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0,
        "scheduled_total": len(scheduled),
        "scheduled_passed": scheduled_passed,
        "scheduled_accuracy": round(scheduled_passed / len(scheduled), 4) if scheduled else 0,
        "by_task": {
            task: {
                **item,
                "accuracy": round(item["passed"] / item["total"], 4) if item["total"] else 0,
            }
            for task, item in sorted(by_task.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run EdgeAgentBench cases.")
    parser.add_argument("--case-dir", default=str(CASE_DIR), help="Directory containing one JSON file per case")
    parser.add_argument("--case-pattern", default="state_*.json", help="Glob pattern for case files")
    parser.add_argument(
        "--policy",
        choices=["ground_truth", "case_oracle", "scheduler", "random", "round_robin"],
        default="scheduler",
        help="Prediction policy to evaluate against case ground truth",
    )
    parser.add_argument(
        "--ignore-constraints",
        action="store_true",
        help="Ignore query exclude_nodes constraints when computing predictions",
    )
    parser.add_argument("--output", default=str(BENCH_ROOT / "results" / "runner_results.csv"))
    parser.add_argument("--failures", type=int, default=10, help="Number of failures to print")
    parser.add_argument("--seed", type=int, default=42, help="Random seed used by random baseline")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 when any case fails")
    args = parser.parse_args()

    cases = load_cases(Path(args.case_dir), args.case_pattern)
    static_info = load_static_info()
    runner_state = {"rng": random.Random(args.seed)}
    rows = [
        evaluate_case(
            case,
            args.policy,
            static_info,
            honor_constraints=not args.ignore_constraints,
            runner_state=runner_state,
        )
        for case in cases
    ]
    summary = summarize(rows)
    write_csv(Path(args.output), rows)

    print(json.dumps({
        "policy": args.policy,
        "seed": args.seed if args.policy == "random" else None,
        "case_count": len(cases),
        "output": args.output,
        **summary,
    }, ensure_ascii=False, indent=2))

    failures = [row for row in rows if row["status"] != "PASS"]
    for row in failures[:args.failures]:
        print(
            f"FAIL {row['case_id']}: expected={row['expected_node']} "
            f"predicted={row['predicted_node']} task={row['expected_task']}"
        )
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
