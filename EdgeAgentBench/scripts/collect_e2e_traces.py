#!/usr/bin/env python3
import argparse
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
QUERY_ROOT = BENCH_ROOT / "queries"
DEFAULT_IMAGE = BENCH_ROOT / "samples" / "images" / "dog1.jpg"
DEFAULT_OUTPUT = BENCH_ROOT / "results" / "e2e_traces.jsonl"

TASKS = [
    "object_detection",
    "semantic_segmentation",
    "image_classification",
    "text_classification",
]
IMAGE_TASKS = {"object_detection", "semantic_segmentation", "image_classification"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_queries(query_root: Path, tasks: list[str]) -> dict[str, list[dict[str, Any]]]:
    selected = {task: [] for task in tasks}
    for path in sorted(query_root.glob("*/*.json")):
        query = load_json(path)
        expected_task = query.get("expected_task")
        if expected_task not in selected:
            continue
        query["_query_file"] = str(path.relative_to(BENCH_ROOT))
        selected[expected_task].append(query)
    missing = [task for task, queries in selected.items() if not queries]
    if missing:
        raise RuntimeError(f"missing queries for tasks: {', '.join(missing)}")
    return selected


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


def post_agent(agent_url: str, query: dict[str, Any], image_path: Path, timeout: int, timing: bool) -> dict[str, Any]:
    fields = {"text": query["query"], "debug": "1"}
    if timing:
        fields["timing"] = "1"
    files = {"image": image_path} if query["expected_task"] in IMAGE_TASKS else {}
    body, content_type = encode_multipart(fields, files)
    request = urllib.request.Request(agent_url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(body)))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return {
            "http_status": response.status,
            "response": json.loads(response.read().decode("utf-8")),
        }


def summarize_trace(response_payload: dict[str, Any]) -> dict[str, Any]:
    response = response_payload.get("response") or {}
    trace = response.get("trace") or []
    usage = response.get("usage") or {}
    tool_elapsed: dict[str, float] = {}
    model_elapsed: dict[str, float] = {}

    for item in trace:
        model_timing = item.get("model_timing")
        if isinstance(model_timing, dict):
            segment = model_timing.get("segment", "model_inference")
            model_elapsed[segment] = model_elapsed.get(segment, 0.0) + float(model_timing.get("elapsed_ms", 0) or 0)
        final_model_timing = item.get("final_model_timing")
        if isinstance(final_model_timing, dict):
            segment = final_model_timing.get("segment", "model_inference.final")
            model_elapsed[segment] = model_elapsed.get(segment, 0.0) + float(final_model_timing.get("elapsed_ms", 0) or 0)
        tool_timing = item.get("tool_timing")
        if isinstance(tool_timing, dict):
            segment = tool_timing.get("segment", f"tool.{item.get('action', 'unknown')}")
            tool_elapsed[segment] = tool_elapsed.get(segment, 0.0) + float(tool_timing.get("elapsed_ms", 0) or 0)

    e2e_timing = usage.get("timing") if isinstance(usage, dict) else None
    return {
        "agent_e2e_ms": e2e_timing.get("elapsed_ms") if isinstance(e2e_timing, dict) else None,
        "model_elapsed_ms": {key: round(value, 3) for key, value in sorted(model_elapsed.items())},
        "tool_elapsed_ms": {key: round(value, 3) for key, value in sorted(tool_elapsed.items())},
        "trace_actions": [item.get("action") for item in trace if item.get("action")],
    }


def write_jsonl_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file_obj:
        file_obj.write(json.dumps(row, ensure_ascii=False) + "\n")
        file_obj.flush()


def print_progress(done: int, total: int, passed: int, started: float) -> None:
    width = 30
    ratio = done / total if total else 1
    filled = int(width * ratio)
    elapsed = time.time() - started
    rate = done / elapsed if elapsed > 0 else 0
    bar = "#" * filled + "-" * (width - filled)
    sys.stderr.write(
        f"\r[{bar}] {done}/{total} "
        f"ok={passed} fail={done - passed} elapsed={elapsed:.1f}s rate={rate:.3f}/s"
    )
    sys.stderr.flush()


def collect(args: argparse.Namespace) -> int:
    tasks = args.tasks.split(",") if args.tasks else TASKS
    queries_by_task = load_queries(Path(args.query_root), tasks)
    image_path = Path(args.image)
    if any(task in IMAGE_TASKS for task in tasks) and not image_path.exists():
        raise RuntimeError(f"image file not found: {image_path}")

    output_path = Path(args.output)
    if output_path.exists() and not args.append:
        output_path.unlink()

    total = len(tasks) * args.runs_per_task
    done = 0
    passed = 0
    started = time.time()

    for task in tasks:
        queries = queries_by_task[task]
        for run_index in range(1, args.runs_per_task + 1):
            query = queries[(run_index - 1) % len(queries)]
            row = {
                "benchmark": "EdgeAgentBench",
                "experiment": "e2e_trace_collection",
                "task": task,
                "run_index": run_index,
                "query_id": query.get("query_id"),
                "query": query.get("query"),
                "query_file": query.get("_query_file"),
                "agent_url": args.agent_url,
                "status": "pending",
            }
            request_started = time.time()
            try:
                payload = post_agent(args.agent_url, query, image_path, args.timeout, args.timing)
                row.update({
                    "status": "success",
                    "http_status": payload.get("http_status"),
                    "request_elapsed_ms": round((time.time() - request_started) * 1000, 3),
                    "summary": summarize_trace(payload),
                    "response": payload.get("response"),
                })
                passed += 1
            except urllib.error.HTTPError as exc:
                row.update({
                    "status": "http_error",
                    "http_status": exc.code,
                    "request_elapsed_ms": round((time.time() - request_started) * 1000, 3),
                    "error": exc.read().decode("utf-8", errors="replace"),
                })
            except Exception as exc:
                row.update({
                    "status": "error",
                    "request_elapsed_ms": round((time.time() - request_started) * 1000, 3),
                    "error": str(exc),
                })

            write_jsonl_row(output_path, row)
            done += 1
            if args.progress_every and (done == 1 or done == total or done % args.progress_every == 0):
                print_progress(done, total, passed, started)
            if args.sleep_sec > 0:
                time.sleep(args.sleep_sec)

    if args.progress_every:
        sys.stderr.write("\n")
    print(json.dumps({
        "output": str(output_path),
        "total": total,
        "success": passed,
        "failed": total - passed,
        "runs_per_task": args.runs_per_task,
        "tasks": tasks,
    }, ensure_ascii=False, indent=2))
    return 0 if passed == total or not args.strict else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect real end-to-end agent traces from EdgeAgentBench queries.")
    parser.add_argument("--agent-url", default="http://127.0.0.1:8082/v1/agent/chat")
    parser.add_argument("--query-root", default=str(QUERY_ROOT))
    parser.add_argument("--image", default=str(DEFAULT_IMAGE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--runs-per-task", type=int, default=20)
    parser.add_argument("--tasks", default=",".join(TASKS), help="Comma-separated expected_task list")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--sleep-sec", type=float, default=0)
    parser.add_argument("--progress-every", type=int, default=1)
    parser.add_argument("--timing", action="store_true", help="Also ask the agent server to print [TIMING] logs")
    parser.add_argument("--append", action="store_true", help="Append to an existing JSONL output instead of replacing it")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 if any request fails")
    args = parser.parse_args()
    return collect(args)


if __name__ == "__main__":
    raise SystemExit(main())
