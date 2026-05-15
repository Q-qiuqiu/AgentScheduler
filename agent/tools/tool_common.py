import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


def gateway_host() -> str:
    return os.environ.get("GATEWAY_HOST", "127.0.0.1")


def gateway_port() -> str:
    return os.environ.get("GATEWAY_PORT", "6666")


def gateway_url(path: str) -> str:
    return f"http://{gateway_host()}:{gateway_port()}{path}"


def default_static_info_path() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "config_files", "static_info.json"))


def default_agent_info_path() -> str:
    return os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "config_files", "agent_info.json"))


def load_json_config(path: str, config_name: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except OSError as exc:
        raise RuntimeError(f"failed to read {config_name} from {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in {config_name} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{config_name} must be a JSON object")
    return payload


def fetch_json(url: str, timeout: int = 10) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"gateway returned HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach gateway at {url}: {exc.reason}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gateway returned invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("gateway returned non-object JSON payload")
    return payload


def task_id_for(task_type: str | None) -> str:
    if not task_type:
        raise RuntimeError("task_type is required")

    static_info_path = os.environ.get("STATIC_INFO_PATH", default_static_info_path())
    static_info = load_json_config(static_info_path, "static_info.json")
    if task_type not in static_info:
        raise RuntimeError(f"unsupported task_type: {task_type}")

    agent_info_path = os.environ.get("AGENT_INFO_PATH", default_agent_info_path())
    agent_info = load_json_config(agent_info_path, "agent_info.json").get("agent", {})
    tools = agent_info.get("tools", {}) if isinstance(agent_info, dict) else {}
    metadata = tools.get(task_type, {}) if isinstance(tools, dict) else {}
    if isinstance(metadata, dict):
        return metadata.get("task_id") or metadata.get("task_type") or task_type
    return task_type


def add_form_field(body: bytearray, boundary: str, key: str, value: Any) -> None:
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
    body.extend(str(value).encode("utf-8"))
    body.extend(b"\r\n")


def add_file_field(body: bytearray, boundary: str, field_name: str, file_path: str) -> None:
    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(file_path, "rb") as file_obj:
        file_bytes = file_obj.read()

    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(
        (
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"\r\n'
        ).encode("utf-8")
    )
    body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    body.extend(file_bytes)
    body.extend(b"\r\n")


def encode_multipart_formdata(
    fields: dict[str, Any] | None = None,
    file_field_name: str | None = None,
    file_path: str | None = None,
) -> tuple[bytes, str]:
    boundary = f"----agent-scheduler-{uuid.uuid4().hex}"
    body = bytearray()

    for key, value in (fields or {}).items():
        if value is not None:
            add_form_field(body, boundary, key, value)

    if file_path:
        add_file_field(body, boundary, file_field_name or "file", file_path)

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def post_empty_json(url: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, data=b"", method="POST")
    request.add_header("Content-Length", "0")
    return read_gateway_response(request, url, timeout)


def post_multipart_json(url: str, fields: dict[str, Any], file_field_name: str, input_path: str | None, timeout: int = 120) -> dict[str, Any]:
    body, content_type = encode_multipart_formdata(fields, file_field_name, input_path)
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(body)))
    return read_gateway_response(request, url, timeout)


def read_gateway_response(request: urllib.request.Request, url: str, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gateway returned HTTP {exc.code} for {url}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach gateway at {url}: {exc.reason}") from exc

    try:
        return json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gateway returned invalid JSON: {exc}") from exc
