#!/usr/bin/env python3
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASE_DIR = BENCH_ROOT / "cases"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def node_matches_identifier(node: dict[str, Any], identifier: str) -> bool:
    return identifier in {node.get("node_id"), node.get("platform")}


def predicted_node(case: dict[str, Any], predicted_id: str | None) -> dict[str, Any] | None:
    if not predicted_id:
        return None
    for node in case.get("nodes", []):
        if node_matches_identifier(node, predicted_id):
            return node
    return None


def has_constraints(case: dict[str, Any]) -> bool:
    constraints = case.get("constraints") or {}
    return bool(
        constraints.get("exclude_nodes")
        or constraints.get("require_node")
        or constraints.get("require_nodes")
    )


def constraint_types(case: dict[str, Any]) -> list[str]:
    constraints = case.get("constraints") or {}
    result = []
    if constraints.get("exclude_nodes"):
        result.append("exclude")
    if constraints.get("require_node") or constraints.get("require_nodes"):
        result.append("require")
    return result or ["constraint"]


def satisfies_constraints(case: dict[str, Any], predicted_id: str | None) -> bool:
    node = predicted_node(case, predicted_id)
    if node is None:
        return False
    constraints = case.get("constraints") or {}
    excluded = set(constraints.get("exclude_nodes") or [])
    if predicted_id in excluded or node.get("node_id") in excluded or node.get("platform") in excluded:
        return False

    required = set(constraints.get("require_nodes") or [])
    require_node = constraints.get("require_node")
    if require_node:
        required.add(require_node)
    if required and predicted_id not in required and node.get("node_id") not in required and node.get("platform") not in required:
        return False
    return True


def empty_bucket() -> dict[str, int]:
    return {
        "total": 0,
        "constraint_ok": 0,
        "gt_ok": 0,
        "missing_pred": 0,
    }


def add_bucket_result(bucket: dict[str, int], constraint_ok: bool, gt_ok: bool, missing_pred: bool) -> None:
    bucket["total"] += 1
    if constraint_ok:
        bucket["constraint_ok"] += 1
    if gt_ok:
        bucket["gt_ok"] += 1
    if missing_pred:
        bucket["missing_pred"] += 1


def finalize_bucket(bucket: dict[str, int]) -> dict[str, Any]:
    total = bucket["total"]
    return {
        **bucket,
        "constraint_accuracy": round(bucket["constraint_ok"] / total, 4) if total else 0,
        "gt_accuracy_on_constrained": round(bucket["gt_ok"] / total, 4) if total else 0,
    }


def evaluate(result_csv: Path, case_dir: Path) -> dict[str, Any]:
    overall = empty_bucket()
    by_task: dict[str, dict[str, int]] = defaultdict(empty_bucket)
    by_constraint_type: dict[str, dict[str, int]] = defaultdict(empty_bucket)

    with result_csv.open(encoding="utf-8", newline="") as file_obj:
        for row in csv.DictReader(file_obj):
            case_id = row.get("case_id")
            if not case_id:
                continue
            case_path = case_dir / f"{case_id}.json"
            if not case_path.exists():
                raise RuntimeError(f"case file not found for {case_id}: {case_path}")
            case = load_json(case_path)
            if not has_constraints(case):
                continue

            pred = (row.get("predicted_node") or row.get("selected_node") or "").strip()
            missing_pred = not bool(pred)
            constraint_ok = satisfies_constraints(case, pred)
            gt_ok = pred == case.get("ground_truth", {}).get("node_id")
            task_type = case.get("task_type") or row.get("task_type") or "unknown"

            add_bucket_result(overall, constraint_ok, gt_ok, missing_pred)
            add_bucket_result(by_task[task_type], constraint_ok, gt_ok, missing_pred)
            for constraint_type in constraint_types(case):
                add_bucket_result(by_constraint_type[constraint_type], constraint_ok, gt_ok, missing_pred)

    return {
        "result_csv": str(result_csv),
        "case_dir": str(case_dir),
        **finalize_bucket(overall),
        "by_task": {
            task: finalize_bucket(bucket)
            for task, bucket in sorted(by_task.items())
        },
        "by_constraint_type": {
            constraint_type: finalize_bucket(bucket)
            for constraint_type, bucket in sorted(by_constraint_type.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate agent constraint-following accuracy on constrained EdgeAgentBench cases.")
    parser.add_argument("result_csv", help="Agent or baseline result CSV containing case_id and predicted_node.")
    parser.add_argument("--case-dir", default=str(DEFAULT_CASE_DIR))
    parser.add_argument("--output")
    args = parser.parse_args()

    payload = evaluate(Path(args.result_csv), Path(args.case_dir))
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
