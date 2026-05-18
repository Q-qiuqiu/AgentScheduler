import os
from typing import Any

from tool_common import default_static_info_path, load_json_config


TASK_ALIASES = {
    "yolo": "YoloV5",
    "yolov5": "YoloV5",
    "mobilenet": "MobileNet",
    "resnet": "ResNet50",
    "resnet50": "ResNet50",
    "bert": "Bert",
    "deeplab": "deeplabv3",
    "deeplabv3": "deeplabv3",
}


def canonical_key(value: Any, keys: set[str], aliases: dict[str, str] | None = None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if text in keys:
        return text
    lowered = text.lower()
    if aliases and lowered in aliases:
        return aliases[lowered]
    for key in keys:
        if key.lower() == lowered:
            return key
    return text


def get_task_overhead(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    arguments = arguments or {}
    static_info_path = os.environ.get("STATIC_INFO_PATH", default_static_info_path())
    payload = load_json_config(static_info_path, "static_info.json")
    device_types = {
        device_type
        for device_entries in payload.values()
        if isinstance(device_entries, dict)
        for device_type in device_entries.keys()
    }
    task_type_filter = canonical_key(arguments.get("task_type"), set(payload.keys()), TASK_ALIASES)
    device_type_filter = canonical_key(arguments.get("device_type"), device_types)
    available_device_types = {
        canonical_key(device_type, device_types)
        for device_type in arguments.get("available_device_types", [])
    }
    available_device_types.discard(None)
    result: list[dict[str, Any]] = []

    for task_type, device_entries in payload.items():
        if task_type_filter and task_type != task_type_filter:
            continue
        if not isinstance(device_entries, dict):
            continue

        for device_type, config in device_entries.items():
            if device_type_filter and device_type != device_type_filter:
                continue
            if available_device_types and device_type not in available_device_types:
                continue
            if not isinstance(config, dict):
                continue

            task_overhead = config.get("taskOverhead")
            if not isinstance(task_overhead, dict):
                continue

            result.append(
                {
                    "task_type": task_type,
                    "device_type": device_type,
                    "overhead": {
                        "proc_time": task_overhead.get("proc_time"),
                        "mem_usage": task_overhead.get("mem_usage"),
                        "cpu_usage": task_overhead.get("cpu_usage"),
                        "xpu_usage": task_overhead.get("xpu_usage"),
                    },
                }
            )

    if task_type_filter and not result:
        raise RuntimeError(f"no overhead found for task_type: {task_type_filter}")
    return {"status": "success", "result": result}
