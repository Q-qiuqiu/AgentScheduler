#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BENCH_ROOT.parent
DEFAULT_CASE_SERVER_URL = "http://127.0.0.1:18082"
AGENT_INFO_PATH = REPO_ROOT / "config_files" / "agent_info.json"
STATIC_INFO_PATH = REPO_ROOT / "config_files" / "static_info.json"
SCHEDULER_WEIGHTS = {
    "base_latency_ms": 0.0003,
    "cpu_after": 0.01,
    "mem_after": 2.0,
    "npu_after": 0.8,
}
WEIGHT_SCHEDULE_WEIGHTS = {
    "cpu_after": 5.0,
    "npu_after": 17.0,
    "mem_after": 39.0,
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def case_server_url() -> str:
    return os.environ.get("EDGE_BENCH_CASE_SERVER_URL", DEFAULT_CASE_SERVER_URL).rstrip("/")


def load_current_case() -> dict[str, Any]:
    url = f"{case_server_url()}/current"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"mock case server returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach mock case server at {url}: {exc.reason}") from exc
    if payload.get("status") != "success" or not isinstance(payload.get("case"), dict):
        raise RuntimeError(f"mock case server returned invalid current case: {payload}")
    return payload["case"]


def make_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_response_text(payload: dict[str, Any], request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]},
    )


def tool_response_error(message: str, request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {"content": [{"type": "text", "text": message}], "isError": True},
    )


def handle_initialize(request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "edge-agent-bench-mock-tools", "version": "1.0.0"},
        },
    )


def node_to_cluster_resource(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "global_id": node.get("node_id"),
        "type": node.get("platform"),
        "ip_address": "127.0.0.1",
        "agent_port": 0,
        "online": node.get("online", True),
        "container_running": node.get("container_running", False),
        "resource": {
            "cpu_used": node.get("cpu_used"),
            "mem_used": node.get("mem_used"),
            "npu_used": node.get("npu_used"),
            "net_bandwidth": 1000.0,
            "net_latency": node.get("network_latency", 1),
        },
    }


def get_cluster_resources(_: dict[str, Any] | None = None) -> dict[str, Any]:
    case = load_current_case()
    return {
        "status": "success",
        "result": [node_to_cluster_resource(node) for node in case.get("nodes", [])],
    }


def get_task_catalog(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    agent_info = load_json(AGENT_INFO_PATH)
    static_info = load_json(STATIC_INFO_PATH)
    tools = agent_info.get("agent", {}).get("tools", {})
    available_device_types = set(arguments.get("available_device_types") or [])
    use_filter = "available_device_types" in arguments
    result = []

    for task_type, by_device in static_info.items():
        if not isinstance(by_device, dict):
            continue
        supported_device_types = [
            device_type
            for device_type in sorted(by_device)
            if not use_filter or device_type in available_device_types
        ]
        if not supported_device_types:
            continue
        metadata = tools.get(task_type, {})
        result.append({
            "model_name": task_type,
            "task_type": task_type,
            "tool_kind": metadata.get("tool_kind"),
            "requires_file": metadata.get("requires_file", False),
            "file_field_name": metadata.get("file_field_name"),
            "real_url": metadata.get("real_url", "predict"),
            "capabilities": metadata.get("capabilities", []),
            "description": metadata.get("description", ""),
            "supported_device_types": supported_device_types,
        })
    return {"status": "success", "result": result}


def get_dispatch_snapshot(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    cluster_resources = get_cluster_resources(arguments)
    available_device_types = sorted({
        node.get("type")
        for node in cluster_resources.get("result", [])
        if isinstance(node, dict) and node.get("type")
    })
    catalog_args = {"available_device_types": available_device_types} if available_device_types else {}
    return {
        "status": "success",
        "cluster_resources": cluster_resources,
        "task_catalog": get_task_catalog(catalog_args),
    }


def merge_constraints(case_constraints: dict[str, Any] | None, arguments: dict[str, Any]) -> dict[str, Any]:
    case_constraints = case_constraints or {}
    excluded = set(case_constraints.get("exclude_nodes", []))
    excluded.update(arguments.get("exclude_nodes", []) or [])
    require_nodes = set(case_constraints.get("require_nodes", []))
    require_nodes.update(arguments.get("require_nodes", []) or [])
    require_node = arguments.get("require_node") or case_constraints.get("require_node")
    return {
        "exclude_nodes": sorted(excluded),
        "require_node": require_node,
        "require_nodes": sorted(require_nodes),
    }


def is_excluded(node: dict[str, Any], constraints: dict[str, Any]) -> bool:
    excluded = set((constraints or {}).get("exclude_nodes", []))
    return node.get("node_id") in excluded or node.get("platform") in excluded


def is_required(node: dict[str, Any], constraints: dict[str, Any]) -> bool:
    required = set((constraints or {}).get("require_nodes", []))
    require_node = (constraints or {}).get("require_node")
    if require_node:
        required.add(require_node)
    if not required:
        return True
    return node.get("node_id") in required or node.get("platform") in required


def static_base_latency_ms(static_info: dict[str, Any], task_type: str, device_type: str | None) -> float:
    task_entry = static_info.get(task_type, {}) if isinstance(static_info, dict) else {}
    device_entry = task_entry.get(device_type, {}) if isinstance(task_entry, dict) else {}
    overhead = device_entry.get("taskOverhead", {}) if isinstance(device_entry, dict) else {}
    value = overhead.get("base_latency_ms")
    return float(value) if isinstance(value, (int, float)) else 0.0


def schedule_candidates(case: dict[str, Any], arguments: dict[str, Any]) -> list[dict[str, Any]]:
    task_type = arguments.get("task_type")
    if not task_type:
        raise RuntimeError("task_type is required")
    static_info = load_json(STATIC_INFO_PATH)
    supported = set(static_info.get(task_type, {}).keys())
    constraints = merge_constraints(case.get("constraints", {}), arguments)
    candidates = []
    for node in case.get("nodes", []):
        if not node.get("online", False):
            continue
        if node.get("latency_profile", {}).get("model") != task_type:
            continue
        if supported and node.get("platform") not in supported:
            continue
        if is_excluded(node, constraints) or not is_required(node, constraints):
            continue
        e2e_ms = node.get("latency_profile", {}).get("e2e_ms")
        if not isinstance(e2e_ms, (int, float)) or e2e_ms < 0:
            continue
        candidates.append({
            "node": node,
            "detail": {
                "target_global_id": node.get("node_id"),
                "device_type": node.get("platform"),
                "base_latency_ms": round(static_base_latency_ms(static_info, task_type, node.get("platform")), 3),
                "cpu_after": round(float(node.get("cpu_used", 1)), 6),
                "mem_after": round(float(node.get("mem_used", 1)), 6),
                "npu_after": round(float(node.get("npu_used", 1)), 6),
                "container_running": bool(node.get("container_running", False)),
                "cold_start": not bool(node.get("container_running", False)),
                "e2e_ms": e2e_ms,
            },
        })
    return candidates


def select_scheduled_candidate(candidates: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    if not candidates:
        raise RuntimeError("no candidate nodes available for schedule_on_node")
    policy = (policy or "scheduler").strip()
    ranked = []
    for item in candidates:
        node = item["node"]
        detail = item["detail"]
        node_id = node.get("node_id", "")
        avg_load = round((detail["cpu_after"] + detail["mem_after"] + detail["npu_after"]) / 3, 6)
        if policy == "ground_truth":
            score = detail["e2e_ms"]
            sort_key = (score, node_id)
        elif policy == "scheduler":
            score = round(
                SCHEDULER_WEIGHTS["base_latency_ms"] * detail["base_latency_ms"]
                + SCHEDULER_WEIGHTS["cpu_after"] * detail["cpu_after"]
                + SCHEDULER_WEIGHTS["npu_after"] * detail["npu_after"]
                + SCHEDULER_WEIGHTS["mem_after"] * detail["mem_after"],
                6,
            )
            sort_key = (score, node_id)
        elif policy == "resource_average":
            score = avg_load
            sort_key = (score, node_id)
        elif policy == "weight_schedule":
            score = round(
                WEIGHT_SCHEDULE_WEIGHTS["cpu_after"] * detail["cpu_after"]
                + WEIGHT_SCHEDULE_WEIGHTS["npu_after"] * detail["npu_after"]
                + WEIGHT_SCHEDULE_WEIGHTS["mem_after"] * detail["mem_after"],
                6,
            )
            sort_key = (score, node_id)
        elif policy == "cpu_min":
            score = detail["cpu_after"]
            sort_key = (score, node_id)
        elif policy == "npu_min":
            score = detail["npu_after"]
            sort_key = (score, node_id)
        elif policy == "mem_min":
            score = detail["mem_after"]
            sort_key = (score, node_id)
        elif policy == "container_first":
            score = avg_load
            sort_key = (1 if detail["cold_start"] else 0, score, node_id)
        else:
            raise RuntimeError(f"unsupported schedule_on_node policy: {policy}")
        ranked.append((sort_key, score, item))
    ranked.sort(key=lambda item: item[0])
    _, score, selected = ranked[0]
    return {
        "policy": policy,
        "target_global_id": selected["node"].get("node_id"),
        "device_type": selected["node"].get("platform"),
        "score": score,
        "eligible_nodes": [item[2]["node"].get("node_id") for item in ranked],
        "detail": selected["detail"],
    }


def run_task_on_node(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    target_global_id = arguments.get("target_global_id")
    task_type = arguments.get("task_type")
    case = load_current_case()
    node = next((item for item in case.get("nodes", []) if item.get("node_id") == target_global_id), None)
    if not node:
        raise RuntimeError(f"target_global_id is not in current case: {target_global_id}")
    latency_profile = node.get("latency_profile", {})
    return {
        "status": "success",
        "mocked": True,
        "task_type": task_type,
        "target_global_id": target_global_id,
        "device_type": node.get("platform"),
        "case_id": case.get("case_id"),
        "ete_ms": latency_profile.get("e2e_ms"),
        "e2e_ms": latency_profile.get("e2e_ms"),
        "exec_ms": latency_profile.get("exec_ms"),
        "results": [],
    }


def schedule_on_node(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    case = load_current_case()
    selection = select_scheduled_candidate(
        schedule_candidates(case, arguments),
        arguments.get("policy", "scheduler"),
    )
    run_result = run_task_on_node({
        **arguments,
        "target_global_id": selection["target_global_id"],
    })
    return {
        **run_result,
        "schedule_policy": selection["policy"],
        "scheduled_target_global_id": selection["target_global_id"],
        "scheduled_device_type": selection["device_type"],
        "schedule_score": selection["score"],
        "eligible_nodes": selection["eligible_nodes"],
        "schedule_detail": selection["detail"],
    }


def handle_tools_list(request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {
            "tools": [
                {"name": "get_dispatch_snapshot", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "get_cluster_resources", "inputSchema": {"type": "object", "properties": {}}},
                {
                    "name": "get_task_catalog",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "available_device_types": {"type": "array", "items": {"type": "string"}}
                        },
                    },
                },
                {
                    "name": "schedule_on_node",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_type": {"type": "string"},
                            "policy": {
                                "type": "string",
                                "enum": [
                                    "ground_truth",
                                    "scheduler",
                                    "weight_schedule",
                                    "resource_average",
                                    "cpu_min",
                                    "npu_min",
                                    "mem_min",
                                    "container_first",
                                ],
                            },
                            "exclude_nodes": {"type": "array", "items": {"type": "string"}},
                            "require_node": {"type": "string"},
                            "require_nodes": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["task_type", "policy"],
                    },
                },
                {
                    "name": "run_task_on_node",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_type": {"type": "string"},
                            "target_global_id": {"type": "string"},
                        },
                        "required": ["task_type", "target_global_id"],
                    },
                },
            ]
        },
    )


def handle_tools_call(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {})
    try:
        if name == "get_dispatch_snapshot":
            return tool_response_text(get_dispatch_snapshot(arguments), request_id)
        if name == "get_cluster_resources":
            return tool_response_text(get_cluster_resources(arguments), request_id)
        if name == "get_task_catalog":
            return tool_response_text(get_task_catalog(arguments), request_id)
        if name == "run_task_on_node":
            return tool_response_text(run_task_on_node(arguments), request_id)
        if name == "schedule_on_node":
            return tool_response_text(schedule_on_node(arguments), request_id)
        return make_error(request_id, -32602, f"unknown tool: {name}")
    except RuntimeError as exc:
        return tool_response_error(str(exc), request_id)


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params", {})
    if method == "initialize":
        return handle_initialize(request_id)
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return handle_tools_list(request_id)
    if method == "tools/call":
        return handle_tools_call(request_id, params)
    if request_id is None:
        return None
    return make_error(request_id, -32601, f"method not found: {method}")


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            response = handle_request(message)
        except json.JSONDecodeError:
            response = make_error(None, -32700, "parse error")
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
