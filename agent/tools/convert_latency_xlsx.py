#!/usr/bin/env python3
import argparse
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"a": SPREADSHEET_NS, "r": REL_NS}

TASK_NAME_MAP = {
    "YOLO": "YoloV5",
    "YoloV5": "YoloV5",
    "Mobilenet": "MobileNet",
    "MobileNet": "MobileNet",
    "Resnet": "ResNet50",
    "ResNet": "ResNet50",
    "ResNet50": "ResNet50",
    "Deeplab": "deeplabv3",
    "deeplabv3": "deeplabv3",
    "Bert": "Bert",
    "BERT": "Bert",
}

def cell_column(ref: str) -> str:
    return re.match(r"([A-Z]+)", ref).group(1)


def column_index(column: str) -> int:
    result = 0
    for char in column:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def cell_value(cell: ET.Element, shared_strings: list[str]) -> Any:
    value = cell.find("a:v", NS)
    if value is None:
        return None
    raw_value = value.text
    if raw_value is None:
        return None
    if cell.attrib.get("t") == "s":
        return shared_strings[int(raw_value)]
    return raw_value


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    value = str(value).strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def normalize_task_type(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return TASK_NAME_MAP.get(text, text or None)


def normalize_device_type(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace("-", "_")


def normalize_header(value: Any) -> str:
    return str(value or "").strip().lower()


def header_role(value: Any) -> str | None:
    text = normalize_header(value)
    if not text:
        return None
    if "模型" in text or text in {"model", "task", "task_type"}:
        return "model"
    if "开发板" in text or "device" in text or "board" in text:
        return "device"
    if "间隔" in text or "interval" in text:
        return "interval"
    if "cpu" in text:
        return "cpu"
    if "mem" in text or "内存" in text:
        return "mem"
    if "npu" in text or "xpu" in text:
        return "npu"
    if "端到端" in text or "e2e" in text or "ete" in text:
        return "e2e"
    if "执行" in text or "exec" in text:
        return "exec"
    return None


def detect_blocks(header_values: dict[str, Any]) -> list[dict[str, str]]:
    ordered = sorted(header_values, key=column_index)
    model_columns = [
        column
        for column in ordered
        if header_role(header_values.get(column)) == "model"
    ]
    blocks: list[dict[str, str]] = []
    for index, model_column in enumerate(model_columns):
        start = column_index(model_column)
        end = (
            column_index(model_columns[index + 1]) - 1
            if index + 1 < len(model_columns)
            else max(column_index(column) for column in ordered)
        )
        block: dict[str, str] = {}
        for column, value in header_values.items():
            column_number = column_index(column)
            if column_number < start or column_number > end:
                continue
            role = header_role(value)
            if role:
                block[role] = column
        required = {"model", "device", "cpu", "mem", "npu", "e2e"}
        if required.issubset(block):
            blocks.append(block)
    return blocks


def load_shared_strings(zip_file: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zip_file.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    result = []
    for item in root.findall("a:si", NS):
        result.append("".join(text.text or "" for text in item.findall(".//a:t", NS)))
    return result


def worksheet_paths(zip_file: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(zip_file.read("xl/workbook.xml"))
    relationships = ET.fromstring(zip_file.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        rel.attrib["Id"]: rel.attrib["Target"].lstrip("/")
        for rel in relationships
    }

    paths = []
    for sheet in workbook.find("a:sheets", NS):
        sheet_name = sheet.attrib["name"]
        rel_id = sheet.attrib[f"{{{REL_NS}}}id"]
        target = rel_targets[rel_id]
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        paths.append((sheet_name, target))
    return paths


def convert_latency_xlsx(input_path: Path) -> dict[str, Any]:
    records = []
    with zipfile.ZipFile(input_path) as zip_file:
        shared_strings = load_shared_strings(zip_file)
        for sheet_name, sheet_path in worksheet_paths(zip_file):
            root = ET.fromstring(zip_file.read(sheet_path))
            rows = []
            for row in root.findall("a:sheetData/a:row", NS):
                values = {}
                for cell in row.findall("a:c", NS):
                    values[cell_column(cell.attrib["r"])] = cell_value(cell, shared_strings)
                rows.append((int(row.attrib["r"]), values))
            header_values = next((values for row_index, values in rows if row_index == 1), {})
            blocks = detect_blocks(header_values)
            current: dict[int, dict[str, str | None]] = {
                index: {"task_type": None, "device_type": None}
                for index in range(len(blocks))
            }

            for row_index, values in rows:
                if row_index == 1:
                    continue

                for block_index, columns in enumerate(blocks):
                    task_type = normalize_task_type(values.get(columns["model"]))
                    device_type = normalize_device_type(values.get(columns["device"]))
                    if task_type:
                        current[block_index]["task_type"] = task_type
                    if device_type:
                        current[block_index]["device_type"] = device_type

                    avg_cpu = parse_number(values.get(columns["cpu"]))
                    avg_mem = parse_number(values.get(columns["mem"]))
                    avg_npu = parse_number(values.get(columns["npu"]))
                    e2e_ms = parse_number(values.get(columns["e2e"]))
                    exec_ms = parse_number(values.get(columns.get("exec")))
                    if avg_cpu is None or avg_mem is None or avg_npu is None or e2e_ms is None:
                        continue

                    block_context = current[block_index]
                    if not block_context["task_type"] or not block_context["device_type"]:
                        continue

                    records.append({
                        "task_type": block_context["task_type"],
                        "device_type": block_context["device_type"],
                        "interval_ms": parse_number(values.get(columns.get("interval"))),
                        "avg_cpu_percent": avg_cpu,
                        "avg_mem_percent": avg_mem,
                        "avg_npu_percent": avg_npu,
                        "e2e_ms": e2e_ms,
                        "exec_ms": exec_ms,
                        "source": {
                            "file": input_path.name,
                            "sheet": sheet_name,
                            "row": row_index,
                            "block": block_index + 1,
                        },
                    })

    return {
        "schema_version": 1,
        "metric_priority": "e2e_ms",
        "resource_unit": "percent",
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert latency.xlsx into latency_profiles.json")
    parser.add_argument("input", nargs="?", default="config_files/latency.xlsx")
    parser.add_argument("output", nargs="?", default="config_files/latency_profiles.json")
    args = parser.parse_args()

    payload = convert_latency_xlsx(Path(args.input))
    output_path = Path(args.output)
    with output_path.open("w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")
    print(f"wrote {len(payload['records'])} records to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
