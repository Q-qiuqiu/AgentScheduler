#!/usr/bin/env python3
import json
from copy import deepcopy
from pathlib import Path


BENCH_ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = BENCH_ROOT / "states"

NODES = ["RK3588_1", "RK3588_2", "ATLAS_L", "ATLAS_H"]
PLATFORMS = {
    "RK3588_1": "RK3588",
    "RK3588_2": "RK3588",
    "ATLAS_L": "Atlas 200DK",
    "ATLAS_H": "Orange Pi aipro",
}
SUPPORT_TASKS = {
    "RK3588_1": ["object_detection", "image_classification"],
    "RK3588_2": ["object_detection", "image_classification"],
    "ATLAS_L": ["object_detection", "image_classification", "semantic_segmentation", "text_classification"],
    "ATLAS_H": ["object_detection", "image_classification", "semantic_segmentation", "text_classification"],
}

READY_VARIANTS = [
    {
        "RK3588_1": ["image_classification"],
        "RK3588_2": ["object_detection"],
        "ATLAS_L": ["semantic_segmentation"],
        "ATLAS_H": ["text_classification"],
    },
    {
        "RK3588_1": ["object_detection", "image_classification"],
        "RK3588_2": [],
        "ATLAS_L": ["text_classification"],
        "ATLAS_H": ["semantic_segmentation"],
    },
    {
        "RK3588_1": [],
        "RK3588_2": ["image_classification"],
        "ATLAS_L": ["object_detection", "semantic_segmentation"],
        "ATLAS_H": [],
    },
    {
        "RK3588_1": ["object_detection"],
        "RK3588_2": ["object_detection", "image_classification"],
        "ATLAS_L": [],
        "ATLAS_H": ["object_detection", "semantic_segmentation", "text_classification"],
    },
]


def clamp(value):
    return round(max(0.0, min(1.0, value)), 2)


def base_load(cpu=0.30, mem=0.35, npu=0.28):
    return {node: {"cpu_used": cpu, "mem_used": mem, "npu_used": npu, "online": True} for node in NODES}


def set_node(loads, node, cpu=None, mem=None, npu=None, online=None):
    if cpu is not None:
        loads[node]["cpu_used"] = cpu
    if mem is not None:
        loads[node]["mem_used"] = mem
    if npu is not None:
        loads[node]["npu_used"] = npu
    if online is not None:
        loads[node]["online"] = online


def apply_variant(loads, variant):
    adjusted = deepcopy(loads)
    deltas = [
        {"cpu": 0.00, "mem": 0.00, "npu": 0.00},
        {"cpu": 0.04, "mem": 0.03, "npu": 0.05},
        {"cpu": -0.03, "mem": 0.05, "npu": -0.02},
        {"cpu": 0.06, "mem": -0.02, "npu": 0.03},
    ][variant]
    for index, node in enumerate(NODES):
        direction = 1 if index % 2 == 0 else -1
        if adjusted[node]["online"]:
            adjusted[node]["cpu_used"] = clamp(adjusted[node]["cpu_used"] + direction * deltas["cpu"])
            adjusted[node]["mem_used"] = clamp(adjusted[node]["mem_used"] + direction * deltas["mem"])
            adjusted[node]["npu_used"] = clamp(adjusted[node]["npu_used"] + direction * deltas["npu"])
    return adjusted


def make_nodes(loads, ready_containers):
    nodes = []
    for node_id in NODES:
        load = loads[node_id]
        online = load["online"]
        nodes.append(
            {
                "node_id": node_id,
                "platform": PLATFORMS[node_id],
                "online": online,
                "cpu_used": 1.0 if not online else load["cpu_used"],
                "mem_used": 1.0 if not online else load["mem_used"],
                "npu_used": 1.0 if not online else load["npu_used"],
                "support_tasks": SUPPORT_TASKS[node_id],
                "ready_containers": [] if not online else ready_containers.get(node_id, []),
                "network_latency": 1,
            }
        )
    return nodes


def scenario_definitions():
    return [
        ("balanced_low", "全局低负载", "验证低负载下热容器与资源余量的基础选择", lambda v: base_load(0.18, 0.24, 0.16)),
        ("balanced_medium", "全局中等负载", "验证常规稳定运行状态下的调度选择", lambda v: base_load(0.45, 0.48, 0.42)),
        ("rk1_high_npu", "RK3588_1 NPU 高负载", "验证目标检测/分类是否避开单个 NPU 热点", lambda v: with_hotspot("RK3588_1", "npu")),
        ("rk2_high_npu", "RK3588_2 NPU 高负载", "验证同构 RK 节点间的热点迁移能力", lambda v: with_hotspot("RK3588_2", "npu")),
        ("atlas_l_high_npu", "ATLAS_L NPU 高负载", "验证分割/文本任务是否避开 Atlas_L 加速器热点", lambda v: with_hotspot("ATLAS_L", "npu")),
        ("atlas_h_high_npu", "ATLAS_H NPU 高负载", "验证高性能 Atlas 节点繁忙时是否选择备选节点", lambda v: with_hotspot("ATLAS_H", "npu")),
        ("rk1_mem_pressure", "RK3588_1 内存压力", "验证容器冷启动前的内存可行性约束", lambda v: with_hotspot("RK3588_1", "mem")),
        ("rk2_mem_pressure", "RK3588_2 内存压力", "验证同构节点中的内存压力规避", lambda v: with_hotspot("RK3588_2", "mem")),
        ("atlas_l_mem_pressure", "ATLAS_L 内存压力", "验证分割/文本任务在 Atlas 内存压力下的切换", lambda v: with_hotspot("ATLAS_L", "mem")),
        ("atlas_h_mem_pressure", "ATLAS_H 内存压力", "验证高性能节点内存不足时的降级选择", lambda v: with_hotspot("ATLAS_H", "mem")),
        ("rk1_cpu_high", "RK3588_1 CPU 高负载", "验证 CPU 饱和但 NPU 可用时的综合评分", lambda v: with_hotspot("RK3588_1", "cpu")),
        ("rk2_cpu_high", "RK3588_2 CPU 高负载", "验证同构 RK 节点 CPU 热点规避", lambda v: with_hotspot("RK3588_2", "cpu")),
        ("atlas_l_cpu_high", "ATLAS_L CPU 高负载", "验证 Atlas_L CPU 辅助开销对调度的影响", lambda v: with_hotspot("ATLAS_L", "cpu")),
        ("atlas_h_cpu_high", "ATLAS_H CPU 高负载", "验证 Atlas_H CPU 繁忙时的候选切换", lambda v: with_hotspot("ATLAS_H", "cpu")),
        ("rk1_offline", "RK3588_1 离线", "验证单 RK 节点故障下的容错调度", lambda v: with_offline("RK3588_1")),
        ("rk2_offline", "RK3588_2 离线", "验证另一 RK 节点故障下的容错调度", lambda v: with_offline("RK3588_2")),
        ("atlas_l_offline", "ATLAS_L 离线", "验证 Atlas_L 故障下分割/文本任务是否转向 Atlas_H", lambda v: with_offline("ATLAS_L")),
        ("atlas_h_offline", "ATLAS_H 离线", "验证 Atlas_H 故障下分割/文本任务是否转向 Atlas_L", lambda v: with_offline("ATLAS_H")),
        ("all_cold_start", "所有容器冷启动", "验证没有预热容器时纯资源余量与 overhead 的影响", lambda v: base_load(0.28, 0.34, 0.26)),
        ("all_hot_moderate", "多节点容器预热且中等负载", "验证热容器集合中的最优节点选择", lambda v: base_load(0.42, 0.46, 0.40)),
        ("rk_pair_vision_hotspot", "两个 RK 节点视觉任务热点", "验证视觉任务是否转向 Atlas 候选", lambda v: rk_pair_hotspot()),
        ("atlas_pair_ai_hotspot", "两个 Atlas 节点分割/文本热点", "验证 Atlas 专属任务高负载下的最小风险选择", lambda v: atlas_pair_hotspot()),
        ("memory_fragmented", "多节点内存碎片化", "验证存在多个内存临界节点时的可行性过滤", lambda v: fragmented("mem")),
        ("npu_fragmented", "多节点 NPU 碎片化", "验证多个加速器临界节点下的候选过滤", lambda v: fragmented("npu")),
        ("mixed_stress", "CPU/内存/NPU 混合压力", "验证现实混合负载下的综合调度收益", lambda v: mixed_stress(v)),
    ]


def with_hotspot(node, resource):
    loads = base_load(0.32, 0.38, 0.30)
    values = {
        "cpu": {"cpu": 0.88, "mem": 0.46, "npu": 0.34},
        "mem": {"cpu": 0.42, "mem": 0.90, "npu": 0.36},
        "npu": {"cpu": 0.48, "mem": 0.52, "npu": 0.91},
    }[resource]
    set_node(loads, node, **values)
    return loads


def with_offline(node):
    loads = base_load(0.30, 0.36, 0.28)
    set_node(loads, node, online=False)
    return loads


def rk_pair_hotspot():
    loads = base_load(0.34, 0.40, 0.32)
    set_node(loads, "RK3588_1", cpu=0.68, mem=0.62, npu=0.86)
    set_node(loads, "RK3588_2", cpu=0.72, mem=0.66, npu=0.88)
    set_node(loads, "ATLAS_L", cpu=0.30, mem=0.38, npu=0.28)
    set_node(loads, "ATLAS_H", cpu=0.24, mem=0.32, npu=0.22)
    return loads


def atlas_pair_hotspot():
    loads = base_load(0.28, 0.34, 0.26)
    set_node(loads, "ATLAS_L", cpu=0.72, mem=0.70, npu=0.84)
    set_node(loads, "ATLAS_H", cpu=0.66, mem=0.64, npu=0.80)
    return loads


def fragmented(resource):
    loads = base_load(0.34, 0.40, 0.32)
    if resource == "mem":
        set_node(loads, "RK3588_1", cpu=0.42, mem=0.88, npu=0.36)
        set_node(loads, "RK3588_2", cpu=0.38, mem=0.82, npu=0.34)
        set_node(loads, "ATLAS_L", cpu=0.46, mem=0.90, npu=0.40)
        set_node(loads, "ATLAS_H", cpu=0.30, mem=0.44, npu=0.28)
    else:
        set_node(loads, "RK3588_1", cpu=0.50, mem=0.52, npu=0.88)
        set_node(loads, "RK3588_2", cpu=0.46, mem=0.48, npu=0.84)
        set_node(loads, "ATLAS_L", cpu=0.54, mem=0.56, npu=0.90)
        set_node(loads, "ATLAS_H", cpu=0.32, mem=0.38, npu=0.34)
    return loads


def mixed_stress(variant):
    loads = base_load(0.40, 0.44, 0.38)
    patterns = [
        {"RK3588_1": (0.82, 0.50, 0.54), "RK3588_2": (0.42, 0.86, 0.46), "ATLAS_L": (0.58, 0.60, 0.88), "ATLAS_H": (0.24, 0.34, 0.22)},
        {"RK3588_1": (0.35, 0.82, 0.38), "RK3588_2": (0.80, 0.46, 0.58), "ATLAS_L": (0.30, 0.38, 0.26), "ATLAS_H": (0.66, 0.70, 0.84)},
        {"RK3588_1": (0.55, 0.58, 0.86), "RK3588_2": (0.28, 0.36, 0.24), "ATLAS_L": (0.78, 0.54, 0.50), "ATLAS_H": (0.42, 0.88, 0.36)},
        {"RK3588_1": (0.30, 0.34, 0.25), "RK3588_2": (0.62, 0.84, 0.72), "ATLAS_L": (0.86, 0.62, 0.60), "ATLAS_H": (0.48, 0.52, 0.82)},
    ][variant]
    for node, (cpu, mem, npu) in patterns.items():
        set_node(loads, node, cpu=cpu, mem=mem, npu=npu)
    return loads


def generate_states():
    scenarios = scenario_definitions()
    states = []
    index = 1
    for scenario_name, title, objective, load_factory in scenarios:
        for variant in range(4):
            raw_loads = load_factory(variant)
            loads = apply_variant(raw_loads, variant)
            ready = READY_VARIANTS[variant]
            if scenario_name == "all_cold_start":
                ready = {node: [] for node in NODES}
            elif scenario_name == "all_hot_moderate":
                ready = {
                    "RK3588_1": ["object_detection", "image_classification"],
                    "RK3588_2": ["object_detection", "image_classification"],
                    "ATLAS_L": ["object_detection", "image_classification", "semantic_segmentation", "text_classification"],
                    "ATLAS_H": ["object_detection", "image_classification", "semantic_segmentation", "text_classification"],
                }
            states.append(
                {
                    "state_id": f"state_{index:03d}",
                    "description": f"{title}，变体 {variant + 1}，{objective}",
                    "nodes": make_nodes(loads, ready),
                }
            )
            index += 1
    return states


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in STATE_DIR.glob("state_*.json"):
        path.unlink()

    states = generate_states()
    for state in states:
        path = STATE_DIR / f"{state['state_id']}.json"
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(states)} states to {STATE_DIR}")


if __name__ == "__main__":
    main()
