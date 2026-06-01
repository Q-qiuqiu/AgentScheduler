#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BENCH_ROOT.parent
DEFAULT_CURRENT_CASE_FILE = Path("/tmp/edgeagentbench_current_case.txt")
AGENT_INFO_PATH = REPO_ROOT / "config_files" / "agent_info.json"
STATIC_INFO_PATH = REPO_ROOT / "config_files" / "static_info.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def current_case_path() -> Path:
    pointer_path = Path(os.environ.get("EDGE_BENCH_CURRENT_CASE_FILE", str(DEFAULT_CURRENT_CASE_FILE)))
    try:
        case_path = Path(pointer_path.read_text(encoding="utf-8").strip())
    except OSError as exc:
        raise RuntimeError(f"failed to read current case pointer {pointer_path}: {exc}") from exc
    if not case_path.exists():
        raise RuntimeError(f"current case does not exist: {case_path}")
    return case_path


def load_current_case() -> dict[str, Any]:
    return load_json(current_case_path())


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


def run_task_on_node(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    target_global_id = arguments.get("target_global_id")
    task_type = arguments.get("task_type")
    case = load_current_case()
    node = next((item for item in case.get("nodes", []) if item.get("node_id") == target_global_id), None)
    if not node:
        raise RuntimeError(f"target_global_id is not in current case: {target_global_id}")
    return {
        "status": "success",
        "mocked": True,
        "task_type": task_type,
        "target_global_id": target_global_id,
        "device_type": node.get("platform"),
        "case_id": case.get("case_id"),
        "results": [],
    }


def handle_tools_list(request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {
            "tools": [
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
        if name == "get_cluster_resources":
            return tool_response_text(get_cluster_resources(arguments), request_id)
        if name == "get_task_catalog":
            return tool_response_text(get_task_catalog(arguments), request_id)
        if name == "run_task_on_node":
            return tool_response_text(run_task_on_node(arguments), request_id)
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
