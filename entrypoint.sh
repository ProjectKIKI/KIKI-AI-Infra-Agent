#!/usr/bin/env bash
set -euo pipefail

# 1) llama.cpp 서버 기동 (백그라운드)
echo "[ENTRYPOINT] starting llama.cpp server..."
python -m llama_cpp.server \
  --model "${MODEL_PATH}" \
  --n_ctx "${CTX_SIZE}" \
  --host 0.0.0.0 \
  --port 8000 \
  --verbose &

LLAMA_PID=$!

# 2) 준비될 때까지 대기 (최대 60초)
echo "[ENTRYPOINT] waiting for llama.cpp to be ready at ${BASE_URL} ..."
for i in $(seq 1 60); do
  if curl -fsS "${BASE_URL}/v1/models" >/dev/null 2>&1; then
    echo "[ENTRYPOINT] llama.cpp OK"
    break
  fi
  sleep 1
  if ! kill -0 $LLAMA_PID 2>/dev/null; then
    echo "[ENTRYPOINT] llama.cpp server exited unexpectedly"
    exit 1
  fi
  if [ "$i" -eq 60 ]; then
    echo "[ENTRYPOINT] timeout waiting for llama.cpp"
    exit 1
  fi
done

# 3) 에이전트 실행
echo "[ENTRYPOINT] starting ansible agent..."
exec python /app/agent_openai.py \
  --base_url "${BASE_URL}" \
  --api_key "${API_KEY}" \
  --model    "${MODEL_NAME}" \
  --inventory "${INVENTORY}" \
  --task      "${TASK}" \
  --verify    "${VERIFY}"
