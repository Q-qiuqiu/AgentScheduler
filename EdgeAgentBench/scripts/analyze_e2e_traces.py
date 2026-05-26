#!/usr/bin/env python3
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = BENCH_ROOT / "results" / "e2e_traces.jsonl"
DEFAULT_JSON_OUTPUT = BENCH_ROOT / "results" / "e2e_trace_summary.json"
DEFAULT_CSV_OUTPUT = BENCH_ROOT / "results" / "e2e_trace_summary.csv"


SEGMENTS = [
    "request_elapsed_ms",
    "agent_e2e_ms",
    "model_action_selection_total_ms",
    "model_final_response_ms",
    "model_total_ms",
    "tool.get_cluster_resources",
    "tool.get_task_catalog",
    "tool.get_task_overhead",
    "tool.schedule_task",
    "tool.run_task_on_node",
    "tool_total_ms",
    "gateway_time_ms",
    "model_exec_time_ms",
]


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as file_obj:
        for line_no, line in enumerate(file_obj, start=1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["_line_no"] = line_no
            rows.append(row)
    return rows


def trace_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    response = row.get("response") or {}
    trace = response.get("trace") or []
    return trace if isinstance(trace, list) else []


def elapsed_ms(timing: Any) -> float:
    if isinstance(timing, dict):
        value = timing.get("elapsed_ms")
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def extract_metrics(row: dict[str, Any]) -> dict[str, float]:
    metrics = {segment: 0.0 for segment in SEGMENTS}
    metrics["request_elapsed_ms"] = float(row.get("request_elapsed_ms") or 0)

    summary = row.get("summary") or {}
    agent_e2e = summary.get("agent_e2e_ms")
    if isinstance(agent_e2e, (int, float)):
        metrics["agent_e2e_ms"] = float(agent_e2e)

    for item in trace_items(row):
        action = item.get("action")
        model_ms = elapsed_ms(item.get("model_timing"))
        final_model_ms = elapsed_ms(item.get("final_model_timing"))
        tool_ms = elapsed_ms(item.get("tool_timing"))

        metrics["model_action_selection_total_ms"] += model_ms
        metrics["model_final_response_ms"] += final_model_ms
        metrics["model_total_ms"] += model_ms + final_model_ms
        metrics["tool_total_ms"] += tool_ms
        if action:
            key = f"tool.{action}"
            if key in metrics:
                metrics[key] += tool_ms

        if action == "run_task_on_node":
            result = item.get("result") or {}
            gateway_time = result.get("gateway_time")
            exec_time = result.get("exec_time")
            if isinstance(gateway_time, (int, float)):
                metrics["gateway_time_ms"] += float(gateway_time)
            if isinstance(exec_time, (int, float)):
                metrics["model_exec_time_ms"] += float(exec_time)

    return metrics


def summarize_values(values: list[float]) -> dict[str, float]:
    if not values:
        return {"avg_ms": 0.0, "median_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0}
    return {
        "avg_ms": round(mean(values), 3),
        "median_ms": round(median(values), 3),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
    }


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, float]]] = defaultdict(list)
    failures = []

    for row in rows:
        task = row.get("task") or "unknown"
        if row.get("status") != "success":
            failures.append({
                "line_no": row.get("_line_no"),
                "task": task,
                "status": row.get("status"),
                "error": row.get("error"),
            })
            continue
        grouped[task].append(extract_metrics(row))

    by_task = {}
    for task, metric_rows in sorted(grouped.items()):
        segment_stats = {}
        for segment in SEGMENTS:
            values = [metrics.get(segment, 0.0) for metrics in metric_rows]
            segment_stats[segment] = summarize_values(values)
        by_task[task] = {
            "success_count": len(metric_rows),
            "segments": segment_stats,
        }

    return {
        "total_rows": len(rows),
        "success_rows": sum(len(items) for items in grouped.values()),
        "failed_rows": len(failures),
        "failures": failures,
        "by_task": by_task,
    }


def write_csv(summary: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["task", "success_count", "segment", "avg_ms", "median_ms", "min_ms", "max_ms"]
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        for task, task_summary in summary["by_task"].items():
            for segment, stats in task_summary["segments"].items():
                writer.writerow({
                    "task": task,
                    "success_count": task_summary["success_count"],
                    "segment": segment,
                    **stats,
                })


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate EdgeAgentBench end-to-end trace timing JSONL.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--json-output", default=str(DEFAULT_JSON_OUTPUT))
    parser.add_argument("--csv-output", default=str(DEFAULT_CSV_OUTPUT))
    args = parser.parse_args()

    input_path = Path(args.input)
    rows = load_rows(input_path)
    summary = analyze(rows)

    json_output = Path(args.json_output)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(summary, Path(args.csv_output))
    print(json.dumps({
        "input": str(input_path),
        "json_output": str(json_output),
        "csv_output": args.csv_output,
        "total_rows": summary["total_rows"],
        "success_rows": summary["success_rows"],
        "failed_rows": summary["failed_rows"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
