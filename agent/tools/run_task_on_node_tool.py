import os
import urllib.parse
from typing import Any

from tool_common import gateway_url, post_multipart_json, task_id_for


def run_task_on_node(arguments: dict[str, Any]) -> dict[str, Any]:
    task_type = arguments.get("task_type")
    target_global_id = arguments.get("target_global_id")
    input_path = arguments.get("input_path") or arguments.get("image_path")
    real_url = arguments.get("real_url", "predict")
    file_field_name = arguments.get("file_field_name", "image")
    fields = arguments.get("fields") or {}

    task_id = task_id_for(task_type)
    if not target_global_id:
        raise RuntimeError("target_global_id is required")
    if input_path and not os.path.exists(input_path):
        raise RuntimeError(f"input_path does not exist: {input_path}")
    if not isinstance(fields, dict):
        raise RuntimeError("fields must be an object")

    path = os.environ.get("GATEWAY_QUEST_ON_NODE_PATH", "/quest_on_node")
    query = urllib.parse.urlencode(
        {
            "taskid": task_id,
            "target_global_id": target_global_id,
            "real_url": real_url,
        }
    )
    return post_multipart_json(f"{gateway_url(path)}?{query}", fields, file_field_name, input_path, timeout=120)
