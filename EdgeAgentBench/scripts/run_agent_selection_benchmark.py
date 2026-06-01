#!/usr/bin/env python3
import argparse
import csv
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = BENCH_ROOT / "cases"
DEFAULT_IMAGE_PATH = BENCH_ROOT / "samples" / "images" / "dog1.jpg"
DEFAULT_CURRENT_CASE_FILE = Path("/tmp/edgeagentbench_current_case.txt")

FILE_TASKS = {"YoloV5", "MobileNet", "ResNet50", "deeplabv3"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(case_dir: Path, case_pattern: str, limit: int | None) -> list[dict[str, Any]]:
    paths = sorted(path for path in case_dir.glob(case_pattern) if path.name != "manifest.json")
    if limit is not None:
        paths = paths[:limit]
    cases = []
    for path in paths:
        case = load_json(path)
        case["_case_file"] = str(path)
        cases.append(case)
    return cases


def encode_multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = f"----EdgeAgentBench{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    for name, path in files.items():
        filename = path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def post_agent(agent_url: str, fields: dict[str, str], files: dict[str, Path], timeout: int) -> dict[str, Any]:
    body, boundary = encode_multipart(fields, files)
    request = urllib.request.Request(agent_url, data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"agent returned HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach agent at {agent_url}: {exc.reason}") from exc


def selected_node_from_response(response: dict[str, Any]) -> str | None:
    trace = response.get("trace", [])
    if isinstance(trace, list):
        for item in reversed(trace):
            if not isinstance(item, dict) or item.get("action") != "run_task_on_node":
                continue
            arguments = item.get("arguments") or {}
            if isinstance(arguments, dict):
                return arguments.get("target_global_id")

    context = response.get("final_state_context", {})
    if isinstance(context, dict):
        selected = context.get("selected_execution", {})
        if isinstance(selected, dict):
            return selected.get("target_global_id")
    return None


def evaluate_case(
    case: dict[str, Any],
    agent_url: str,
    current_case_file: Path,
    image_path: Path,
    force_task_type: bool,
    timeout: int,
) -> dict[str, Any]:
    case_path = Path(case["_case_file"]).resolve()
    current_case_file.parent.mkdir(parents=True, exist_ok=True)
    current_case_file.write_text(str(case_path) + "\n", encoding="utf-8")

    task_type = case.get("task_type")
    fields = {
        "text": case.get("query", ""),
        "debug": "1",
    }
    if force_task_type and task_type:
        fields["task_type"] = task_type

    files = {}
    if task_type in FILE_TASKS:
        files["image"] = image_path

    started = time.perf_counter()
    try:
        response = post_agent(agent_url, fields, files, timeout)
        predicted_node = selected_node_from_response(response)
        error = ""
    except Exception as exc:
        response = {}
        predicted_node = None
        error = str(exc)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    expected_node = case.get("ground_truth", {}).get("node_id")
    passed = predicted_node == expected_node
    return {
        "case_id": case.get("case_id"),
        "task_type": task_type,
        "status": "PASS" if passed else "FAIL",
        "reason": "" if passed else (error or "selected_node_mismatch"),
        "expected_node": expected_node,
        "predicted_node": predicted_node,
        "expected_ete_ms": case.get("ground_truth", {}).get("ete_time"),
        "elapsed_ms": elapsed_ms,
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
        "elapsed_ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row["status"] == "PASS")
    by_task: dict[str, dict[str, int]] = {}
    for row in rows:
        item = by_task.setdefault(row["task_type"], {"total": 0, "passed": 0})
        item["total"] += 1
        if row["status"] == "PASS":
            item["passed"] += 1
    return {
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0,
        "by_task": {
            task: {
                **item,
                "accuracy": round(item["passed"] / item["total"], 4) if item["total"] else 0,
            }
            for task, item in sorted(by_task.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate real agent node selection with EdgeAgentBench mock MCP.")
    parser.add_argument("--agent-url", default="http://127.0.0.1:8082/v1/agent/chat")
    parser.add_argument("--case-dir", default=str(CASE_DIR))
    parser.add_argument("--case-pattern", default="*.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--current-case-file", default=str(DEFAULT_CURRENT_CASE_FILE))
    parser.add_argument("--image", default=str(DEFAULT_IMAGE_PATH))
    parser.add_argument("--output", default=str(BENCH_ROOT / "results" / "agent_selection_results.csv"))
    parser.add_argument("--summary-output")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--failures", type=int, default=10)
    parser.add_argument("--no-force-task-type", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        raise RuntimeError(f"image file does not exist: {image_path}")

    cases = load_cases(Path(args.case_dir), args.case_pattern, args.limit)
    rows = [
        evaluate_case(
            case,
            args.agent_url,
            Path(args.current_case_file),
            image_path,
            force_task_type=not args.no_force_task_type,
            timeout=args.timeout,
        )
        for case in cases
    ]

    output_path = Path(args.output)
    summary_output_path = Path(args.summary_output) if args.summary_output else output_path.with_suffix(".summary.json")
    summary_payload = {
        "agent_url": args.agent_url,
        "case_count": len(cases),
        "force_task_type": not args.no_force_task_type,
        "current_case_file": args.current_case_file,
        "output": args.output,
        "summary_output": str(summary_output_path),
        **summarize(rows),
    }
    write_csv(output_path, rows)
    summary_output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_output_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(summary_payload, ensure_ascii=False, indent=2))
    failures = [row for row in rows if row["status"] != "PASS"]
    for row in failures[:args.failures]:
        print(
            f"FAIL {row['case_id']}: expected={row['expected_node']} "
            f"predicted={row['predicted_node']} task={row['task_type']} reason={row['reason']}"
        )
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
