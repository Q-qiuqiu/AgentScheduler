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

BLOCKS = [
    ("A", "B", "C", "D", "E", "F", "G", "H"),
    ("J", "K", "L", "M", "N", "O", "P", "Q"),
    ("S", "T", "U", "V", "W", "X", "Y", "Z"),
]


def cell_column(ref: str) -> str:
    return re.match(r"([A-Z]+)", ref).group(1)


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
            current: dict[int, dict[str, str | None]] = {
                index: {"task_type": None, "device_type": None}
                for index in range(len(BLOCKS))
            }

            for row in root.findall("a:sheetData/a:row", NS):
                row_index = int(row.attrib["r"])
                if row_index == 1:
                    continue

                values = {}
                for cell in row.findall("a:c", NS):
                    values[cell_column(cell.attrib["r"])] = cell_value(cell, shared_strings)

                for block_index, columns in enumerate(BLOCKS):
                    model_col, device_col, interval_col, cpu_col, mem_col, npu_col, e2e_col, exec_col = columns
                    task_type = normalize_task_type(values.get(model_col))
                    device_type = values.get(device_col)
                    if task_type:
                        current[block_index]["task_type"] = task_type
                    if device_type:
                        current[block_index]["device_type"] = str(device_type).strip()

                    avg_cpu = parse_number(values.get(cpu_col))
                    avg_mem = parse_number(values.get(mem_col))
                    avg_npu = parse_number(values.get(npu_col))
                    e2e_ms = parse_number(values.get(e2e_col))
                    exec_ms = parse_number(values.get(exec_col))
                    if avg_cpu is None or avg_mem is None or avg_npu is None or e2e_ms is None:
                        continue

                    block_context = current[block_index]
                    if not block_context["task_type"] or not block_context["device_type"]:
                        continue

                    records.append({
                        "task_type": block_context["task_type"],
                        "device_type": block_context["device_type"],
                        "interval_ms": parse_number(values.get(interval_col)),
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
