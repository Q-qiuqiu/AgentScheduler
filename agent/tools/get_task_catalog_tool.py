import os
from typing import Any

from tool_common import default_agent_info_path, default_static_info_path, load_json_config


def load_task_metadata() -> dict[str, Any]:
    agent_info_path = os.environ.get("AGENT_INFO_PATH", default_agent_info_path())
    payload = load_json_config(agent_info_path, "agent_info.json")
    tools = payload.get("agent", {}).get("tools", {})
    if not isinstance(tools, dict):
        raise RuntimeError("agent_info.json must contain an object field named agent.tools")
    return tools


def get_task_catalog(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    available_device_types = arguments.get("available_device_types")
    static_info_path = os.environ.get("STATIC_INFO_PATH", default_static_info_path())
    payload = load_json_config(static_info_path, "static_info.json")

    allowed_device_types = set(available_device_types or [])
    use_filter = bool(allowed_device_types)
    task_metadata = load_task_metadata()
    result: list[dict[str, Any]] = []

    for task_name, device_entries in payload.items():
        if not isinstance(device_entries, dict):
            continue
        metadata = task_metadata.get(task_name, {})

        for device_type, config in device_entries.items():
            if not isinstance(config, dict):
                continue
            if use_filter and device_type not in allowed_device_types:
                continue
            result.append(
                {
                    "device_type": device_type,
                    "model_name": task_name,
                    "task_type": task_name,
                    "tool_kind": metadata.get("tool_kind"),
                    "requires_file": metadata.get("requires_file", False),
                    "file_field_name": metadata.get("file_field_name"),
                    "real_url": metadata.get("real_url", "predict"),
                    "capabilities": metadata.get("capabilities", []),
                    "description": metadata.get("description", ""),
                }
            )

    return {"status": "success", "result": result}
