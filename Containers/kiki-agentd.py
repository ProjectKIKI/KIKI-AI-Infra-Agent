#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KIKI Agent Daemon

역할:
  - OpenAI 호환 /v1/chat/completions 엔드포인트 제공
    → kiki --base-url http://agentd:8082 로 들어온 요청 처리
    → 내부에서 실제 LLM 서버(KIKI_UPSTREAM_LLM_BASE_URL)로 프록시

  - /api/v1/generate 엔드포인트 제공
    → 자연어 + target(ansible/k8s/osp/heat)를 받아
      target별 system prompt를 구성 후 upstream LLM 호출,
      YAML/Ansible/Heat 템플릿 문자열 반환

SYSTEM PROMPT 우선순위 (target = ansible/k8s/osp/heat):
  1) 환경 변수: KIKI_SYSTEM_PROMPT_<TARGET>   (예: KIKI_SYSTEM_PROMPT_ANSIBLE)
  2) 프롬프트 파일: KIKI_SYSTEM_PROMPT_FILE   (예: /etc/kiki/prompts.yaml)
     - YAML 형식 예:
       ansible: |
         You are an Ansible playbook generator...
       k8s: |
         You are a K8s-focused Ansible generator...
  3) 코드 내 DEFAULT_SYSTEM_PROMPTS[target]

환경 변수:
  - KIKI_UPSTREAM_LLM_BASE_URL : 실제 LLM 서버 base URL (예: http://127.0.0.1:8000)
  - KIKI_LLM_MODEL             : upstream 모델 이름 (기본값: local-model)
  - KIKI_LLM_API_KEY           : 필요 시 Authorization 헤더에 사용
  - KIKI_SYSTEM_PROMPT_FILE    : YAML 프롬프트 파일 경로
  - KIKI_SYSTEM_PROMPT_<TARGET>: per-target prompt override (ex: KIKI_SYSTEM_PROMPT_ANSIBLE)
"""

import os
import json
import re
from typing import Optional, Dict

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import textwrap

# optional dependencies
try:
    import requests  # type: ignore
except ImportError:
    requests = None

try:
    import yaml  # type: ignore
except ImportError:
    yaml = None


app = FastAPI(
    title="KIKI Agent Daemon",
    version="1.1.0",
    description="OpenAI-compatible proxy + infra code generator with external system prompts",
)

# ─────────────────────────────────────────────
# 기본 SYSTEM PROMPT 정의 (fallback)
# ─────────────────────────────────────────────

DEFAULT_SYSTEM_PROMPTS: Dict[str, str] = {
    "ansible": textwrap.dedent("""
        You are an Ansible playbook generator.
        Output ONLY valid YAML for a complete Ansible playbook.
        No markdown fences, no explanations, no comments or notes.
        Use idempotent modules. YAML only.
        Never wrap the YAML in any kind of markdown code fences such as ``` or ```yaml.
    """).strip(),
    "k8s": textwrap.dedent("""
        You are an Ansible playbook generator for Kubernetes.
        Output ONLY valid YAML for a complete Ansible playbook.
        Use kubernetes.core.k8s (and related) modules to manage Kubernetes resources.
        No markdown fences, no explanations, YAML only.
        Never wrap the YAML in any kind of markdown code fences such as ``` or ```yaml.
    """).strip(),
    "osp": textwrap.dedent("""
        You are an Ansible playbook generator for OpenStack.
        Output ONLY valid YAML for a complete Ansible playbook.
        Use openstack.cloud collection modules instead of legacy os_* modules.
        No markdown fences, no explanations, YAML only.
        Never wrap the YAML in any kind of markdown code fences such as ``` or ```yaml.
    """).strip(),
    "heat": textwrap.dedent("""
        You are an OpenStack Heat template generator.
        Output ONLY a single Heat template as valid YAML.
        Include heat_template_version, description, parameters, resources, and outputs.
        No markdown fences, no explanations, YAML only.
        Never wrap the YAML in any kind of markdown code fences such as ``` or ```yaml.
    """).strip(),
}

_PROMPT_FILE_CACHE: Optional[Dict[str, str]] = None  # lazy load


# ─────────────────────────────────────────────
# 유틸 함수들
# ─────────────────────────────────────────────

def _ensure_requests():
    if requests is None:
        raise RuntimeError(
            "requests 모듈이 없습니다. 컨테이너/가상환경에 'pip install requests' 를 추가하세요."
        )


def _normalize_upstream_url(base: str) -> str:
    """
    base에 이미 /v1/chat/completions 같은 경로가 있으면 그대로 사용.
    없으면 /v1/chat/completions 붙인다.
    """
    if re.search(r"/v\d+/", base):
        return base
    return base.rstrip("/") + "/v1/chat/completions"


def load_prompt_file() -> Dict[str, str]:
    """
    KIKI_SYSTEM_PROMPT_FILE 환경 변수에 지정된 YAML 파일에서
    target별 system prompt를 읽어온다.

    예시 YAML:
      ansible: |
        You are an Ansible playbook generator...
      k8s: |
        You are a K8s Ansible generator...
    """
    global _PROMPT_FILE_CACHE

    if _PROMPT_FILE_CACHE is not None:
        return _PROMPT_FILE_CACHE

    path = os.environ.get("KIKI_SYSTEM_PROMPT_FILE")
    if not path:
        _PROMPT_FILE_CACHE = {}
        return _PROMPT_FILE_CACHE

    if yaml is None:
        print("[KIKI][WARN] KIKI_SYSTEM_PROMPT_FILE 설정됨, 하지만 'yaml' 모듈이 없어 무시합니다.")
        _PROMPT_FILE_CACHE = {}
        return _PROMPT_FILE_CACHE

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        result: Dict[str, str] = {}
        if isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, str):
                    result[key.lower()] = val.strip()
        _PROMPT_FILE_CACHE = result
        print(f"[KIKI] Loaded system prompts from file: {path} (keys: {list(result.keys())})")
        return _PROMPT_FILE_CACHE
    except FileNotFoundError:
        print(f"[KIKI][WARN] KIKI_SYSTEM_PROMPT_FILE='{path}' 를 찾을 수 없습니다.")
    except Exception as e:
        print(f"[KIKI][WARN] KIKI_SYSTEM_PROMPT_FILE 로딩 실패: {e}")

    _PROMPT_FILE_CACHE = {}
    return _PROMPT_FILE_CACHE


def get_system_prompt_for_target(target: str) -> str:
    """
    SYSTEM PROMPT 우선순위:
      1) env KIKI_SYSTEM_PROMPT_<TARGET> (예: KIKI_SYSTEM_PROMPT_ANSIBLE)
      2) KIKI_SYSTEM_PROMPT_FILE YAML 내 정의 (키: ansible/k8s/osp/heat)
      3) DEFAULT_SYSTEM_PROMPTS[target] (없으면 ansible 기본값)
    """
    key = target.lower()

    # 1) 환경 변수 per target
    env_name = f"KIKI_SYSTEM_PROMPT_{key.upper()}"
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val.strip()

    # 2) 파일 기반 프롬프트
    file_prompts = load_prompt_file()
    if key in file_prompts:
        return file_prompts[key]

    # 3) 코드 기본값
    if key in DEFAULT_SYSTEM_PROMPTS:
        return DEFAULT_SYSTEM_PROMPTS[key]

    # fallback: ansible 기본
    return DEFAULT_SYSTEM_PROMPTS["ansible"]


def call_upstream_chat(body: bytes) -> dict:
    """
    OpenAI /v1/chat/completions 요청을 그대로 upstream 에 포워딩.
    """
    _ensure_requests()

    upstream_base = os.environ.get("KIKI_UPSTREAM_LLM_BASE_URL", "http://127.0.0.1:8000")
    upstream_url = _normalize_upstream_url(upstream_base)

    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("KIKI_LLM_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = requests.post(upstream_url, headers=headers, data=body, timeout=600)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=502, detail=f"Upstream 응답 JSON 파싱 실패: {resp.text}")


def call_upstream_with_prompt(model: str, system_prompt: str, user_prompt: str) -> str:
    """
    /api/v1/generate 용: system+user prompt로 upstream LLM 호출.
    """
    _ensure_requests()

    upstream_base = os.environ.get("KIKI_UPSTREAM_LLM_BASE_URL", "http://127.0.0.1:8000")
    upstream_url = _normalize_upstream_url(upstream_base)

    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("KIKI_LLM_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    resp = requests.post(upstream_url, headers=headers, data=json.dumps(payload), timeout=600)
    if resp.status_code >= 400:
        raise RuntimeError(f"Upstream LLM 오류: {resp.status_code} {resp.text}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError(f"Upstream 응답 포맷 이상: {data}")


# ─────────────────────────────────────────────
# 모델 정의 (/api/v1/generate)
# ─────────────────────────────────────────────

class GenerateRequest(BaseModel):
    prompt: str
    target: str = "ansible"     # ansible | k8s | osp | heat
    inventory: Optional[str] = None
    verify: str = "none"        # none | syntax | all


class GenerateResponse(BaseModel):
    target: str
    yaml: str


# ─────────────────────────────────────────────
# 헬스체크
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "kiki-agentd"}


# ─────────────────────────────────────────────
# OpenAI 호환: /v1/chat/completions
# ─────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    kiki --base-url http://agentd:8082 로 들어오는 OpenAI chat/completions 요청을
    실제 LLM 서버(KIKI_UPSTREAM_LLM_BASE_URL)로 프록시한다.
    """
    body = await request.body()
    result = call_upstream_chat(body)
    return result


# ─────────────────────────────────────────────
# 구조화된 API: /api/v1/generate
# ─────────────────────────────────────────────

@app.post("/api/v1/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest):
    """
    자연어 + target에 맞는 system prompt를 구성해 upstream LLM 호출.
    결과는 YAML/Ansible/Heat 템플릿 텍스트.
    """

    model = os.environ.get("KIKI_LLM_MODEL", "local-model")

    # 1) 외부 설정 (env/file/기본값)에서 target별 system prompt 가져오기
    system_prompt = get_system_prompt_for_target(req.target)

    # 2) verify 수준에 따라 조건 추가
    if req.verify in ("syntax", "all"):
        system_prompt += "\n- The YAML must be syntactically valid."
    if req.verify == "all":
        system_prompt += "\n- Make resources idempotent and follow best practices."

    # 3) 추가 컨텍스트 구성
    extra_ctx = []
    if req.inventory:
        extra_ctx.append(f"Inventory context: {req.inventory}")
    if req.target != "heat":
        extra_ctx.append("Return a full Ansible Playbook YAML.")
    else:
        extra_ctx.append("Return only the Heat template YAML.")

    user_prompt = req.prompt
    if extra_ctx:
        user_prompt = user_prompt + "\n\n[Context]\n" + "\n".join(extra_ctx)

    try:
        yaml_text = call_upstream_with_prompt(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return GenerateResponse(target=req.target, yaml=yaml_text)