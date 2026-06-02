#!/usr/bin/env python3
import json
import sys
from typing import Any

from get_cluster_resources_tool import get_cluster_resources
from get_task_catalog_tool import get_task_catalog
from get_task_overhead_tool import get_task_overhead
from run_task_auto_schedule_tool import run_task_auto_schedule
from run_task_on_node_tool import run_task_on_node
from schedule_task_tool import schedule_task


GET_CLUSTER_RESOURCES_TOOL = "get_cluster_resources"
GET_TASK_CATALOG_TOOL = "get_task_catalog"
GET_DISPATCH_SNAPSHOT_TOOL = "get_dispatch_snapshot"
GET_TASK_OVERHEAD_TOOL = "get_task_overhead"
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
                    "name": GET_DISPATCH_SNAPSHOT_TOOL,
                    "description": "Fetch cluster resources and the filtered task catalog in one call for direct dispatch.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                },
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
                    "description": "Fetch a compact task catalog from JSON config files. The catalog exposes task metadata needed by the agent, but hides scheduler-only overhead data. Optionally filter by available_device_types from get_cluster_resources.",
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
                    "name": GET_TASK_OVERHEAD_TOOL,
                    "description": "Fetch scheduler-only task overhead data from static_info.json. Use this only when the user explicitly asks for resource cost, overhead, or startup/resource requirements for a task on a device type.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_type": {"type": "string"},
                            "device_type": {"type": "string"},
                            "available_device_types": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
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
                            "exclude_nodes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Node ids or device types that must not be selected, for example RK3588 or ATLAS_L.",
                            },
                            "require_node": {
                                "type": "string",
                                "description": "A node id or device type that must be selected from, for example ATLAS_H.",
                            },
                            "require_nodes": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Node ids or device types that are allowed candidates.",
                            },
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
        if name == GET_DISPATCH_SNAPSHOT_TOOL:
            cluster_resources = get_cluster_resources(arguments)
            available_device_types = sorted({
                node.get("type")
                for node in cluster_resources.get("result", [])
                if isinstance(node, dict) and node.get("type")
            })
            catalog_args = {"available_device_types": available_device_types} if available_device_types else {}
            task_catalog = get_task_catalog(catalog_args)
            return tool_response_text(
                {
                    "status": "success",
                    "cluster_resources": cluster_resources,
                    "task_catalog": task_catalog,
                },
                request_id,
            )
        if name == GET_CLUSTER_RESOURCES_TOOL:
            return tool_response_text(get_cluster_resources(arguments), request_id)
        if name == GET_TASK_CATALOG_TOOL:
            return tool_response_text(get_task_catalog(arguments), request_id)
        if name == GET_TASK_OVERHEAD_TOOL:
            return tool_response_text(get_task_overhead(arguments), request_id)
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
