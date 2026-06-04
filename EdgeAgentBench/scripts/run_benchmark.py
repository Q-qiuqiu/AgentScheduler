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
SCHEDULER_WEIGHTS = {
    "base_latency_ms": 0.0003,
    "cpu_after": 0.01,
    "mem_after": 2.0,
    "npu_after": 0.8,
}


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


def case_model(case: dict[str, Any]) -> str | None:
    return case.get("task_type")


def load_static_info() -> dict[str, Any]:
    return load_json(STATIC_INFO_PATH)


def candidate_nodes(case: dict[str, Any], state: dict[str, Any], honor_constraints: bool) -> list[dict[str, Any]]:
    model_name = case_model(case)
    constraints = case.get("constraints", {}) if honor_constraints else {}
    candidates = []
    for node in state.get("nodes", []):
        if not node.get("online", False):
            continue
        profile_model = node.get("latency_profile", {}).get("model")
        if profile_model != model_name:
            continue
        profile_e2e = node.get("latency_profile", {}).get("e2e_ms")
        if not isinstance(profile_e2e, (int, float)) or profile_e2e < 0:
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
        "selected_node": gt.get("node_id"),
        "score": gt.get("ete_time"),
        "eligible_nodes": [],
    }


def feasible_scheduler_candidates(
    case: dict[str, Any],
    state: dict[str, Any],
    static_info: dict[str, Any],
    honor_constraints: bool,
) -> list[dict[str, Any]]:
    model_name = case_model(case)
    model_profile = static_info.get(model_name, {})
    candidates = []

    for node in candidate_nodes(case, state, honor_constraints):
        platform = normalize_platform(node.get("platform"))
        profile = model_profile.get(platform)
        if not profile:
            continue

        overhead = profile.get("taskOverhead", {}) if isinstance(profile, dict) else {}
        cpu_after = node.get("cpu_used", 1)
        mem_after = node.get("mem_used", 1)
        npu_after = node.get("npu_used", 1)
        base_latency_ms = overhead.get("base_latency_ms", 0)
        if not isinstance(base_latency_ms, (int, float)):
            base_latency_ms = 0

        candidates.append({
            "node": node,
            "detail": {
                "base_latency_ms": round(base_latency_ms, 3),
                "cpu_after": round(cpu_after, 4),
                "mem_after": round(mem_after, 4),
                "npu_after": round(npu_after, 4),
                "container_running": bool(node.get("container_running", False)),
                "cold_start": not bool(node.get("container_running", False)),
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
        cpu_after = detail["cpu_after"]
        mem_after = detail["mem_after"]
        npu_after = detail["npu_after"]
        base_latency_ms = detail.get("base_latency_ms", 0)
        detail = {**detail, "scheduler_weights": SCHEDULER_WEIGHTS}
        score = round(
            SCHEDULER_WEIGHTS["base_latency_ms"] * base_latency_ms
            + SCHEDULER_WEIGHTS["cpu_after"] * cpu_after
            + SCHEDULER_WEIGHTS["npu_after"] * npu_after
            + SCHEDULER_WEIGHTS["mem_after"] * mem_after,
            4,
        )
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
        "RK3588",
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


def ranked_candidate_prediction(
    case: dict[str, Any],
    state: dict[str, Any],
    static_info: dict[str, Any],
    honor_constraints: bool,
    policy: str,
) -> dict[str, Any]:
    candidates = feasible_scheduler_candidates(case, state, static_info, honor_constraints)
    if not candidates:
        return {"selected_node": None, "selected_platform": None, "score": None, "eligible_nodes": []}

    ranked = []
    for item in candidates:
        node = item["node"]
        detail = item["detail"]
        avg_load = round((detail["cpu_after"] + detail["mem_after"] + detail["npu_after"]) / 3, 4)
        if policy == "resource_average":
            score = avg_load
            sort_key = (score, node.get("node_id", ""))
        elif policy == "cpu_min":
            score = detail["cpu_after"]
            sort_key = (score, node.get("node_id", ""))
        elif policy == "npu_min":
            score = detail["npu_after"]
            sort_key = (score, node.get("node_id", ""))
        elif policy == "mem_min":
            score = detail["mem_after"]
            sort_key = (score, node.get("node_id", ""))
        elif policy == "container_first":
            score = avg_load
            sort_key = (1 if detail["cold_start"] else 0, score, node.get("node_id", ""))
        else:
            raise RuntimeError(f"unsupported ranked baseline policy: {policy}")
        ranked.append((sort_key, score, node, detail))

    ranked.sort(key=lambda item: item[0])
    _, score, node, detail = ranked[0]
    return {
        "selected_node": node.get("node_id"),
        "selected_platform": node.get("platform"),
        "score": score,
        "eligible_nodes": [item[2].get("node_id") for item in ranked],
        "detail": detail,
    }


def node_ete_ms(case: dict[str, Any], node_id: str | None) -> float | None:
    if not node_id:
        return None
    for node in case.get("nodes", []):
        if node.get("node_id") != node_id:
            continue
        e2e_ms = node.get("latency_profile", {}).get("e2e_ms")
        if isinstance(e2e_ms, (int, float)) and e2e_ms >= 0:
            return float(e2e_ms)
    return None


def evaluate_case(
    case: dict[str, Any],
    policy: str,
    static_info: dict[str, Any],
    honor_constraints: bool,
    runner_state: dict[str, Any],
) -> dict[str, Any]:
    gt = case["ground_truth"]
    task_type = case_model(case)
    state = {"nodes": case.get("nodes", [])}
    expected_node = gt.get("node_id")
    if policy == "ground_truth":
        prediction = ground_truth_prediction(case)
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
    elif policy in {"resource_average", "cpu_min", "npu_min", "mem_min", "container_first"}:
        prediction = ranked_candidate_prediction(
            case,
            state,
            static_info,
            honor_constraints,
            policy,
        )
    else:
        raise RuntimeError(f"unsupported policy: {policy}")

    predicted_node = prediction.get("selected_node")
    expected_ete_ms = gt.get("ete_time")
    predicted_ete_ms = node_ete_ms(case, predicted_node)
    latency_regret_ms = None
    latency_ratio = None
    if isinstance(expected_ete_ms, (int, float)) and predicted_ete_ms is not None:
        latency_regret_ms = round(predicted_ete_ms - expected_ete_ms, 3)
        latency_ratio = round(predicted_ete_ms / expected_ete_ms, 4) if expected_ete_ms > 0 else None
    passed = predicted_node == expected_node
    return {
        "case_id": case["case_id"],
        "task_type": task_type,
        "status": "PASS" if passed else "FAIL",
        "reason": "" if passed else "selected_node_mismatch",
        "expected_node": expected_node,
        "predicted_node": predicted_node,
        "expected_ete_ms": expected_ete_ms,
        "predicted_ete_ms": predicted_ete_ms,
        "latency_regret_ms": latency_regret_ms,
        "latency_ratio": latency_ratio,
        "predicted_score": prediction.get("score"),
        "eligible_nodes": ",".join(prediction.get("eligible_nodes", [])),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "case_id",
        "task_type",
        "status",
        "reason",
        "expected_node",
        "predicted_node",
        "expected_ete_ms",
        "predicted_ete_ms",
        "latency_regret_ms",
        "latency_ratio",
        "predicted_score",
        "eligible_nodes",
    ]
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row["status"] == "PASS")
    latency_rows = [row for row in rows if isinstance(row.get("predicted_ete_ms"), (int, float))]
    by_task: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = by_task.setdefault(row["task_type"], {"total": 0, "passed": 0, "rows": []})
        item["total"] += 1
        if row["status"] == "PASS":
            item["passed"] += 1
        if isinstance(row.get("predicted_ete_ms"), (int, float)):
            item["rows"].append(row)
    return {
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0,
        "latency_sample_count": len(latency_rows),
        "average_expected_ete_ms": round(
            sum(row["expected_ete_ms"] for row in latency_rows) / len(latency_rows), 3
        ) if latency_rows else None,
        "average_predicted_ete_ms": round(
            sum(row["predicted_ete_ms"] for row in latency_rows) / len(latency_rows), 3
        ) if latency_rows else None,
        "average_latency_regret_ms": round(
            sum(row["latency_regret_ms"] for row in latency_rows) / len(latency_rows), 3
        ) if latency_rows else None,
        "task_average_ete_ms": {
            task: {
                "average_expected_ete_ms": round(
                    sum(row["expected_ete_ms"] for row in item["rows"]) / len(item["rows"]), 3
                ) if item["rows"] else None,
                "average_predicted_ete_ms": round(
                    sum(row["predicted_ete_ms"] for row in item["rows"]) / len(item["rows"]), 3
                ) if item["rows"] else None,
                "average_latency_regret_ms": round(
                    sum(row["latency_regret_ms"] for row in item["rows"]) / len(item["rows"]), 3
                ) if item["rows"] else None,
            }
            for task, item in sorted(by_task.items())
        },
        "by_task": {
            task: {
                "total": item["total"],
                "passed": item["passed"],
                "accuracy": round(item["passed"] / item["total"], 4) if item["total"] else 0,
                "latency_sample_count": len(item["rows"]),
                "average_expected_ete_ms": round(
                    sum(row["expected_ete_ms"] for row in item["rows"]) / len(item["rows"]), 3
                ) if item["rows"] else None,
                "average_predicted_ete_ms": round(
                    sum(row["predicted_ete_ms"] for row in item["rows"]) / len(item["rows"]), 3
                ) if item["rows"] else None,
                "average_latency_regret_ms": round(
                    sum(row["latency_regret_ms"] for row in item["rows"]) / len(item["rows"]), 3
                ) if item["rows"] else None,
            }
            for task, item in sorted(by_task.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run EdgeAgentBench cases.")
    parser.add_argument("--case-dir", default=str(CASE_DIR), help="Directory containing one JSON file per case")
    parser.add_argument("--case-pattern", default="*.json", help="Glob pattern for case files")
    parser.add_argument(
        "--policy",
        choices=[
            "ground_truth",
            "scheduler",
            "random",
            "round_robin",
            "resource_average",
            "cpu_min",
            "npu_min",
            "mem_min",
            "container_first",
        ],
        default="scheduler",
        help="Prediction policy to evaluate against case ground truth",
    )
    parser.add_argument(
        "--ignore-constraints",
        action="store_true",
        help="Ignore query exclude_nodes constraints when computing predictions",
    )
    parser.add_argument("--output", default=str(BENCH_ROOT / "results" / "runner_results.csv"))
    parser.add_argument(
        "--summary-output",
        help="Path for summary JSON. Defaults to the CSV output path with .summary.json suffix.",
    )
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
    output_path = Path(args.output)
    summary_output_path = Path(args.summary_output) if args.summary_output else output_path.with_suffix(".summary.json")
    summary_payload = {
        "policy": args.policy,
        "seed": args.seed if args.policy == "random" else None,
        "case_count": len(cases),
        "output": args.output,
        "summary_output": str(summary_output_path),
        **summary,
    }
    write_csv(output_path, rows)
    write_summary_json(summary_output_path, summary_payload)

    print(json.dumps(summary_payload, ensure_ascii=False, indent=2))

    failures = [row for row in rows if row["status"] != "PASS"]
    for row in failures[:args.failures]:
        print(
            f"FAIL {row['case_id']}: expected={row['expected_node']} "
            f"predicted={row['predicted_node']} task={row['task_type']}"
        )
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
