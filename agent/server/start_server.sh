cd /root/AgentScheduler/agent/server || exit 1
python3 /root/AgentScheduler/agent/server/flask_openai_server.py --rkllm_model_path="${RKLLM_MODEL_PATH:-/root/models/Qwen3-1.7B-rk3588-w8a8.rkllm}" --target_platform="${RKLLM_TARGET_PLATFORM:-rk3588}"
