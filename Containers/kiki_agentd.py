#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KIKI Agent Daemon (Auth + History + RAG)

역할:
  - OpenAI 호환 /v1/chat/completions 엔드포인트 제공
    → kiki --base-url http://agentd:8082 로 들어온 요청 처리
    → 내부에서 실제 LLM 서버(KIKI_UPSTREAM_LLM_BASE_URL)로 프록시

  - /api/v1/generate 엔드포인트 제공
    → 자연어 + target(ansible/k8s/osp/heat)를 받아
      target별 system prompt를 구성 후 upstream LLM 호출,
      YAML/Ansible/Heat 템플릿 문자열 반환

  - /api/v1/register, /api/v1/login, /api/v1/history, /api/v1/history/summary 제공
    → kiki login / kiki history 와 연동
    → SQLite 기반으로 사용자 / 세션 / 명령 로그 관리

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
  - KIKI_AGENT_DB_PATH         : SQLite DB 경로 (기본: /app/data/kiki_agent.db)
"""

import os
import json
import re
import sqlite3
from datetime import datetime
from typing import Optional, Dict, List

import textwrap

from fastapi import FastAPI, HTTPException, Request, Depends, Header
from pydantic import BaseModel

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
    version="1.2.0-auth-rag",
    description="OpenAI-compatible proxy + infra code generator + auth/history/RAG",
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

_DB_PATH_DEFAULT = "/app/data/kiki_agent.db"
DB_PATH = os.environ.get("KIKI_AGENT_DB_PATH", _DB_PATH_DEFAULT)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS command_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            command_type TEXT NOT NULL,
            prompt       TEXT NOT NULL,
            target       TEXT,
            created_at   TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )
    conn.commit()
    conn.close()


def hash_password(password: str) -> str:
    import hashlib

    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_user(username: str, password: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, hash_password(password), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def authenticate_user(username: str, password: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    if row["password_hash"] != hash_password(password):
        return None
    return row


def create_session(user_id: int) -> str:
    import secrets

    token = secrets.token_hex(32)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return token


def get_user_by_token(token: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.id, u.username
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token = ?
        """,
        (token,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row["id"], "username": row["username"]}


def log_command(user_id: int, command_type: str, prompt: str, target: Optional[str] = None) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO command_logs (user_id, command_type, prompt, target, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, command_type, prompt, target, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_recent_logs(user_id: int, limit: int = 20):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, command_type, prompt, target, created_at
        FROM command_logs
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def build_rag_context(user_id: int, query: str, limit: int = 5) -> str:
    """
    간단한 CPU 기반 "RAG 스타일" 검색 구현:
      - 최근 50개 로그에서 query와 단어 겹치는 수로 점수 계산
      - 상위 N개를 context 텍스트로 반환
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT command_type, prompt, target, created_at
        FROM command_logs
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return ""

    q_words = set(query.lower().split())
    scored = []
    for r in rows:
        text = (r["prompt"] or "").lower()
        words = set(text.split())
        score = len(q_words & words)
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [r for score, r in scored if score > 0][:limit]
    if not top:
        top = [r for score, r in scored][:limit]

    lines = []
    for r in top:
        lines.append(
            f"- [{r['created_at']}] ({r['target'] or r['command_type']}) {r['prompt']}"
        )
    return "\n".join(lines)


def get_current_user(x_kiki_user_token: Optional[str] = Header(None)):
    """
    X-KIKI-USER-TOKEN 헤더를 통해 현재 로그인한 사용자 조회.
    토큰이 없으면 None 반환, 토큰이 잘못되면 401.
    """
    if not x_kiki_user_token:
        return None
    user = get_user_by_token(x_kiki_user_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired user token")
    return user


# 앱 시작 시 DB 초기화
init_db()


# ─────────────────────────────────────────────
# DB (사용자 / 세션 / 명령 로그)
# ─────────────────────────────────────────────

_DB_PATH_DEFAULT = "/app/data/kiki_agent.db"
DB_PATH = os.environ.get("KIKI_AGENT_DB_PATH", _DB_PATH_DEFAULT)


def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS command_logs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            command_type TEXT NOT NULL,
            prompt       TEXT NOT NULL,
            target       TEXT,
            created_at   TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )
    conn.commit()
    conn.close()


def hash_password(password: str) -> str:
    import hashlib

    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def create_user(username: str, password: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, hash_password(password), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def authenticate_user(username: str, password: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    if row["password_hash"] != hash_password(password):
        return None
    return row


def create_session(user_id: int) -> str:
    import secrets

    token = secrets.token_hex(32)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return token


def get_user_by_token(token: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.id, u.username
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token = ?
        """,
        (token,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row["id"], "username": row["username"]}


def log_command(user_id: int, command_type: str, prompt: str, target: Optional[str] = None) -> None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO command_logs (user_id, command_type, prompt, target, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user_id, command_type, prompt, target, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_recent_logs(user_id: int, limit: int = 20):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, command_type, prompt, target, created_at
        FROM command_logs
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def build_rag_context(user_id: int, query: str, limit: int = 5) -> str:
    """간단한 CPU 기반 RAG 스타일: 최근 50개 로그에서 query와 단어 겹침 개수 기준으로 상위 N개 선택."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT command_type, prompt, target, created_at
        FROM command_logs
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return ""

    q_words = set(query.lower().split())
    scored: List = []
    for r in rows:
        text = (r["prompt"] or "").lower()
        words = set(text.split())
        score = len(q_words & words)
        scored.append((score, r))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [r for score, r in scored if score > 0][:limit]
    if not top:
        top = [r for score, r in scored][:limit]

    lines = []
    for r in top:
        lines.append(f"- [{r['created_at']}] ({r['target'] or r['command_type']}) {r['prompt']}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# SYSTEM PROMPT 로딩
# ─────────────────────────────────────────────


def load_prompt_file() -> Dict[str, str]:
    """KIKI_SYSTEM_PROMPT_FILE 환경 변수에 지정된 YAML 파일에서 target별 system prompt 로드."""
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
    """SYSTEM PROMPT 우선순위: env → 파일 → DEFAULT_SYSTEM_PROMPTS."""
    key = target.lower()

    # 1) per-target env
    env_name = f"KIKI_SYSTEM_PROMPT_{key.upper()}"
    env_val = os.environ.get(env_name)
    if env_val:
        return env_val.strip()

    # 2) 파일 기반
    file_prompts = load_prompt_file()
    if key in file_prompts:
        return file_prompts[key]

    # 3) 기본값
    if key in DEFAULT_SYSTEM_PROMPTS:
        return DEFAULT_SYSTEM_PROMPTS[key]

    return DEFAULT_SYSTEM_PROMPTS["ansible"]


# ─────────────────────────────────────────────
# Upstream 호출 유틸
# ─────────────────────────────────────────────


def _ensure_requests():
    if requests is None:
        raise RuntimeError("requests 모듈이 없습니다. 컨테이너/가상환경에 'pip install requests' 를 추가하세요.")


def _normalize_upstream_url(base: str) -> str:
    """base에 이미 /v1/chat/completions 경로가 있으면 그대로, 아니면 붙인다."""
    if re.search(r"/v\d+/", base):
        return base
    return base.rstrip("/") + "/v1/chat/completions"


def call_upstream_chat(body: bytes) -> dict:
    """OpenAI /v1/chat/completions 요청을 그대로 upstream 에 포워딩."""
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
    """/api/v1/generate 용: system+user prompt로 upstream LLM 호출."""
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
# Pydantic 모델
# ─────────────────────────────────────────────


class GenerateRequest(BaseModel):
    prompt: str
    target: str = "ansible"     # ansible | k8s | osp | heat
    inventory: Optional[str] = None
    verify: str = "none"        # none | syntax | all


class GenerateResponse(BaseModel):
    target: str
    yaml: str

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class HistoryItem(BaseModel):
    id: int
    command_type: str
    prompt: str
    target: Optional[str] = None
    created_at: str


class HistoryResponse(BaseModel):
    items: List[HistoryItem]



class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class HistoryItem(BaseModel):
    id: int
    command_type: str
    prompt: str
    target: Optional[str] = None
    created_at: str


class HistoryResponse(BaseModel):
    items: List[HistoryItem]


# ─────────────────────────────────────────────
# 현재 사용자 확인 (헤더 기반)
# ─────────────────────────────────────────────


def get_current_user(x_kiki_user_token: Optional[str] = Header(None)):
    """X-KIKI-USER-TOKEN 헤더를 통해 로그인 사용자 조회. 없으면 None, 잘못되면 401."""
    if not x_kiki_user_token:
        return None
    user = get_user_by_token(x_kiki_user_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired user token")
    return user


# 앱 시작 시 DB 초기화
init_db()


# ─────────────────────────────────────────────
# Auth / History API
# ─────────────────────────────────────────────


@app.post("/api/v1/register")
async def register(req: RegisterRequest):
    try:
        create_user(req.username, req.password)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Username already exists")
    return {"status": "ok", "username": req.username}


@app.post("/api/v1/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    user = authenticate_user(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_session(user["id"])
    return LoginResponse(access_token=token)


@app.get("/api/v1/history", response_model=HistoryResponse)
async def get_history(limit: int = 20, current_user=Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="User token is required")
    rows = get_recent_logs(current_user["id"], limit=limit)
    items = [
        HistoryItem(
            id=r["id"],
            command_type=r["command_type"],
            prompt=r["prompt"],
            target=r["target"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return HistoryResponse(items=items)


@app.get("/api/v1/history/summary")
async def get_history_summary(limit: int = 20, current_user=Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="User token is required")
    rows = get_recent_logs(current_user["id"], limit=limit)
    if not rows:
        return {"summary": "아직 기록된 명령이 없습니다."}

    bullet_lines = []
    for r in rows:
        bullet_lines.append(
            f"- [{r['created_at']}] ({r['command_type']} / {r['target'] or '-'}) {r['prompt']}"
        )

    history_text = "\n".join(bullet_lines)

    system_prompt = (
        "You are an assistant that summarizes a user's past infrastructure automation commands.\n"
        "Explain in Korean, in 3~6 bullet points, what kind of work this user has done.\n"
        "Be concise and high-level (e.g., 'OpenStack 프로젝트/네트워크 생성', 'Kubernetes Deployment 배포 자동화' 등)."
    )
    user_prompt = "사용자가 과거에 다음과 같은 명령을 수행했습니다:\n\n" + history_text

    model = os.environ.get("KIKI_LLM_MODEL", "local-model")
    summary = call_upstream_with_prompt(model=model, system_prompt=system_prompt, user_prompt=user_prompt)

    return {"summary": summary.strip()}


# ─────────────────────────────────────────────
# 헬스체크
# ─────────────────────────────────────────────



@app.get("/health")
async def health():
    return {"status": "ok", "service": "kiki-agentd"}


# ─────────────────────────────────────────────
# Auth / History
# ─────────────────────────────────────────────

@app.post("/api/v1/register")
async def register(req: RegisterRequest):
    try:
        create_user(req.username, req.password)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Username already exists")
    return {"status": "ok", "username": req.username}


@app.post("/api/v1/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    user = authenticate_user(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_session(user["id"])
    return LoginResponse(access_token=token)


@app.get("/api/v1/history", response_model=HistoryResponse)
async def get_history(limit: int = 20, current_user=Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="User token is required")
    rows = get_recent_logs(current_user["id"], limit=limit)
    items = [
        HistoryItem(
            id=r["id"],
            command_type=r["command_type"],
            prompt=r["prompt"],
            target=r["target"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
    return HistoryResponse(items=items)


@app.get("/api/v1/history/summary")
async def get_history_summary(limit: int = 20, current_user=Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="User token is required")
    rows = get_recent_logs(current_user["id"], limit=limit)
    if not rows:
        return {"summary": "아직 기록된 명령이 없습니다."}

    bullet_lines = []
    for r in rows:
        bullet_lines.append(
            f"- [{r['created_at']}] ({r['command_type']} / {r['target'] or '-'}) {r['prompt']}"
        )

    history_text = "\n".join(bullet_lines)

    system_prompt = (
        "You are an assistant that summarizes a user's past infrastructure automation commands.\n"
        "Explain in Korean, in 3~6 bullet points, what kind of work this user has done.\n"
        "Be concise and high-level (e.g., 'OpenStack 프로젝트/네트워크 생성', 'Kubernetes Deployment 배포 자동화' 등)."
    )
    user_prompt = "사용자가 과거에 다음과 같은 명령을 수행했습니다:\n\n" + history_text

    model = os.environ.get("KIKI_LLM_MODEL", "local-model")

    summary = call_upstream_with_prompt(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    return {"summary": summary.strip()}


# ─────────────────────────────────────────────
# OpenAI 호환: /v1/chat/completions
# ─────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, current_user=Depends(get_current_user)):
    """
    kiki --base-url http://agentd:8082 로 들어오는 OpenAI chat/completions 요청을
    실제 LLM 서버(KIKI_UPSTREAM_LLM_BASE_URL)로 프록시한다.
    """
    body = await request.body()

    # 사용자 메시지 추출 (마지막 user 메시지 기준)
    user_content = ""
    try:
        body_json = json.loads(body.decode("utf-8"))
        messages = body_json.get("messages", [])
        for m in reversed(messages):
            if m.get("role") == "user":
                user_content = m.get("content", "")
                break
    except Exception:
        user_content = ""

    # 로그인한 사용자라면 명령 로그 기록
    if current_user and user_content:
        log_command(
            user_id=current_user["id"],
            command_type="chat",
            prompt=user_content,
            target=None,
        )

    result = call_upstream_chat(body)
    return result


# ─────────────────────────────────────────────
# 구조화된 API: /api/v1/generate
# ─────────────────────────────────────────────

@app.post("/api/v1/generate", response_model=GenerateResponse)
async def generate(req: GenerateRequest, current_user=Depends(get_current_user)):
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

    # 4) 로그인한 사용자의 과거 명령을 간단 RAG 스타일로 붙이기
    if current_user:
        rag_ctx = build_rag_context(current_user["id"], req.prompt)
        if rag_ctx:
            extra_ctx.append(
                "Below are some of this user's previous related commands:\n" + rag_ctx
            )

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

    # 5) generate 명령 로그 기록
    if current_user:
        log_command(
            user_id=current_user["id"],
            command_type="generate",
            prompt=req.prompt,
            target=req.target,
        )

    return GenerateResponse(target=req.target, yaml=yaml_text)
