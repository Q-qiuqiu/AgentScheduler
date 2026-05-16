import os
import urllib.parse
from typing import Any

from tool_common import gateway_url, post_empty_json, task_id_for


def schedule_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = task_id_for(arguments.get("task_type"))
    path = os.environ.get("GATEWAY_SCHEDULE_TASK_PATH", "/schedule_task")
    query_params: dict[str, Any] = {"taskid": task_id}
    for key in ("exclude_nodes", "require_node", "require_nodes"):
        value = arguments.get(key)
        if isinstance(value, list):
            query_params[key] = ",".join(str(item) for item in value if item)
        elif value:
            query_params[key] = str(value)
    query = urllib.parse.urlencode(query_params)
    return post_empty_json(f"{gateway_url(path)}?{query}", timeout=30)
