#!/usr/bin/env python3
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = BENCH_ROOT / "results"

DEFAULT_POLICY_FILES = {
    "Random": RESULTS_DIR / "random_results.csv",
    "Round-Robin": RESULTS_DIR / "round_robin_results.csv",
    "Container-First": RESULTS_DIR / "container_first_results.csv",
    "MEM-Min": RESULTS_DIR / "mem_min_results.csv",
    "NPU-Min": RESULTS_DIR / "npu_min_results.csv",
    "CPU-Min": RESULTS_DIR / "cpu_min_results.csv",
    "Average-Load": RESULTS_DIR / "resource_average_results.csv",
    "Weighted-Schedule": RESULTS_DIR / "weight_schedule_results.csv",
    "agent_with_history": RESULTS_DIR / "agent_selection_full_results.csv",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def task_from_case(case_id: str) -> str:
    prefix = case_id.split("_", 1)[0]
    return {
        "bert": "Bert",
        "mobilenet": "MobileNet",
        "resnet50": "ResNet50",
        "yolov5": "YoloV5",
        "deeplabv3": "deeplabv3",
    }.get(prefix, prefix)


def state_id_from_case(case_id: str) -> str:
    return case_id.split("_q_", 1)[0]


def state_id_from_name(name: str) -> str:
    return Path(name).stem


def read_policy_rows(path: Path) -> dict[str, dict[str, Any]]:
    rows = {}
    if not path.exists():
        return rows
    with path.open(encoding="utf-8", newline="") as file_obj:
        for row in csv.DictReader(file_obj):
            case_id = row.get("case_id")
            if not case_id:
                continue
            try:
                latency = float(row["predicted_ete_ms"]) if row.get("predicted_ete_ms") else None
            except ValueError:
                latency = None
            rows[case_id] = {
                "task_type": row.get("task_type") or task_from_case(case_id),
                "latency": latency,
            }
    return rows


def average_record(case_ids: list[str], policy_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total_latency = 0.0
    sample_count = 0
    missing_count = 0
    for case_id in case_ids:
        row = policy_rows.get(case_id)
        latency = row.get("latency") if row else None
        if isinstance(latency, (int, float)):
            total_latency += float(latency)
            sample_count += 1
        else:
            missing_count += 1
    return {
        "average_latency_ms": round(total_latency / sample_count, 3) if sample_count else None,
        "sample_count": sample_count,
        "case_count": len(case_ids),
        "missing_latency_count": missing_count,
    }


def build_case_indexes(policy_rows_by_name: dict[str, dict[str, dict[str, Any]]]):
    all_case_ids = set()
    task_by_case = {}
    cases_by_state: dict[str, list[str]] = defaultdict(list)
    for rows in policy_rows_by_name.values():
        for case_id, item in rows.items():
            all_case_ids.add(case_id)
            task_by_case.setdefault(case_id, item.get("task_type") or task_from_case(case_id))
    for case_id in sorted(all_case_ids):
        cases_by_state[state_id_from_case(case_id)].append(case_id)
    return task_by_case, cases_by_state


def case_ids_for_category(state_names: list[str], cases_by_state: dict[str, list[str]]) -> list[str]:
    case_ids = []
    for name in state_names:
        case_ids.extend(cases_by_state.get(state_id_from_name(name), []))
    return sorted(case_ids)


def summarize_categories(
    category_payload: dict[str, Any],
    policies: list[str],
    policy_rows_by_name: dict[str, dict[str, dict[str, Any]]],
    task_by_case: dict[str, str],
    cases_by_state: dict[str, list[str]],
) -> dict[str, Any]:
    overall_by_category = {}
    by_category_by_task = {}
    all_category_case_ids = set()

    for category, state_names in category_payload.get("categories", {}).items():
        case_ids = case_ids_for_category(state_names, cases_by_state)
        all_category_case_ids.update(case_ids)
        overall_by_category[category] = {
            policy: average_record(case_ids, policy_rows_by_name.get(policy, {}))
            for policy in policies
        }

        tasks = sorted({task_by_case.get(case_id) or task_from_case(case_id) for case_id in case_ids})
        by_category_by_task[category] = {}
        for task in tasks:
            task_case_ids = [
                case_id
                for case_id in case_ids
                if (task_by_case.get(case_id) or task_from_case(case_id)) == task
            ]
            by_category_by_task[category][task] = {
                policy: average_record(task_case_ids, policy_rows_by_name.get(policy, {}))
                for policy in policies
            }

    all_case_ids = sorted(all_category_case_ids)
    return {
        "overall_by_category": overall_by_category,
        "by_category_by_task": by_category_by_task,
        "policy_overall": {
            policy: average_record(all_case_ids, policy_rows_by_name.get(policy, {}))
            for policy in policies
        },
    }


def parse_policy_file(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("policy file must use NAME=PATH")
    name, path = value.split("=", 1)
    if not name.strip() or not path.strip():
        raise argparse.ArgumentTypeError("policy file must use NAME=PATH")
    return name.strip(), Path(path.strip())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate average latency by load/scenario category for benchmark policy CSVs."
    )
    parser.add_argument("--load-json", default=str(RESULTS_DIR / "load.json"))
    parser.add_argument("--scenario-json", default=str(RESULTS_DIR / "scenario.json"))
    parser.add_argument("--output", default=str(RESULTS_DIR / "bench.json"))
    parser.add_argument(
        "--policy-file",
        action="append",
        type=parse_policy_file,
        default=[],
        help="Override or add a policy CSV mapping, format NAME=PATH.",
    )
    args = parser.parse_args()

    policy_files = dict(DEFAULT_POLICY_FILES)
    policy_files.update(dict(args.policy_file))
    policies = list(policy_files)
    policy_rows_by_name = {policy: read_policy_rows(path) for policy, path in policy_files.items()}
    task_by_case, cases_by_state = build_case_indexes(policy_rows_by_name)

    load_payload = load_json(Path(args.load_json))
    scenario_payload = load_json(Path(args.scenario_json))
    missing_policy_files = [str(path) for path in policy_files.values() if not path.exists()]

    result = {
        "description": (
            "Average predicted latency by benchmark state category, AI task, and scheduling policy. "
            "Averages are computed over benchmark cases whose state belongs to each category."
        ),
        "latency_field": "predicted_ete_ms",
        "case_to_state_mapping": "case_id prefix before _q_ maps to state_id, e.g. yolov5_001_q_det_001 -> yolov5_001",
        "policies": policies,
        "policy_files": {policy: str(path) for policy, path in policy_files.items()},
        "load_categories_source": str(Path(args.load_json)),
        "scenario_categories_source": str(Path(args.scenario_json)),
        "load_summary": load_payload.get("summary", {}),
        "scenario_summary": scenario_payload.get("summary", {}),
        "load": {
            **summarize_categories(load_payload, policies, policy_rows_by_name, task_by_case, cases_by_state),
            "missing_policy_files": missing_policy_files,
        },
        "scenario": {
            **summarize_categories(scenario_payload, policies, policy_rows_by_name, task_by_case, cases_by_state),
            "missing_policy_files": missing_policy_files,
        },
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output_path),
        "policy_count": len(policies),
        "missing_policy_files": missing_policy_files,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
