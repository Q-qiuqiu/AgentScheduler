#!/usr/bin/env python3
import argparse
import csv
import json
import sys
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = BENCH_ROOT / "cases"
DEFAULT_IMAGE = BENCH_ROOT / "samples" / "images" / "dog1.jpg"
IMAGE_TASKS = {"image_classification", "object_detection", "semantic_segmentation"}
CSV_FIELDS = [
    "case_id",
    "expected_task",
    "status",
    "reason",
    "expected_tools",
    "actual_tools",
    "latency_sec",
    "message_preview",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(case_dir: Path, pattern: str, limit: int | None) -> list[dict[str, Any]]:
    paths = sorted(path for path in case_dir.glob(pattern) if path.name != "manifest.json")
    if limit is not None:
        paths = paths[:limit]
    cases = []
    for path in paths:
        case = load_json(path)
        case["_case_file"] = str(path)
        cases.append(case)
    return cases


def add_field(body: bytearray, boundary: str, name: str, value: Any) -> None:
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
    body.extend(str(value).encode("utf-8"))
    body.extend(b"\r\n")


def add_file(body: bytearray, boundary: str, name: str, path: Path) -> None:
    filename = path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
    )
    body.extend(path.read_bytes())
    body.extend(b"\r\n")


def encode_multipart(fields: dict[str, Any], files: dict[str, Path] | None = None) -> tuple[bytes, str]:
    boundary = f"----edgeagentbench-{uuid.uuid4().hex}"
    body = bytearray()
    for key, value in fields.items():
        add_field(body, boundary, key, value)
    for key, path in (files or {}).items():
        add_file(body, boundary, key, path)
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def post_case(agent_url: str, case: dict[str, Any], image_path: Path, timeout: int) -> dict[str, Any]:
    fields = {"text": case["query"], "debug": "1"}
    files = {"image": image_path} if case["expected_task"] in IMAGE_TASKS else {}
    body, content_type = encode_multipart(fields, files)
    request = urllib.request.Request(agent_url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(body)))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_tools(trace: list[dict[str, Any]]) -> list[str]:
    return [item.get("action") for item in trace if item.get("action") and item.get("action") != "final_response"]


def is_subsequence(expected: list[str], actual: list[str]) -> bool:
    index = 0
    for tool in actual:
        if index < len(expected) and tool == expected[index]:
            index += 1
    return index == len(expected)


def tools_match(expected: list[str], actual: list[str], mode: str) -> bool:
    if mode == "exact":
        return expected == actual
    if mode == "subsequence":
        return is_subsequence(expected, actual)
    raise RuntimeError(f"unsupported match mode: {mode}")


def tools_match_any(expected_alternatives: list[list[str]], actual: list[str], mode: str) -> bool:
    return any(tools_match(expected, actual, mode) for expected in expected_alternatives)


def tool_mismatch_reason(expected: list[str], actual: list[str]) -> str:
    if actual == ["get_task_catalog"] and expected != actual:
        return "early_stop_after_get_task_catalog"
    if actual == ["get_cluster_resources", "get_task_catalog"] and expected != actual:
        return "early_stop_after_filtered_catalog"
    if not actual:
        return "no_tool_calls"
    return "tool_sequence_mismatch"


def argument_matches(expected_args: dict[str, Any], trace: list[dict[str, Any]]) -> tuple[bool, str]:
    if not expected_args:
        return True, ""
    actions = {}
    for item in trace:
        action = item.get("action")
        if action and action not in actions:
            actions[action] = item.get("arguments") or {}
    for tool, required_args in expected_args.items():
        actual_args = actions.get(tool)
        if actual_args is None:
            return False, f"missing_args_for_{tool}"
        for key, expected_value in (required_args or {}).items():
            if actual_args.get(key) != expected_value:
                return False, f"arg_mismatch_{tool}.{key}:expected={expected_value},actual={actual_args.get(key)}"
    return True, ""


def evaluate_case(
    agent_url: str,
    case: dict[str, Any],
    image_path: Path,
    timeout: int,
    match_mode: str,
    check_arguments: bool,
) -> dict[str, Any]:
    gt = case["ground_truth"]
    expected_tools = gt.get("expected_tools", [])
    expected_tool_alternatives = gt.get("expected_tools_alternatives") or [expected_tools]
    expected_args = gt.get("expected_tool_arguments", {})
    started = time.time()
    try:
        response = post_case(agent_url, case, image_path, timeout)
        trace = response.get("trace", [])
        actual_tools = normalize_tools(trace)
        tool_ok = tools_match_any(expected_tool_alternatives, actual_tools, match_mode)
        args_ok, args_reason = argument_matches(expected_args, trace) if check_arguments else (True, "")
        ok = tool_ok and args_ok
        reason = "" if ok else (tool_mismatch_reason(expected_tools, actual_tools) if not tool_ok else args_reason)
        message = response.get("message", "")
    except urllib.error.HTTPError as exc:
        actual_tools = []
        ok = False
        reason = f"http_{exc.code}:{exc.read().decode('utf-8', errors='replace')[:200]}"
        message = ""
    except Exception as exc:
        actual_tools = []
        ok = False
        reason = f"error:{exc}"
        message = ""

    return {
        "case_id": case["case_id"],
        "expected_task": case["expected_task"],
        "status": "PASS" if ok else "FAIL",
        "reason": reason,
        "expected_tools": " || ".join("->".join(item) for item in expected_tool_alternatives),
        "actual_tools": "->".join(actual_tools),
        "latency_sec": round(time.time() - started, 3),
        "message_preview": message[:120].replace("\n", "\\n"),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def init_incremental_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writeheader()


def append_csv_row(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writerow(row)
        file_obj.flush()


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row["status"] == "PASS")
    by_task: dict[str, dict[str, int]] = {}
    for row in rows:
        item = by_task.setdefault(row["expected_task"], {"total": 0, "passed": 0})
        item["total"] += 1
        if row["status"] == "PASS":
            item["passed"] += 1
    return {
        "total": total,
        "passed": passed,
        "tool_call_accuracy": round(passed / total, 4) if total else 0,
        "by_task": {
            task: {
                **item,
                "accuracy": round(item["passed"] / item["total"], 4) if item["total"] else 0,
            }
            for task, item in sorted(by_task.items())
        },
    }


def print_progress(done: int, total: int, passed: int, started: float) -> None:
    width = 30
    ratio = done / total if total else 1
    filled = int(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    elapsed = time.time() - started
    rate = done / elapsed if elapsed > 0 else 0
    sys.stderr.write(
        f"\r[{bar}] {done}/{total} "
        f"pass={passed} fail={done - passed} "
        f"elapsed={elapsed:.1f}s rate={rate:.2f}/s"
    )
    sys.stderr.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate agent tool-call traces against EdgeAgentBench ground truth.")
    parser.add_argument("--agent-url", default="http://127.0.0.1:8082/v1/agent/chat")
    parser.add_argument("--case-dir", default=str(CASE_DIR))
    parser.add_argument("--case-pattern", default="state_*.json")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE))
    parser.add_argument("--output", default=str(BENCH_ROOT / "results" / "tool_call_results.csv"))
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--match", choices=["exact", "subsequence"], default="exact")
    parser.add_argument("--check-arguments", action="store_true", help="Also validate expected_tool_arguments when present")
    parser.add_argument("--failures", type=int, default=10)
    parser.add_argument("--progress-every", type=int, default=1, help="Update progress every N cases; set 0 to disable")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    cases = load_cases(Path(args.case_dir), args.case_pattern, args.limit)
    output_path = Path(args.output)
    init_incremental_csv(output_path)
    rows = []
    started = time.time()
    passed = 0
    for index, case in enumerate(cases, start=1):
        row = evaluate_case(args.agent_url, case, Path(args.image), args.timeout, args.match, args.check_arguments)
        rows.append(row)
        append_csv_row(output_path, row)
        if row["status"] == "PASS":
            passed += 1
        if args.progress_every and (index == 1 or index == len(cases) or index % args.progress_every == 0):
            print_progress(index, len(cases), passed, started)
    if args.progress_every:
        sys.stderr.write("\n")
    summary = summarize(rows)
    print(json.dumps({
        "case_count": len(cases),
        "output": args.output,
        "match": args.match,
        "check_arguments": args.check_arguments,
        **summary,
    }, ensure_ascii=False, indent=2))

    failures = [row for row in rows if row["status"] != "PASS"]
    for row in failures[:args.failures]:
        print(
            f"FAIL {row['case_id']}: expected={row['expected_tools']} "
            f"actual={row['actual_tools']} reason={row['reason']}"
        )
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
