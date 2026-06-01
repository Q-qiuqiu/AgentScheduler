
# docker-scheduler
nku docker-scheduler

# AgentScheduler 单智能体流程

当前仓库保留一个单智能体入口，由智能体直接完成“读取请求 -> 拉取集群和任务上下文 -> 选择任务与目标节点 -> 通过 gateway 启动/复用工具容器 -> 返回结果”的流程，不再拆分 main/sub agent。
Agent 入口现在使用直接执行链路：代码默认调用 `get_cluster_resources` 和 `get_task_catalog`，把用户问题、集群资源、任务目录放入同一个上下文中交给大模型选择。大模型只需要输出一次选择结果，即 `task_type` 和 `target_global_id`，随后代码只调用一次执行工具 `run_task_on_node`。

## 关键文件

- `agent/server/agent.py`：单智能体 HTTP 服务，接口为 `POST /v1/agent/chat`，健康检查为 `GET /health`。
- `config_files/agent_info.json`：单智能体配置，描述可调度工具、工具入口、是否需要文件等任务语义信息。
- `config_files/static_info.json`：集群静态任务部署配置，描述各设备类型支持的任务和调度开销。
- `config_files/latency_profiles.json`：由真实设备采样表转换得到的端到端时延画像，用于给大模型提供当前资源附近的历史参考样本。
- `agent/tools/convert_latency_xlsx.py`：将 `config_files/latency.xlsx` 转换为 `config_files/latency_profiles.json` 的无第三方依赖脚本。
- `agent/tools/mcp_server.py`：本地 MCP 工具入口，提供 `get_cluster_resources`、`get_task_catalog`、`run_task_on_node`。
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

- `get_cluster_resources`
- `get_task_catalog`
- `run_task_on_node`

`schedule_task` 和 `run_task_auto_schedule` 仍可作为 gateway/MCP 低层接口存在，但 Agent HTTP 入口不再使用它们。

## 直接执行 Agent Flow

`agent/server/agent.py` 的 `/v1/agent/chat` 默认执行：

1. 固定调用 `get_cluster_resources`，获取当前节点 `global_id`、设备类型和资源状态。
2. 固定调用 `get_task_catalog`，并按当前在线设备类型过滤可用任务。
3. 先确定用户请求对应的 `task_type`：优先使用请求里的 `task_type`，其次使用本地规则，仍无法判断时再调用一次大模型从任务目录里选择。
4. 从 `latency_profiles.json` 中只为该 `task_type` 和当前在线候选节点匹配 CPU/MEM/NPU 附近的历史采样，计算 `estimated_e2e_ms` 并放入 `latency_references`。
5. 如果只有一个在线候选节点支持该任务，代码直接选择这个节点，不再调用大模型做开发板选择。
6. 如果有多个候选节点，代码会整合成一个 `dispatch_context` JSON 发给大模型，让它基于候选节点和该任务的时延参考输出：

```json
{"task_type":"YoloV5","target_global_id":"..."}
```

7. 代码校验任务是否存在、目标节点是否存在、设备类型是否支持该任务、文件输入是否满足要求。
8. 只调用 `run_task_on_node` 执行任务，不再走 `schedule_task` 或多轮 action 状态机。

时延参考的选择规则：

- `latency.xlsx` 中的 CPU/MEM/NPU 采样按百分比保存。
- gateway 的实时资源如果是 0-1 比例，会在匹配时转换为 0-100 百分比。
- 先确定任务类型，再对当前在线且支持该任务的候选节点默认各取最近 3 条采样。
- 最近样本使用全局加权距离匹配：`CPU=0.5`、`NPU=0.3`、`MEM=0.2`。
- `estimated_e2e_ms` 是该候选节点最近 3 条端到端时间的平均值。
- 发送给开发板选择模型的是一个合并后的 `dispatch_context` JSON，不再重复发送完整 `cluster_resources`、`task_catalog` 和原始采样来源。
- `dispatch_context.candidate_nodes` 只保留选择必需字段：`target_global_id`、`device_type`、当前资源百分比、`estimated_e2e_ms` 和最近 3 条端到端样本；平局时参考 `npu_percent` 和 `mem_percent`。
- prompt 明确要求大模型优先按 `estimated_e2e_ms` 选择开发板；接近时再参考 NPU/MEM 当前负载。

重新生成时延画像：

```bash
python3 agent/tools/convert_latency_xlsx.py
```

可用 `LATENCY_PROFILE_PATH` 覆盖默认画像文件路径。

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
- `EdgeAgentBench/scripts/mock_mcp_server.py`：真实 Agent 评测时注入 bench case 资源状态。
- `EdgeAgentBench/scripts/run_agent_selection_benchmark.py`：请求真实 Agent，只比较最终选择的 device id。

生成 benchmark：

```bash
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py \
  --states-per-model 500 \
  --offline-every 5
```

参数含义：

- `--profile`：时延画像路径，默认 `config_files/latency_profiles.json`。
- `--states-per-model`：每个模型生成多少个资源状态，默认 `500`。
- `--offline-every`：每 N 个 state 人为设置一个节点 offline，默认 `5`，设置为 `0` 表示不生成 offline。

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
EDGE_BENCH_CURRENT_CASE_FILE=/tmp/edgeagentbench_current_case.txt \
MCP_SERVER_PATH=/root/AgentScheduler/EdgeAgentBench/scripts/mock_mcp_server.py \
python3 agent/server/agent.py --host 0.0.0.0 --port 8082
```

另开终端：

```bash
python3 EdgeAgentBench/scripts/run_agent_selection_benchmark.py \
  --agent-url http://127.0.0.1:8082/v1/agent/chat \
  --output EdgeAgentBench/results/agent_selection_results.csv
```

默认会把 `case.task_type` 直接传给 Agent，只测试“当前资源状态下选哪个开发板”。如果想同时测试任务识别，加 `--no-force-task-type`。

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
