#!/usr/bin/env bash
set -euo pipefail

python3 -m venv .venv
source .venv/bin/activate

pip install -U pip
pip install -r requirements.txt

# llama.cpp 서버는 별도 실행되어 있어야 합니다.
# 예: pip install "llama-cpp-python[server]"
#     python -m llama_cpp.server --model /path/to/model.gguf --n_ctx 4096

python agent_openai.py   --base_url "http://127.0.0.1:8000/v1"   --api_key "sk-noauth"   --model "local-llama"   --inventory example_inventory.ini   --task "Apache 또는 Nginx 설치 후 /var/www/html/index.html에 'Hello Dustbox' 콘텐츠 배포, 서비스 활성화"   --workdir workdir   --verify all
