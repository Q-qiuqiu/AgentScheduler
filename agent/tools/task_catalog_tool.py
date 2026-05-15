import json
import os
from typing import Any


def _default_static_info_path() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "config_files", "static_info.json"))


def _default_agent_info_path() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "config_files", "agent_info.json"))


def load_task_metadata() -> dict[str, Any]:
    agent_info_path = os.environ.get("AGENT_INFO_PATH", _default_agent_info_path())
    try:
        with open(agent_info_path, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except OSError as exc:
        raise RuntimeError(f"failed to read agent info from {agent_info_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in agent info {agent_info_path}: {exc}") from exc

    tools = payload.get("agent", {}).get("tools", {})
    if not isinstance(tools, dict):
        raise RuntimeError("agent_info.json must contain an object field named agent.tools")
    return tools


def fetch_task_catalog(available_device_types: list[str] | None = None) -> dict[str, Any]:
    static_info_path = os.environ.get("STATIC_INFO_PATH", _default_static_info_path())
    try:
        with open(static_info_path, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except OSError as exc:
        raise RuntimeError(f"failed to read static info from {static_info_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in static info {static_info_path}: {exc}") from exc

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
            task_overhead = config.get("taskOverhead", {})
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
                    "overhead": {
                        "proc_time": task_overhead.get("proc_time"),
                        "mem_usage": task_overhead.get("mem_usage"),
                        "cpu_usage": task_overhead.get("cpu_usage"),
                        "xpu_usage": task_overhead.get("xpu_usage"),
                    },
                }
            )

    return {"status": "success", "result": result}
