
# docker-scheduler
nku docker-scheduler

# AgentScheduler 单智能体流程

当前仓库保留一个单智能体入口，由智能体直接完成“读取请求 -> 拉取集群和任务上下文 -> 选择任务与目标节点 -> 通过 gateway 启动/复用工具容器 -> 返回结果”的流程，不再拆分 main/sub agent。
Agent 入口现在使用直接执行链路：代码默认调用一次 `get_dispatch_snapshot` 获取集群资源和过滤后的任务目录；如果请求没有明确 `task_type`，保留一次任务类型识别 LLM；开发板选择阶段再给大模型一个极简调度上下文，让它只输出 `task_type` 和 `target_global_id`，随后代码只调用一次执行工具 `run_task_on_node`。

## 关键文件

- `agent/server/agent.py`：单智能体 HTTP 服务，接口为 `POST /v1/agent/chat`，健康检查为 `GET /health`。
- `config_files/agent_info.json`：单智能体配置，描述可调度工具、工具入口、是否需要文件等任务语义信息。
- `config_files/static_info.json`：集群静态任务部署配置，描述各设备类型支持的任务和调度开销。
- `config_files/dispatch_history.json`：Agent 在线执行后记录的历史调度样本，包含任务、节点、资源状态和端到端时延。
- `config_files/latency_profiles.json`：由真实设备采样表转换得到的端到端时延画像，主要用于生成 EdgeAgentBench 和刷新低负载时延先验。
- `agent/tools/convert_latency_xlsx.py`：将 `config_files/latency.xlsx` 转换为 `config_files/latency_profiles.json` 的无第三方依赖脚本。
- `agent/tools/mcp_server.py`：本地 MCP 工具入口，提供 `get_dispatch_snapshot`、`get_cluster_resources`、`get_task_catalog`、`run_task_on_node`。
- `src/gateway/HttpServer.cpp`：gateway 仍负责 `/quest`、`/quest_on_node`、`/schedule_task`、`/cluster_resources`、`/register_node` 等接口。

## Gateway 调度接口

只获取调度结果，不启动工具容器：

```bash
curl -X POST "http://127.0.0.1:6666/schedule_task?taskid=YoloV5"
```

自动调度并执行工具：

```bash
curl -F "image=@/path/to/image.jpg" \
     -X POST "http://127.0.0.1:6666/quest?taskid=YoloV5&real_url=predict"
```

Agent 当前使用的 MCP 工具为：

- `get_dispatch_snapshot`
- `run_task_on_node`

`schedule_task` 和 `run_task_auto_schedule` 仍可作为 gateway/MCP 低层接口存在，但 Agent HTTP 入口不再使用它们。

## 直接执行 Agent Flow

`agent/server/agent.py` 的 `/v1/agent/chat` 默认执行：

1. 固定调用一次 `get_dispatch_snapshot`，同时获取当前节点 `global_id`、设备类型、资源状态和按在线设备类型过滤后的任务目录。
2. 先确定用户请求对应的 `task_type`：优先使用请求里的 `task_type`，其次使用本地规则，仍无法判断时再调用一次大模型从任务目录里选择。
3. 从 `config_files/static_info.json` 读取该 `task_type` 在各开发板上的 `taskOverhead`，和当前资源一起构造成候选节点上下文。
4. 如果只有一个在线候选节点支持该任务，代码直接选择这个节点，不再调用大模型做开发板选择。
5. 如果有多个候选节点，代码会整合成一个 `dispatch_context` JSON 发给大模型，让它基于当前资源和打分公式输出：

```json
{"task_type":"YoloV5","target_global_id":"..."}
```

6. 代码校验任务是否存在、目标节点是否存在、设备类型是否支持该任务、文件输入是否满足要求。
7. 只调用 `run_task_on_node` 执行任务，不再走 `schedule_task` 或多轮 action 状态机。

开发板选择上下文：

- 不再向大模型提供 `latency_profiles.json` 中的相近采样，避免用 bench GT 同源数据测试 Agent。
- gateway 的实时资源如果是 0-1 比例，会在上下文里转换为 0-100 百分比。
- 发给大模型的 `dispatch_context` 只保留 `task_type`、`policy`、`columns` 和 `nodes`。
- `nodes` 使用数组压缩，列含义固定为 `[id, base, cpu, npu, mem]`，其中资源值是百分比。
- `base_latency_ms` 写在 `config_files/static_info.json` 的 `taskOverhead` 中，由真实采样里 CPU/MEM/NPU 总占用最低的记录作为该任务在该开发板上的低负载端到端时延先验。
- prompt 要求大模型在所有在线且支持任务的候选中选择分数最小的开发板，分数公式为：

```text
score = 3*base + 1*cpu + 80*npu + 200*mem
```

- 这个整数公式和离线 scheduler 的 `0.0003*base + 0.01*cpu_ratio + 0.8*npu_ratio + 2.0*mem_ratio` 等价；Agent 上下文中的 `cpu`、`npu`、`mem` 是百分比，因此资源权重除以 100 后对应离线 ratio 权重。
- 权重直接写在公式里告诉大模型：低负载端到端先验为 `3`，CPU 为 `1`，NPU 为 `80`，MEM 为 `200`。
- 这组权重由 `EdgeAgentBench/scripts/optimize_scheduler_weights.py` 在当前 bench 上按平均 `predicted_ete_ms` 搜索得到；当前全量 12450 个 case 的 runner 复现结果为 `average_predicted_ete_ms=9592.809`、`accuracy=0.7443`。
- `cpu`、`mem`、`npu` 直接使用当前资源百分比；bench 资源值已经是用于调度的状态，不再叠加任务 overhead。
- 如果计算后的资源超过 100%，大模型也不能过滤候选；实际执行时可能通过内存回收或 CPU 共用继续运行。

历史记录驱动选择：

- 每次 `run_task_on_node` 返回端到端时延字段后，Agent 会把本次 `task_type`、`target_global_id`、`device_type`、当前 CPU/MEM/NPU 百分比和 `ete_ms` 写入 `config_files/dispatch_history.json`。
- 历史文件按 `task_type -> device_type` 分桶记录，查找时只扫描当前任务和当前平台，避免所有样本堆在一个列表里。
- 同一任务、同一平台、同一资源状态已存在时，不追加新记录；如果新的 `ete_ms` 不同，会更新已有记录的 `ete_ms` 和时间戳。
- 下一次请求到达时，Agent 会先扫描历史记录。只有所有候选节点都有相近历史时，才启用历史上下文。
- 严格历史模式下，Agent 只发送每个开发板的历史 `latency_ms`，不再发送资源信息和权重公式。
- 如果任意候选节点缺少相近历史，Agent 会回退到普通权重公式上下文。
- 历史开关默认开启；做无历史消融实验时可以设置 `DISPATCH_HISTORY_ENABLED=0`。
- 调度 prompt 默认包含当前资源状态；做无资源状态消融实验时可以设置 `DISPATCH_INCLUDE_RESOURCE_STATE=0`，此时保留节点 ID 和基础时延，不发送当前 CPU/NPU/MEM。
- 默认相近阈值是 CPU/MEM/NPU 三个指标各自最大偏差不超过 `1.0` 个百分点，可用环境变量调整：

```bash
DISPATCH_HISTORY_ENABLED=1
DISPATCH_HISTORY_TOLERANCE_PERCENT=1.0
DISPATCH_HISTORY_PATH=/root/AgentScheduler/config_files/dispatch_history.json
DISPATCH_HISTORY_MAX_RECORDS=5000
DISPATCH_INCLUDE_RESOURCE_STATE=1
```

所有候选都有历史时，发给大模型的是：

```json
{
  "task_type": "YoloV5",
  "history_policy": "choose min latency_ms",
  "history_coverage": "full",
  "columns": ["id", "latency_ms"],
  "nodes": [["RK3588", 239.28], ["ATLAS_H", 2036.36]]
}
```

可从当前 EdgeAgentBench cases 生成一份完整历史记录，用于测试历史命中时的 Agent 表现：

```bash
python3 EdgeAgentBench/scripts/build_dispatch_history_from_cases.py \
  --output config_files/dispatch_history.json
```

重新生成时延画像：

```bash
python3 agent/tools/convert_latency_xlsx.py
```

`latency_profiles.json` 现在主要用于生成 EdgeAgentBench 的状态和 ground truth，不进入 Agent 开发板选择上下文。

## 单智能体接口示例

图片工具：

```bash
curl -F "text=检测图片里的物体" \
     -F "image=@/path/to/image.jpg" \
     -F "debug=true" \
     http://127.0.0.1:8082/v1/agent/chat
```

文本工具：

```bash
curl -F "text=请对这句话做文本分类：这家店体验很好" \
     -F "debug=true" \
     http://127.0.0.1:8082/v1/agent/chat
```

可以用 `task_type` 强制指定工具，例如 `YoloV5`、`MobileNet`、`ResNet50`、`Bert`、`deeplabv3`。

## EdgeAgentBench 调度评测

当前 bench 使用 `config_files/latency_profiles.json` 中的真实端到端时延画像生成 cases，并用 `ground_truth.node_id` 表示当前资源状态下真实 `e2e_ms` 最短的开发板。

核心脚本：

- `EdgeAgentBench/scripts/generate_latency_profile_bench.py`：从 `latency_profiles.json` 生成 states/cases。
- `EdgeAgentBench/scripts/run_benchmark.py`：离线评测 scheduler 和 baseline policy。
- `EdgeAgentBench/scripts/mock_case_server.py`：真实 Agent 仿真时维护当前 bench case。
- `EdgeAgentBench/scripts/mock_mcp_server.py`：真实 Agent 评测时注入 bench case 资源状态。
- `EdgeAgentBench/scripts/run_agent_selection_benchmark.py`：请求真实 Agent，只比较最终选择的 device id。

生成 benchmark：

```bash
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py \
  --states-per-model 500 \
  --offline-every 5 \
  --warm-every 3
```

参数含义：

- `--profile`：时延画像路径，默认 `config_files/latency_profiles.json`。
- `--states-per-model`：每个模型生成多少个资源状态，默认 `500`。
- `--offline-every`：每 N 个 state 人为设置一个节点 offline，默认 `5`，设置为 `0` 表示不生成 offline。
- `--warm-every`：每 N 个设备位置设置一个已启动容器节点，默认 `3`，设置为 `0` 表示不生成 warm container 状态。

测试当前 scheduler：

```bash
python3 EdgeAgentBench/scripts/run_benchmark.py \
  --policy scheduler \
  --output EdgeAgentBench/results/scheduler_results.csv
```

常用 policy：

- `ground_truth`：直接读取 GT，应该 100%，用于 sanity check。
- `scheduler`：模拟当前 gateway 调度公式。
- `resource_average`：选择 CPU/MEM/NPU 平均负载最低的节点。
- `cpu_min`、`npu_min`、`mem_min`：单资源最低 baseline。
- `round_robin`：按 `RK3588 -> ATLAS_L -> ATLAS_H` 轮询。
- `random`：随机 baseline，可用 `--seed` 固定随机性。

runner 常用参数：

- `--case-dir`：case 目录，默认 `EdgeAgentBench/cases`。
- `--case-pattern`：case 文件匹配模式，默认 `*.json`，例如 `yolov5_*.json`。
- `--ignore-constraints`：忽略 query 中的节点 require/exclude 约束。
- `--output`：CSV 输出路径。
- `--summary-output`：summary JSON 输出路径，默认由 CSV 路径自动推导。
- `--failures`：最多打印多少条失败样例。
- `--strict`：存在失败时返回非 0 exit code。

更完整的 case 格式和脚本说明见 `EdgeAgentBench/README.md`。

测试真实 Agent 的开发板选择：

```bash
python3 EdgeAgentBench/scripts/mock_case_server.py \
  --host 127.0.0.1 \
  --port 18082 \
  --case-dir EdgeAgentBench/cases
```

另开终端启动 Agent：

```bash
EDGE_BENCH_CASE_SERVER_URL=http://127.0.0.1:18082 \
MCP_SERVER_PATH=/root/AgentScheduler/EdgeAgentBench/scripts/mock_mcp_server.py \
python3 agent/server/agent.py --host 0.0.0.0 --port 8082
```

再开一个终端运行评测：

```bash
python3 EdgeAgentBench/scripts/run_agent_selection_benchmark.py \
  --agent-url http://127.0.0.1:8082/v1/agent/chat \
  --case-server-url http://127.0.0.1:18082 \
  --output EdgeAgentBench/results/agent_selection_results.csv
```

runner 会先把当前 case_id 设置到 mock case server，mock MCP 再读取同一个 case 返回 cluster resource，因此传给大模型和 mock server 的资源状态保持一致。默认会把 `case.task_type` 直接传给 Agent，只测试“当前资源状态下选哪个开发板”。如果想同时测试任务识别，加 `--no-force-task-type`。

#docker API

docker接口文档:https://docs.docker.com/reference/api/engine/version/v1.47/

docker的sdk文档:https://docs.docker.com/reference/api/engine/

# 第三方库
http库 https://github.com/yhirose/cpp-httplib

json库 https://github.com/nlohmann/json/

# 目前工作

## 实现接口
### Container 

```
// 详细接口、参数返回值 参考函数定义上注解
CreateContainer
StartContainer
StopContainer
PauseContainer
UnpauseContainer
```

## 修改docker默认配置使其可以被外部访问
```
// 编辑启动项

vi /usr/lib/systemd/system/docker.service
    
// 将启动配置文件添加如下选项
ExecStart=/usr/bin/dockerd -H tcp://0.0.0.0:2375 -H unix:///var/run/docker.sock

// 重启docker
systemctl daemon-reload
systemctl restart docker

// 使用下面命令开启防火墙
firewall-cmd --permanent --add-port=2375/tcp
firewall-cmd --reload
firewall-cmd --list-ports // 验证
可以查看所有的镜像
curl --unix-socket /var/run/docker.sock http://localhost:2375/v1.44/images/json
curl 192.168.134.144:2375/v1.44/images/json
```

## 其他
### Docker 启动yolov5命令
```
docker run -itd --privileged \
-e ASCEND_VISIBLE_DEVICES=0 \
-e ASCEND_ALLOW_LINK=True \
--device=/dev/svm0 \
--device=/dev/ts_aisle \
--device=/dev/upgrade \
--device=/dev/sys \
--device=/dev/vdec \
--device=/dev/vpc \
--device=/dev/pngd \
--device=/dev/venc \
--device=/dev/dvpp_cmdlist \
--device=/dev/log_drv \
-v /etc/sys_version.conf:/etc/sys_version.conf \
-v /etc/hdcBasic.cfg:/etc/hdcBasic.cfg \
-v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
-v /usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64 \
-v /usr/lib64/aicpu_kernels/:/usr/lib64/aicpu_kernels/ \
-v /var/slogd:/var/slogd \
-v /var/dmp_daemon:/var/dmp_daemon \
-v /usr/lib64/libaicpu_processer.so:/usr/lib64/libaicpu_processer.so \
-v /usr/lib64/libaicpu_prof.so:/usr/lib64/libaicpu_prof.so \
-v /usr/lib64/libaicpu_sharder.so:/usr/lib64/libaicpu_sharder.so \
-v /usr/lib64/libadump.so:/usr/lib64/libadump.so \
-v /usr/lib64/libtsd_eventclient.so:/usr/lib64/libtsd_eventclient.so \
-v /usr/lib64/libaicpu_scheduler.so:/usr/lib64/libaicpu_scheduler.so \
-v /usr/lib64/libdcmi.so:/usr/lib64/libdcmi.so \
-v /usr/lib64/libmpi_dvpp_adapter.so:/usr/lib64/libmpi_dvpp_adapter.so \
-v /usr/lib64/libstackcore.so:/usr/lib64/libstackcore.so \
-v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
-v /etc/ascend_install.info:/etc/ascend_install.info \
-v /var/log/ascend_seclog:/var/log/ascend_seclog \
-v /var/davinci/driver:/var/davinci/driver \
-v /usr/lib64/libc_sec.so:/usr/lib64/libc_sec.so \
-v /usr/lib64/libdevmmap.so:/usr/lib64/libdevmmap.so \
-v /usr/lib64/libdrvdsmi.so:/usr/lib64/libdrvdsmi.so \
-v /usr/lib64/libslog.so:/usr/lib64/libslog.so \
-v /usr/lib64/libmmpa.so:/usr/lib64/libmmpa.so \
-v /usr/lib64/libascend_hal.so:/usr/lib64/libascend_hal.so \
-v /usr/local/Ascend/ascend-toolkit:/usr/local/Ascend/ascend-toolkit \
--name yolov5-infer \
-p 8080:80 \
yolov5-infer
```
### curl发送测试请求
```
http://ip:8080/predict，请求体为form-data，key为image，value为文件
curl -F "image=@/root/lxs/soccer.jpg" -X POST localhost:8080/predict

curl -F "key=@[文件地址]" -F "key=value" -X POST https://reqbin.com/echo/post
```
