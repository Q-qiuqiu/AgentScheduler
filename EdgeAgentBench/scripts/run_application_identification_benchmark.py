#!/usr/bin/env python3
import argparse
import csv
import json
import mimetypes
import sys
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

TASK_TO_APPLICATION = {
    "object_detection": "YoloV5",
    "image_classification": "MobileNet",
    "semantic_segmentation": "deeplabv3",
    "text_classification": "Bert",
}

APPLICATION_ALIASES = {
    "object_detection": "YoloV5",
    "yolo": "YoloV5",
    "yolov5": "YoloV5",
    "YoloV5": "YoloV5",
    "image_classification": "MobileNet",
    "mobilenet": "MobileNet",
    "MobileNet": "MobileNet",
    "semantic_segmentation": "deeplabv3",
    "deeplabv3": "deeplabv3",
    "DeepLabV3": "deeplabv3",
    "text_classification": "Bert",
    "bert": "Bert",
    "Bert": "Bert",
    "BERT": "Bert",
}

CSV_FIELDS = [
    "case_id",
    "query_id",
    "expected_task",
    "expected_application",
    "actual_application",
    "status",
    "reason",
    "actual_tools",
    "latency_sec",
    "message_preview",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cases(case_dir: Path, pattern: str, limit: int | None) -> list[dict[str, Any]]:
    paths = sorted(path for path in case_dir.glob(pattern) if path.name != "manifest.json")
    cases = []
    for path in paths:
        case = load_json(path)
        expected_application = case.get("expected_application")
        if expected_application is None:
            expected_application = (case.get("ground_truth") or {}).get("expected_application")
        if expected_application is None:
            continue
        case["_case_file"] = str(path)
        cases.append(case)
        if limit is not None and len(cases) >= limit:
            break
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


def normalize_application(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return APPLICATION_ALIASES.get(text) or APPLICATION_ALIASES.get(text.lower()) or text


def normalize_tools(trace: list[dict[str, Any]]) -> list[str]:
    return [item.get("action") for item in trace if item.get("action") and item.get("action") != "final_response"]


def extract_application(trace: list[dict[str, Any]]) -> tuple[str | None, str]:
    for action_name in ("schedule_task", "run_task_on_node", "get_task_overhead"):
        for item in trace:
            if item.get("action") != action_name:
                continue
            arguments = item.get("arguments") or {}
            application = normalize_application(arguments.get("task_type"))
            if application:
                return application, action_name
    return None, "missing_task_type"


def evaluate_case(agent_url: str, case: dict[str, Any], image_path: Path, timeout: int) -> dict[str, Any]:
    expected_application = normalize_application(
        case.get("expected_application") or (case.get("ground_truth") or {}).get("expected_application")
    )
    started = time.time()
    try:
        response = post_case(agent_url, case, image_path, timeout)
        trace = response.get("trace", [])
        actual_tools = normalize_tools(trace)
        actual_application, source = extract_application(trace)
        ok = expected_application == actual_application
        reason = "" if ok else f"application_mismatch_from_{source}"
        message = response.get("message", "")
    except urllib.error.HTTPError as exc:
        actual_tools = []
        actual_application = None
        ok = False
        reason = f"http_{exc.code}:{exc.read().decode('utf-8', errors='replace')[:200]}"
        message = ""
    except Exception as exc:
        actual_tools = []
        actual_application = None
        ok = False
        reason = f"error:{exc}"
        message = ""

    return {
        "case_id": case["case_id"],
        "query_id": case["query_id"],
        "expected_task": case["expected_task"],
        "expected_application": expected_application or "",
        "actual_application": actual_application or "",
        "status": "PASS" if ok else "FAIL",
        "reason": reason,
        "actual_tools": "->".join(actual_tools),
        "latency_sec": round(time.time() - started, 3),
        "message_preview": message[:120].replace("\n", "\\n"),
    }


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
    by_application: dict[str, dict[str, int]] = {}
    for row in rows:
        task_item = by_task.setdefault(row["expected_task"], {"total": 0, "passed": 0})
        app_item = by_application.setdefault(row["expected_application"], {"total": 0, "passed": 0})
        task_item["total"] += 1
        app_item["total"] += 1
        if row["status"] == "PASS":
            task_item["passed"] += 1
            app_item["passed"] += 1
    return {
        "total": total,
        "passed": passed,
        "application_identification_accuracy": round(passed / total, 4) if total else 0,
        "by_task": {
            key: {**item, "accuracy": round(item["passed"] / item["total"], 4) if item["total"] else 0}
            for key, item in sorted(by_task.items())
        },
        "by_application": {
            key: {**item, "accuracy": round(item["passed"] / item["total"], 4) if item["total"] else 0}
            for key, item in sorted(by_application.items())
        },
    }


def default_summary_path(output_path: Path) -> Path:
    return output_path.with_suffix(".summary.json")


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    parser = argparse.ArgumentParser(
        description="Evaluate whether the agent identifies the expected executable application from natural language."
    )
    parser.add_argument("--agent-url", default="http://127.0.0.1:8082/v1/agent/chat")
    parser.add_argument("--case-dir", default=str(CASE_DIR))
    parser.add_argument("--case-pattern", default="state_*.json")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE))
    parser.add_argument("--output", default=str(BENCH_ROOT / "results" / "application_identification_results.csv"))
    parser.add_argument("--summary-output")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--failures", type=int, default=10)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    cases = load_cases(Path(args.case_dir), args.case_pattern, args.limit)
    output_path = Path(args.output)
    summary_path = Path(args.summary_output) if args.summary_output else default_summary_path(output_path)
    init_incremental_csv(output_path)

    rows = []
    started = time.time()
    passed = 0
    for index, case in enumerate(cases, start=1):
        row = evaluate_case(args.agent_url, case, Path(args.image), args.timeout)
        rows.append(row)
        append_csv_row(output_path, row)
        if row["status"] == "PASS":
            passed += 1
        if args.progress_every and (index == 1 or index == len(cases) or index % args.progress_every == 0):
            print_progress(index, len(cases), passed, started)
    if args.progress_every:
        sys.stderr.write("\n")

    summary = {
        "case_count": len(cases),
        "output": str(output_path),
        "summary_output": str(summary_path),
        **summarize(rows),
    }
    write_summary(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    failures = [row for row in rows if row["status"] != "PASS"]
    for row in failures[:args.failures]:
        print(
            f"FAIL {row['case_id']}: expected={row['expected_application']} "
            f"actual={row['actual_application']} tools={row['actual_tools']} reason={row['reason']}"
        )
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
