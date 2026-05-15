import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


TASK_TYPE_TO_TASK_ID = {
    "YoloV5": "YoloV5",
    "MobileNet": "MobileNet",
    "ResNet50": "ResNet50",
    "Bert": "Bert",
    "deeplabv3": "deeplabv3",
}


def _gateway_host() -> str:
    return os.environ.get("GATEWAY_HOST", "127.0.0.1")


def _gateway_port() -> str:
    return os.environ.get("GATEWAY_PORT", "6666")


def _gateway_url(path: str) -> str:
    return f"http://{_gateway_host()}:{_gateway_port()}{path}"


def _add_form_field(body: bytearray, boundary: str, key: str, value: Any) -> None:
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
    body.extend(str(value).encode("utf-8"))
    body.extend(b"\r\n")


def _add_file_field(body: bytearray, boundary: str, field_name: str, file_path: str) -> None:
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
            _add_form_field(body, boundary, key, value)

    if file_path:
        _add_file_field(body, boundary, file_field_name or "file", file_path)

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def run_task_on_node(arguments: dict[str, Any]) -> dict[str, Any]:
    task_type = arguments.get("task_type")
    target_global_id = arguments.get("target_global_id")
    input_path = arguments.get("input_path") or arguments.get("image_path")
    real_url = arguments.get("real_url", "predict")
    file_field_name = arguments.get("file_field_name", "image")
    fields = arguments.get("fields") or {}

    if task_type not in TASK_TYPE_TO_TASK_ID:
        raise RuntimeError(f"unsupported task_type: {task_type}")
    if not target_global_id:
        raise RuntimeError("target_global_id is required")
    if input_path and not os.path.exists(input_path):
        raise RuntimeError(f"input_path does not exist: {input_path}")
    if not isinstance(fields, dict):
        raise RuntimeError("fields must be an object")

    path = os.environ.get("GATEWAY_QUEST_ON_NODE_PATH", "/quest_on_node")
    query = urllib.parse.urlencode(
        {
            "taskid": TASK_TYPE_TO_TASK_ID[task_type],
            "target_global_id": target_global_id,
            "real_url": real_url,
        }
    )
    url = f"{_gateway_url(path)}?{query}"

    body, content_type = encode_multipart_formdata(fields, file_field_name, input_path)
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
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


def schedule_task(arguments: dict[str, Any]) -> dict[str, Any]:
    task_type = arguments.get("task_type")
    if task_type not in TASK_TYPE_TO_TASK_ID:
        raise RuntimeError(f"unsupported task_type: {task_type}")

    path = os.environ.get("GATEWAY_SCHEDULE_TASK_PATH", "/schedule_task")
    query = urllib.parse.urlencode({"taskid": TASK_TYPE_TO_TASK_ID[task_type]})
    url = f"{_gateway_url(path)}?{query}"
    request = urllib.request.Request(url, data=b"", method="POST")
    request.add_header("Content-Length", "0")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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


def run_task_auto_schedule(arguments: dict[str, Any]) -> dict[str, Any]:
    task_type = arguments.get("task_type")
    input_path = arguments.get("input_path") or arguments.get("image_path")
    real_url = arguments.get("real_url", "predict")
    file_field_name = arguments.get("file_field_name", "image")
    fields = arguments.get("fields") or {}

    if task_type not in TASK_TYPE_TO_TASK_ID:
        raise RuntimeError(f"unsupported task_type: {task_type}")
    if input_path and not os.path.exists(input_path):
        raise RuntimeError(f"input_path does not exist: {input_path}")
    if not isinstance(fields, dict):
        raise RuntimeError("fields must be an object")

    path = os.environ.get("GATEWAY_QUEST_PATH", "/quest")
    query = urllib.parse.urlencode(
        {
            "taskid": TASK_TYPE_TO_TASK_ID[task_type],
            "real_url": real_url,
        }
    )
    url = f"{_gateway_url(path)}?{query}"

    body, content_type = encode_multipart_formdata(fields, file_field_name, input_path)
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
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
