#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = BENCH_ROOT / "results" / "toolcall_results.csv"
DEFAULT_OUTPUT = BENCH_ROOT / "results" / "toolcall_counts_summary.json"


def parse_sequence(value: str) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split("->") if item.strip()]


def parse_expected_alternatives(value: str) -> list[list[str]]:
    if not value:
        return [[]]
    return [parse_sequence(item.strip()) for item in value.split("||")]


def correct_prefix_count(expected: list[str], actual: list[str]) -> int:
    count = 0
    for expected_tool, actual_tool in zip(expected, actual):
        if expected_tool != actual_tool:
            break
        count += 1
    return count


def best_prefix_match(expected_alternatives: list[list[str]], actual: list[str]) -> tuple[int, list[str]]:
    best_count = -1
    best_expected: list[str] = []
    for expected in expected_alternatives:
        count = correct_prefix_count(expected, actual)
        if count > best_count:
            best_count = count
            best_expected = expected
    return max(best_count, 0), best_expected


def empty_stats() -> dict[str, Any]:
    return {
        "case_count": 0,
        "full_sequence_correct": 0,
        "expected_tool_count_sum": 0,
        "actual_tool_count_sum": 0,
        "correct_prefix_tool_count_sum": 0,
        "correct_prefix_ratio_sum": 0.0,
    }


def finalize_stats(stats: dict[str, Any]) -> dict[str, Any]:
    total = stats["case_count"]
    if not total:
        return {
            "case_count": 0,
            "full_sequence_correct": 0,
            "full_sequence_accuracy": 0,
            "avg_expected_tool_count": 0,
            "avg_actual_tool_count": 0,
            "avg_correct_prefix_tool_count": 0,
            "avg_correct_prefix_ratio": 0,
        }
    return {
        "case_count": total,
        "full_sequence_correct": stats["full_sequence_correct"],
        "full_sequence_accuracy": round(stats["full_sequence_correct"] / total, 4),
        "avg_expected_tool_count": round(stats["expected_tool_count_sum"] / total, 4),
        "avg_actual_tool_count": round(stats["actual_tool_count_sum"] / total, 4),
        "avg_correct_prefix_tool_count": round(stats["correct_prefix_tool_count_sum"] / total, 4),
        "avg_correct_prefix_ratio": round(stats["correct_prefix_ratio_sum"] / total, 4),
    }


def analyze(input_path: Path) -> dict[str, Any]:
    by_task: dict[str, dict[str, Any]] = {}
    overall = empty_stats()

    with input_path.open("r", encoding="utf-8", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        for row in reader:
            expected_task = row.get("expected_task") or "unknown"
            expected_alternatives = parse_expected_alternatives(row.get("expected_tools", ""))
            actual = parse_sequence(row.get("actual_tools", ""))
            correct_count, best_expected = best_prefix_match(expected_alternatives, actual)
            expected_count = len(best_expected)
            full_correct = expected_count == len(actual) and correct_count == expected_count
            prefix_ratio = correct_count / expected_count if expected_count else 0

            for stats in (overall, by_task.setdefault(expected_task, empty_stats())):
                stats["case_count"] += 1
                stats["expected_tool_count_sum"] += expected_count
                stats["actual_tool_count_sum"] += len(actual)
                stats["correct_prefix_tool_count_sum"] += correct_count
                stats["correct_prefix_ratio_sum"] += prefix_ratio
                if full_correct:
                    stats["full_sequence_correct"] += 1

    return {
        "input": str(input_path),
        "metric_definition": {
            "actual_tool_count": "number of tool calls in actual_tools",
            "correct_prefix_tool_count": "number of consecutive tools matching expected_tools from the first tool until the first mismatch",
            "correct_prefix_ratio": "correct_prefix_tool_count divided by expected tool count",
            "full_sequence_correct": "actual sequence exactly equals the best-matching expected alternative",
        },
        "overall": finalize_stats(overall),
        "by_task": {task: finalize_stats(stats) for task, stats in sorted(by_task.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze average actual and correct-prefix tool-call counts from run_tool_call_benchmark CSV output."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"input CSV not found: {input_path}")

    summary = analyze(input_path)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
