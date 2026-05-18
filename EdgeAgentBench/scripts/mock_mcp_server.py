#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from typing import Any


TASK_CATALOG = [
    {
        "task_type": "YoloV5",
        "model_name": "YoloV5",
        "tool_kind": "vision_model",
        "requires_file": True,
        "file_field_name": "image",
        "real_url": "predict",
        "capabilities": ["object_detection", "multi_object_detection", "bounding_box_estimation"],
        "description": "Object detection model for locating targets in an image.",
        "device_types": ["RK3588", "ATLAS_L", "ATLAS_H"],
    },
    {
        "task_type": "MobileNet",
        "model_name": "MobileNet",
        "tool_kind": "vision_model",
        "requires_file": True,
        "file_field_name": "image",
        "real_url": "predict",
        "capabilities": ["image_classification"],
        "description": "Fast image classification model.",
        "device_types": ["RK3588", "ATLAS_L", "ATLAS_H"],
    },
    {
        "task_type": "ResNet50",
        "model_name": "ResNet50",
        "tool_kind": "vision_model",
        "requires_file": True,
        "file_field_name": "image",
        "real_url": "predict",
        "capabilities": ["image_classification"],
        "description": "Image classification model.",
        "device_types": ["RK3588", "ATLAS_L", "ATLAS_H"],
    },
    {
        "task_type": "Bert",
        "model_name": "Bert",
        "tool_kind": "text_model",
        "requires_file": False,
        "file_field_name": None,
        "real_url": "predict",
        "capabilities": ["text_classification"],
        "description": "Text classification model.",
        "device_types": ["ATLAS_L", "ATLAS_H"],
    },
    {
        "task_type": "deeplabv3",
        "model_name": "deeplabv3",
        "tool_kind": "segmentation_model",
        "requires_file": True,
        "file_field_name": "image",
        "real_url": "segmentation",
        "capabilities": ["semantic_segmentation"],
        "description": "Semantic segmentation model.",
        "device_types": ["ATLAS_L", "ATLAS_H"],
    },
]


def make_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def make_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_response_text(payload: dict[str, Any], request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]},
    )


def get_task_catalog(arguments: dict[str, Any]) -> dict[str, Any]:
    available = set(arguments.get("available_device_types") or [])
    use_filter = "available_device_types" in arguments
    result = []
    for task in TASK_CATALOG:
        supported_device_types = [
            device_type
            for device_type in task["device_types"]
            if not use_filter or device_type in available
        ]
        if supported_device_types:
            item = {key: value for key, value in task.items() if key != "device_types"}
            item["supported_device_types"] = supported_device_types
            result.append(item)
    return {"status": "success", "result": result}


def get_cluster_resources(_: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "success",
        "result": [
            {"global_id": "mock-rk3588-1", "type": "RK3588", "resource": {"cpu_used": 0.2, "mem_used": 0.3, "npu_used": 0.2}},
            {"global_id": "mock-rk3588-2", "type": "RK3588", "resource": {"cpu_used": 0.3, "mem_used": 0.4, "npu_used": 0.3}},
            {"global_id": "mock-atlas-l", "type": "ATLAS_L", "resource": {"cpu_used": 0.4, "mem_used": 0.4, "npu_used": 0.4}},
            {"global_id": "mock-atlas-h", "type": "ATLAS_H", "resource": {"cpu_used": 0.2, "mem_used": 0.3, "npu_used": 0.2}},
        ],
    }


def get_task_overhead(arguments: dict[str, Any]) -> dict[str, Any]:
    task_type = arguments.get("task_type") or "YoloV5"
    device_type = arguments.get("device_type") or "RK3588"
    return {
        "status": "success",
        "result": [
            {
                "task_type": task_type,
                "device_type": device_type,
                "overhead": {
                    "proc_time": 1.0,
                    "mem_usage": 0.1,
                    "cpu_usage": 0.1,
                    "xpu_usage": 0.1,
                },
            }
        ],
    }


def schedule_task(arguments: dict[str, Any]) -> dict[str, Any]:
    require_nodes = arguments.get("require_nodes") or []
    require_node = arguments.get("require_node")
    exclude_nodes = set(arguments.get("exclude_nodes") or [])
    if require_node:
        require_nodes = [require_node]

    if require_nodes:
        selected_type = require_nodes[0]
    elif "RK3588" not in exclude_nodes:
        selected_type = "RK3588"
    elif "ATLAS_H" not in exclude_nodes:
        selected_type = "ATLAS_H"
    else:
        selected_type = "ATLAS_L"

    return {
        "status": "success",
        "result": {
            "task_type": arguments.get("task_type"),
            "target_global_id": f"mock-{str(selected_type).lower().replace('_', '-')}",
            "type": selected_type,
            "ip_address": "127.0.0.1",
            "agent_port": 0,
            "exclude_nodes": list(exclude_nodes),
            "require_nodes": require_nodes,
        },
    }


def run_task_on_node(arguments: dict[str, Any]) -> dict[str, Any]:
    task_type = arguments.get("task_type")
    if task_type in {"YoloV5", "object_detection"}:
        return {"status": "success", "results": [{"class": "mock", "confidence": 0.9, "bbox": [1, 2, 3, 4]}], "exec_time": 1.0}
    if task_type in {"MobileNet", "ResNet50", "image_classification"}:
        return {"status": "success", "results": [{"class": "mock", "confidence": 0.9}], "exec_time": 1.0}
    return {"status": "success", "result": "mock task completed", "exec_time": 1.0}


def handle_tools_list(request_id: Any) -> dict[str, Any]:
    return make_response(
        request_id,
        {
            "tools": [
                {"name": "get_cluster_resources", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "get_task_catalog", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "get_task_overhead", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "schedule_task", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "run_task_on_node", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "run_task_auto_schedule", "inputSchema": {"type": "object", "properties": {}}},
            ]
        },
    )


def handle_tools_call(request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    handlers = {
        "get_cluster_resources": get_cluster_resources,
        "get_task_catalog": get_task_catalog,
        "get_task_overhead": get_task_overhead,
        "schedule_task": schedule_task,
        "run_task_on_node": run_task_on_node,
        "run_task_auto_schedule": run_task_on_node,
    }
    if name not in handlers:
        return make_error(request_id, -32602, f"unknown tool: {name}")
    return tool_response_text(handlers[name](arguments), request_id)


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return make_response(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "edge-agent-bench-mock", "version": "0.1"},
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return handle_tools_list(request_id)
    if method == "tools/call":
        return handle_tools_call(request_id, params)
    return make_error(request_id, -32601, f"method not found: {method}") if request_id is not None else None


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            response = handle_request(json.loads(line))
        except Exception as exc:
            response = make_error(None, -32000, str(exc))
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
