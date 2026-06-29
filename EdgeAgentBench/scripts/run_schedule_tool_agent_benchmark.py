#!/usr/bin/env python3
import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BENCH_ROOT.parent
CASE_DIR = BENCH_ROOT / "cases"
DEFAULT_CASE_SERVER_URL = "http://127.0.0.1:18082"
DEFAULT_LLM_API_URL = "http://127.0.0.1:8081/v1/chat/completions"
DEFAULT_MCP_SERVER_PATH = BENCH_ROOT / "scripts" / "mock_mcp_server.py"

POLICIES = {
    "ground_truth",
    "scheduler",
    "weight_schedule",
    "resource_average",
    "cpu_min",
    "npu_min",
    "mem_min",
    "container_first",
}


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
        raise RuntimeError(f"HTTP {exc.code} for {url}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach {url}: {exc.reason}") from exc


def set_current_case(case_server_url: str, case_id: str, timeout: int) -> None:
    url = f"{case_server_url.rstrip('/')}/current"
    result = post_json(url, {"case_id": case_id}, timeout)
    if result.get("status") != "success":
        raise RuntimeError(f"mock case server failed to set case {case_id}: {result}")


def call_llm(llm_url: str, model: str, messages: list[dict[str, str]], timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    response = post_json(
        llm_url,
        {
            "model": model,
            "messages": messages,
            "stream": False,
        },
        timeout,
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    choices = response.get("choices", [])
    if not choices:
        raise RuntimeError(f"LLM returned no choices: {response}")
    content = choices[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("LLM returned empty content")
    usage = response.get("usage", {}) if isinstance(response.get("usage"), dict) else {}
    return {
        "content": content,
        "elapsed_ms": elapsed_ms,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            raise
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == "\"":
                    in_string = False
                continue
            if char == "\"":
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    payload = json.loads(cleaned[start:index + 1])
                    break
        else:
            raise
    if not isinstance(payload, dict):
        raise RuntimeError("LLM JSON output must be an object")
    return payload


def call_mcp_tool(mcp_server_path: Path, tool_name: str, arguments: dict[str, Any], timeout: int) -> dict[str, Any]:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    ]
    process = subprocess.Popen(
        [sys.executable, str(mcp_server_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    request_payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)
    try:
        stdout, stderr = process.communicate(request_payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("MCP request timed out")
    if process.returncode != 0:
        raise RuntimeError(f"MCP server exited with {process.returncode}: {stderr.strip()}")
    for line in stdout.splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("id") != 2:
            continue
        result = item.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            message = content[0].get("text") if content and isinstance(content[0], dict) else "MCP tool returned an error"
            raise RuntimeError(message)
        content = result.get("content", [])
        if not content or not isinstance(content[0], dict):
            raise RuntimeError("MCP tool returned empty content")
        return json.loads(content[0].get("text", "{}"))
    raise RuntimeError("MCP tool response not found")


def build_messages(case: dict[str, Any], task_type: str, policy: str) -> list[dict[str, str]]:
    tool_description = {
        "name": "schedule_on_node",
        "description": "Select a node using the given baseline policy, then execute the task on that node.",
        "arguments": {
            "task_type": "AI task type, such as YoloV5, MobileNet, ResNet50, Bert, deeplabv3",
            "policy": sorted(POLICIES),
        },
        "return": {
            "target_global_id": "selected node id",
            "e2e_ms": "latency of the selected node in benchmark mock mode",
        },
    }
    user_payload = {
        "fixed_task_type": task_type,
        "fixed_policy": policy,
        "user_query": case.get("query", ""),
        "tool": tool_description,
    }
    return [
        {
            "role": "system",
            "content": (
                "You are a tool-argument planner. Return JSON only.\n"
                "Use the fixed task_type and fixed policy exactly.\n"
                "Return exactly: {\"tool\":\"schedule_on_node\",\"arguments\":{\"task_type\":\"<fixed_task_type>\",\"policy\":\"<fixed_policy>\"}}."
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def normalize_tool_arguments(selection: dict[str, Any]) -> dict[str, Any]:
    if selection.get("tool") not in {None, "schedule_on_node"}:
        raise RuntimeError(f"LLM selected unsupported tool: {selection.get('tool')}")
    arguments = selection.get("arguments", selection)
    if not isinstance(arguments, dict):
        raise RuntimeError("LLM output arguments must be an object")
    return {
        "task_type": arguments.get("task_type"),
        "policy": arguments.get("policy"),
    }


def node_ete_values(case: dict[str, Any]) -> list[float]:
    values = []
    for node in case.get("nodes", []):
        if not node.get("online", False):
            continue
        e2e_ms = node.get("latency_profile", {}).get("e2e_ms")
        if isinstance(e2e_ms, (int, float)) and e2e_ms >= 0:
            values.append(float(e2e_ms))
    return values


def evaluate_case(
    case: dict[str, Any],
    llm_url: str,
    llm_model: str,
    mcp_server_path: Path,
    case_server_url: str,
    fixed_policy: str,
    force_task_type: bool,
    timeout: int,
) -> dict[str, Any]:
    set_current_case(case_server_url, case["case_id"], timeout)
    task_type = case.get("task_type") if force_task_type else ""
    expected_node = case.get("ground_truth", {}).get("node_id")
    expected_ete_ms = case.get("ground_truth", {}).get("ete_time")
    started = time.perf_counter()
    llm_ms = None
    tool_ms = None
    predicted_node = None
    predicted_ete_ms = None
    tool_result: dict[str, Any] = {}
    error = ""
    try:
        llm_result = call_llm(
            llm_url,
            llm_model,
            build_messages(case, task_type, fixed_policy),
            timeout,
        )
        llm_ms = llm_result["elapsed_ms"]
        arguments = normalize_tool_arguments(parse_json_object(llm_result["content"]))
        if arguments.get("task_type") != task_type:
            raise RuntimeError(f"LLM returned task_type {arguments.get('task_type')}, expected {task_type}")
        if arguments.get("policy") != fixed_policy:
            raise RuntimeError(f"LLM returned policy {arguments.get('policy')}, expected {fixed_policy}")
        tool_started = time.perf_counter()
        tool_result = call_mcp_tool(mcp_server_path, "schedule_on_node", arguments, timeout)
        tool_ms = round((time.perf_counter() - tool_started) * 1000, 3)
        predicted_node = tool_result.get("target_global_id") or tool_result.get("scheduled_target_global_id")
        predicted_ete_ms = tool_result.get("e2e_ms") or tool_result.get("ete_ms")
        if not isinstance(predicted_ete_ms, (int, float)):
            predicted_ete_ms = None
    except Exception as exc:
        error = str(exc)
        values = node_ete_values(case)
        predicted_ete_ms = max(values) if values else None
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    latency_regret_ms = None
    latency_ratio = None
    if isinstance(expected_ete_ms, (int, float)) and isinstance(predicted_ete_ms, (int, float)):
        latency_regret_ms = round(float(predicted_ete_ms) - float(expected_ete_ms), 3)
        latency_ratio = round(float(predicted_ete_ms) / float(expected_ete_ms), 4) if expected_ete_ms > 0 else None
    passed = predicted_node == expected_node
    return {
        "case_id": case.get("case_id"),
        "task_type": case.get("task_type"),
        "policy": fixed_policy,
        "status": "PASS" if passed else "FAIL",
        "reason": "" if passed else (error or "selected_node_mismatch"),
        "expected_node": expected_node,
        "predicted_node": predicted_node,
        "expected_ete_ms": expected_ete_ms,
        "predicted_ete_ms": predicted_ete_ms,
        "latency_regret_ms": latency_regret_ms,
        "latency_ratio": latency_ratio,
        "elapsed_ms": elapsed_ms,
        "llm_total_ms": llm_ms,
        "tool_ms": tool_ms,
        "schedule_score": tool_result.get("schedule_score") if isinstance(tool_result, dict) else None,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "case_id",
        "task_type",
        "policy",
        "status",
        "reason",
        "expected_node",
        "predicted_node",
        "expected_ete_ms",
        "predicted_ete_ms",
        "latency_regret_ms",
        "latency_ratio",
        "elapsed_ms",
        "llm_total_ms",
        "tool_ms",
        "schedule_score",
    ]
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def average(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row.get(key) for row in rows if isinstance(row.get(key), (int, float))]
    return round(sum(values) / len(values), 3) if values else None


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row.get("status") == "PASS")
    latency_rows = [row for row in rows if isinstance(row.get("predicted_ete_ms"), (int, float))]
    by_task: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = by_task.setdefault(row["task_type"], {"total": 0, "passed": 0, "rows": []})
        item["total"] += 1
        if row.get("status") == "PASS":
            item["passed"] += 1
        if isinstance(row.get("predicted_ete_ms"), (int, float)):
            item["rows"].append(row)
    return {
        "total": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0,
        "latency_sample_count": len(latency_rows),
        "average_expected_ete_ms": average(rows, "expected_ete_ms"),
        "average_predicted_ete_ms": average(latency_rows, "predicted_ete_ms"),
        "average_latency_regret_ms": average(latency_rows, "latency_regret_ms"),
        "average_request_elapsed_ms": average(rows, "elapsed_ms"),
        "average_llm_total_ms": average(rows, "llm_total_ms"),
        "average_tool_ms": average(rows, "tool_ms"),
        "by_task": {
            task: {
                "total": item["total"],
                "passed": item["passed"],
                "accuracy": round(item["passed"] / item["total"], 4) if item["total"] else 0,
                "latency_sample_count": len(item["rows"]),
                "average_expected_ete_ms": average(
                    [row for row in rows if row["task_type"] == task],
                    "expected_ete_ms",
                ),
                "average_predicted_ete_ms": average(item["rows"], "predicted_ete_ms"),
                "average_latency_regret_ms": average(item["rows"], "latency_regret_ms"),
            }
            for task, item in sorted(by_task.items())
        },
    }


def progress_line(done: int, total: int, started: float) -> str:
    elapsed = time.perf_counter() - started
    rate = done / elapsed if elapsed > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0
    return f"{done}/{total} {done / total * 100:5.1f}% elapsed={elapsed:.1f}s eta={eta:.1f}s"


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate LLM-planned schedule_on_node tool calls with EdgeAgentBench mock MCP.")
    parser.add_argument("--llm-url", default=os.environ.get("LLM_API_URL", DEFAULT_LLM_API_URL))
    parser.add_argument("--llm-model", default=os.environ.get("LLM_MODEL_NAME", "rkllm-default"))
    parser.add_argument("--mcp-server-path", default=str(DEFAULT_MCP_SERVER_PATH))
    parser.add_argument("--case-server-url", default=DEFAULT_CASE_SERVER_URL)
    parser.add_argument("--case-dir", default=str(CASE_DIR))
    parser.add_argument("--case-pattern", default="*.json")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--policy", default="scheduler", choices=sorted(POLICIES))
    parser.add_argument("--no-force-task-type", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--output", default=str(BENCH_ROOT / "results" / "schedule_tool_agent_results.csv"))
    parser.add_argument("--summary-output")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    cases = load_cases(Path(args.case_dir), args.case_pattern, args.limit)
    rows = []
    started = time.perf_counter()
    for index, case in enumerate(cases, start=1):
        row = evaluate_case(
            case,
            args.llm_url,
            args.llm_model,
            Path(args.mcp_server_path),
            args.case_server_url,
            args.policy,
            force_task_type=not args.no_force_task_type,
            timeout=args.timeout,
        )
        rows.append(row)
        if args.progress_every > 0 and (index % args.progress_every == 0 or index == len(cases)):
            passed = sum(1 for item in rows if item.get("status") == "PASS")
            print(
                f"{progress_line(index, len(cases), started)} acc={passed / index:.4f} "
                f"last={row['case_id']} pred={row['predicted_node']} llm={row['llm_total_ms']}ms",
                file=sys.stderr,
                flush=True,
            )

    output_path = Path(args.output)
    summary_output_path = Path(args.summary_output) if args.summary_output else output_path.with_suffix(".summary.json")
    write_csv(output_path, rows)
    summary_payload = {
        "llm_url": args.llm_url,
        "llm_model": args.llm_model,
        "mcp_server_path": args.mcp_server_path,
        "case_count": len(cases),
        "force_task_type": not args.no_force_task_type,
        "policy": args.policy,
        "output": str(output_path),
        "summary_output": str(summary_output_path),
        **summarize(rows),
    }
    summary_output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_output_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary_payload, ensure_ascii=False, indent=2))
    failures = [row for row in rows if row.get("status") != "PASS"]
    return 1 if args.strict and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
