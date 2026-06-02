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
DEFAULT_IMAGE_PATH = BENCH_ROOT / "samples" / "images" / "dog1.jpg"
DEFAULT_CASE_SERVER_URL = "http://127.0.0.1:18082"

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


def post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Content-Length", str(len(body)))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"case server returned HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach case server at {url}: {exc.reason}") from exc


def set_current_case(case_server_url: str, case_id: str, timeout: int) -> None:
    url = f"{case_server_url.rstrip('/')}/current"
    result = post_json(url, {"case_id": case_id}, timeout)
    if result.get("status") != "success":
        raise RuntimeError(f"mock case server failed to set case {case_id}: {result}")


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


def timing_elapsed_ms(payload: Any) -> float | None:
    if not isinstance(payload, dict):
        return None
    elapsed_ms = payload.get("elapsed_ms")
    return float(elapsed_ms) if isinstance(elapsed_ms, (int, float)) else None


def response_timings(response: dict[str, Any]) -> dict[str, float | None]:
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    trace = response.get("trace", []) if isinstance(response, dict) else []
    result: dict[str, float | None] = {
        "agent_e2e_ms": timing_elapsed_ms(usage.get("timing") if isinstance(usage, dict) else None),
        "task_llm_ms": None,
        "dispatch_llm_ms": None,
        "llm_total_ms": None,
        "snapshot_tool_ms": None,
        "run_tool_ms": None,
    }
    llm_values: list[float] = []
    if isinstance(trace, list):
        for item in trace:
            if not isinstance(item, dict):
                continue
            action = item.get("action")
            model_ms = timing_elapsed_ms(item.get("model_timing"))
            tool_ms = timing_elapsed_ms(item.get("tool_timing"))
            if model_ms is not None:
                llm_values.append(model_ms)
                if action == "select_task_type":
                    result["task_llm_ms"] = model_ms
                elif action == "run_task_on_node":
                    result["dispatch_llm_ms"] = model_ms
            if tool_ms is not None:
                if action == "get_dispatch_snapshot":
                    result["snapshot_tool_ms"] = tool_ms
                elif action == "run_task_on_node":
                    result["run_tool_ms"] = tool_ms
    if llm_values:
        result["llm_total_ms"] = round(sum(llm_values), 3)
    return result


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
    agent_url: str,
    case_server_url: str,
    image_path: Path,
    force_task_type: bool,
    timeout: int,
) -> dict[str, Any]:
    set_current_case(case_server_url, case["case_id"], timeout)
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
        timings = response_timings(response)
        error = ""
    except Exception as exc:
        response = {}
        predicted_node = None
        timings = response_timings(response)
        error = str(exc)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

    expected_node = case.get("ground_truth", {}).get("node_id")
    expected_ete_ms = case.get("ground_truth", {}).get("ete_time")
    predicted_ete_ms = node_ete_ms(case, predicted_node)
    latency_regret_ms = None
    latency_ratio = None
    if isinstance(expected_ete_ms, (int, float)) and predicted_ete_ms is not None:
        latency_regret_ms = round(predicted_ete_ms - expected_ete_ms, 3)
        latency_ratio = round(predicted_ete_ms / expected_ete_ms, 4) if expected_ete_ms > 0 else None
    passed = predicted_node == expected_node
    return {
        "case_id": case.get("case_id"),
        "task_type": task_type,
        "status": "PASS" if passed else "FAIL",
        "reason": "" if passed else (error or "selected_node_mismatch"),
        "expected_node": expected_node,
        "predicted_node": predicted_node,
        "expected_ete_ms": expected_ete_ms,
        "predicted_ete_ms": predicted_ete_ms,
        "latency_regret_ms": latency_regret_ms,
        "latency_ratio": latency_ratio,
        "elapsed_ms": elapsed_ms,
        **timings,
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
        "elapsed_ms",
        "agent_e2e_ms",
        "llm_total_ms",
        "task_llm_ms",
        "dispatch_llm_ms",
        "snapshot_tool_ms",
        "run_tool_ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


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
    elapsed_rows = [row for row in rows if isinstance(row.get("elapsed_ms"), (int, float))]
    agent_e2e_rows = [row for row in rows if isinstance(row.get("agent_e2e_ms"), (int, float))]
    llm_rows = [row for row in rows if isinstance(row.get("llm_total_ms"), (int, float))]
    dispatch_llm_rows = [row for row in rows if isinstance(row.get("dispatch_llm_ms"), (int, float))]
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
        "average_request_elapsed_ms": round(
            sum(row["elapsed_ms"] for row in elapsed_rows) / len(elapsed_rows), 3
        ) if elapsed_rows else None,
        "average_agent_e2e_ms": round(
            sum(row["agent_e2e_ms"] for row in agent_e2e_rows) / len(agent_e2e_rows), 3
        ) if agent_e2e_rows else None,
        "average_llm_total_ms": round(
            sum(row["llm_total_ms"] for row in llm_rows) / len(llm_rows), 3
        ) if llm_rows else None,
        "average_dispatch_llm_ms": round(
            sum(row["dispatch_llm_ms"] for row in dispatch_llm_rows) / len(dispatch_llm_rows), 3
        ) if dispatch_llm_rows else None,
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


def progress_line(done: int, total: int, started: float) -> str:
    width = 28
    ratio = done / total if total else 1
    filled = min(width, int(width * ratio))
    bar = "#" * filled + "-" * (width - filled)
    elapsed = time.perf_counter() - started
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0
    return f"[{bar}] {done}/{total} {ratio * 100:5.1f}% elapsed={elapsed:.1f}s eta={eta:.1f}s"


def log_progress(rows: list[dict[str, Any]], total: int, started: float) -> None:
    done = len(rows)
    if not rows:
        message = progress_line(done, total, started)
    else:
        row = rows[-1]
        passed = sum(1 for item in rows if item["status"] == "PASS")
        avg_elapsed = sum(item["elapsed_ms"] for item in rows) / done
        llm_values = [item["llm_total_ms"] for item in rows if isinstance(item.get("llm_total_ms"), (int, float))]
        avg_llm = sum(llm_values) / len(llm_values) if llm_values else 0
        message = (
            f"{progress_line(done, total, started)} "
            f"acc={passed / done:.4f} avg_req={avg_elapsed:.1f}ms avg_llm={avg_llm:.1f}ms "
            f"last={row['case_id']} pred={row['predicted_node']} "
            f"llm={row.get('llm_total_ms')}ms dispatch={row.get('dispatch_llm_ms')}ms"
        )
    print(message, file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate real agent node selection with EdgeAgentBench mock MCP.")
    parser.add_argument("--agent-url", default="http://127.0.0.1:8082/v1/agent/chat")
    parser.add_argument("--case-dir", default=str(CASE_DIR))
    parser.add_argument("--case-pattern", default="*.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--case-server-url", default=DEFAULT_CASE_SERVER_URL)
    parser.add_argument("--image", default=str(DEFAULT_IMAGE_PATH))
    parser.add_argument("--output", default=str(BENCH_ROOT / "results" / "agent_selection_results.csv"))
    parser.add_argument("--summary-output")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--failures", type=int, default=10)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--no-force-task-type", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    image_path = Path(args.image)
    if not image_path.exists():
        raise RuntimeError(f"image file does not exist: {image_path}")

    cases = load_cases(Path(args.case_dir), args.case_pattern, args.limit)
    rows = []
    started = time.perf_counter()
    for index, case in enumerate(cases, start=1):
        row = evaluate_case(
            case,
            args.agent_url,
            args.case_server_url,
            image_path,
            force_task_type=not args.no_force_task_type,
            timeout=args.timeout,
        )
        rows.append(row)
        if not args.no_progress:
            should_log = index == len(cases) or (
                args.progress_every > 0 and index % args.progress_every == 0
            )
            if should_log:
                log_progress(rows, len(cases), started)

    output_path = Path(args.output)
    summary_output_path = Path(args.summary_output) if args.summary_output else output_path.with_suffix(".summary.json")
    summary_payload = {
        "agent_url": args.agent_url,
        "case_count": len(cases),
        "force_task_type": not args.no_force_task_type,
        "case_server_url": args.case_server_url,
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
