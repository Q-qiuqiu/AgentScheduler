import os
import urllib.parse
from typing import Any

from tool_common import gateway_url, post_empty_json, task_id_for


def schedule_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_id = task_id_for(arguments.get("task_type"))
    path = os.environ.get("GATEWAY_SCHEDULE_TASK_PATH", "/schedule_task")
    query = urllib.parse.urlencode({"taskid": task_id})
    return post_empty_json(f"{gateway_url(path)}?{query}", timeout=30)
