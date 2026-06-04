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
    if text in {"模型", "model", "Model"}:
        return None
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


def detect_sampling_blocks(rows: list[tuple[int, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows_by_index = {row_index: values for row_index, values in rows}
    blocks: list[dict[str, Any]] = []
    block_starts = [
        ("J", "K", "L", "M", "N", "O", "P", "Q"),
        ("S", "T", "U", "V", "W", "X", "Y", "Z"),
        ("AB", "AC", "AD", "AE", "AF", "AG", "AH", "AI"),
    ]
    for block_start_index, columns in enumerate(block_starts):
        detected_for_start: list[dict[str, Any]] = []
        for row_index, values in rows:
            model_col, device_col, interval_col, cpu_col, mem_col, npu_col, e2e_col, exec_col = columns
            task_type = normalize_task_type(values.get(model_col))
            device_type = normalize_device_type(values.get(device_col))
            if not task_type or not device_type:
                continue

            metric_values = values
            metric_row_index = row_index
            if not all(header_role(metric_values.get(column)) for column in (cpu_col, mem_col, npu_col, e2e_col)):
                metric_values = rows_by_index.get(row_index + 1, {})
                metric_row_index = row_index + 1
            if not all(header_role(metric_values.get(column)) for column in (cpu_col, mem_col, npu_col, e2e_col)):
                metric_values = rows_by_index.get(row_index - 1, {})
                metric_row_index = row_index - 1
            if not all(header_role(metric_values.get(column)) for column in (cpu_col, mem_col, npu_col, e2e_col)):
                continue

            data_start = row_index + 1
            if metric_row_index > row_index:
                data_start = row_index + 2
            elif metric_row_index < row_index:
                data_start = row_index
            detected_for_start.append({
                "task_type": task_type,
                "device_type": device_type,
                "interval_ms": parse_number(values.get(interval_col)),
                "data_start": data_start,
                "data_end": None,
                "columns": {
                    "cpu": cpu_col,
                    "mem": mem_col,
                    "npu": npu_col,
                    "e2e": e2e_col,
                    "exec": exec_col,
                },
                "block_start_index": block_start_index,
            })
        detected_for_start.sort(key=lambda item: item["data_start"])
        for index, block in enumerate(detected_for_start):
            if index + 1 < len(detected_for_start):
                block["data_end"] = detected_for_start[index + 1]["data_start"] - 1
            blocks.append(block)
    blocks.sort(key=lambda item: (item["data_start"], column_index(item["columns"]["cpu"])))
    for block_index, block in enumerate(blocks, 1):
        block["block"] = block_index
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
            blocks = detect_sampling_blocks(rows)
            if not blocks:
                continue

            for row_index, values in rows:
                for block in blocks:
                    if row_index < block["data_start"]:
                        continue
                    if block.get("data_end") is not None and row_index > block["data_end"]:
                        continue
                    columns = block["columns"]
                    avg_cpu = parse_number(values.get(columns["cpu"]))
                    avg_mem = parse_number(values.get(columns["mem"]))
                    avg_npu = parse_number(values.get(columns["npu"]))
                    e2e_ms = parse_number(values.get(columns["e2e"]))
                    exec_ms = parse_number(values.get(columns.get("exec")))
                    if avg_cpu is None or avg_mem is None or avg_npu is None or e2e_ms is None:
                        continue

                    records.append({
                        "task_type": block["task_type"],
                        "device_type": block["device_type"],
                        "interval_ms": block["interval_ms"],
                        "avg_cpu_percent": avg_cpu,
                        "avg_mem_percent": avg_mem,
                        "avg_npu_percent": avg_npu,
                        "e2e_ms": e2e_ms,
                        "exec_ms": exec_ms,
                        "source": {
                            "file": input_path.name,
                            "sheet": sheet_name,
                            "row": row_index,
                            "block": block["block"],
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
