import os
from typing import Any

from tool_common import fetch_json, gateway_url


def get_cluster_resources(arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    path = os.environ.get("GATEWAY_CLUSTER_RESOURCES_PATH", "/cluster_resources")
    return fetch_json(gateway_url(path))
