#!/usr/bin/env python3
import json
import sys
from typing import Any

from cluster_resource_tool import fetch_cluster_resources
from task_catalog_tool import fetch_task_catalog
from tool_execute_tool import run_task_auto_schedule, run_task_on_node, schedule_task


GET_CLUSTER_RESOURCES_TOOL = "get_cluster_resources"
GET_TASK_CATALOG_TOOL = "get_task_catalog"
SCHEDULE_TASK_TOOL = "schedule_task"
RUN_TASK_ON_NODE_TOOL = "run_task_on_node"
RUN_TASK_AUTO_SCHEDULE_TOOL = "run_task_auto_schedule"


def make_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def tool_response_text(payload: dict[str, Any], request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
        },
    )


def tool_response_error(message: str, request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        },
    )


def handle_initialize(request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "edge-cluster-tools", "version": "0.3.0"},
        },
    )


def handle_tools_list(request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {
            "tools": [
                {
                    "name": GET_CLUSTER_RESOURCES_TOOL,
                    "description": "Fetch the latest cluster resource snapshot collected by the gateway.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
                {
                    "name": GET_TASK_CATALOG_TOOL,
                    "description": "Fetch a compact task catalog from JSON config files. static_info.json provides deployment and overhead data; agent_info.json provides task metadata. Optionally filter by available_device_types from get_cluster_resources.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "available_device_types": {
                                "type": "array",
                                "items": {"type": "string"},
                            }
                        },
                        "additionalProperties": False,
                    },
                },
                {
                    "name": SCHEDULE_TASK_TOOL,
                    "description": "Ask the gateway scheduler to select the best target board for a task_type. This does not start the tool container or run inference.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_type": {"type": "string"},
                        },
                        "required": ["task_type"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": RUN_TASK_AUTO_SCHEDULE_TOOL,
                    "description": "Run a configured task through the gateway scheduler. The caller provides task_type and request payload only; the gateway selects the target board, creates or reuses the container, and forwards the request.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_type": {"type": "string"},
                            "input_path": {"type": "string"},
                            "real_url": {"type": "string", "default": "predict"},
                            "file_field_name": {"type": "string", "default": "image"},
                            "fields": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                        },
                        "required": ["task_type"],
                        "additionalProperties": False,
                    },
                },
                {
                    "name": RUN_TASK_ON_NODE_TOOL,
                    "description": "Run a configured task tool on a specific board. Prefer run_task_auto_schedule unless the target_global_id is explicitly required.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_type": {"type": "string"},
                            "target_global_id": {"type": "string"},
                            "input_path": {"type": "string"},
                            "real_url": {"type": "string", "default": "predict"},
                            "file_field_name": {"type": "string", "default": "image"},
                            "fields": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                            },
                        },
                        "required": ["task_type", "target_global_id"],
                        "additionalProperties": False,
                    },
                },
            ]
        },
    )


def handle_tools_call(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments", {})

    try:
        if name == GET_CLUSTER_RESOURCES_TOOL:
            return tool_response_text(fetch_cluster_resources(), request_id)
        if name == GET_TASK_CATALOG_TOOL:
            return tool_response_text(fetch_task_catalog(arguments.get("available_device_types")), request_id)
        if name == SCHEDULE_TASK_TOOL:
            return tool_response_text(schedule_task(arguments), request_id)
        if name == RUN_TASK_AUTO_SCHEDULE_TOOL:
            return tool_response_text(run_task_auto_schedule(arguments), request_id)
        if name == RUN_TASK_ON_NODE_TOOL:
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
        except json.JSONDecodeError:
            response = make_error(None, -32700, "parse error")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        response = handle_request(message)
        if response is None:
            continue

        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
