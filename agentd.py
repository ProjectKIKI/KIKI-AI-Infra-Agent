#!/usr/bin/env python3
import os, re, json, uuid, shutil, pathlib, subprocess, zipfile
import re
import yaml
from datetime import datetime
from typing import List, Optional, Literal, Union, Dict, Any, Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from pydantic import BaseModel, Field
from openai import OpenAI
import ansible_runner

app = FastAPI(title="llama-ansible-agent", version="0.4.0")

MODEL_URL = os.getenv("MODEL_URL", "http://127.0.0.1:8000/v1")
API_KEY   = os.getenv("API_KEY", "sk-noauth")
WORK_DIR  = pathlib.Path(os.getenv("WORK_DIR", "/work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

client = OpenAI(base_url=MODEL_URL, api_key=API_KEY)

_ALLOWED_TOP_KEYS = {
    "name", "hosts", "become", "vars", "tasks", "handlers",
    "roles", "gather_facts", "vars_files", "pre_tasks", "post_tasks",
}

SYSTEM_PROMPT = (
    "You are an Ansible playbook generator.\n"
    "Your only job is to output a complete, valid YAML playbook.\n"
    "Respond strictly in raw YAML.\n"
    "Do NOT include markdown fences, code blocks, explanations, or commentary.\n"
    "If the user asks anything else, still respond only with YAML.\n"
    "Use idempotent modules and proper indentation.\n"
    "Language: YAML only.\n"
)


class GenerateReq(BaseModel):
    message: str
    model: str = "local-llama"
    max_token: int = Field(256, ge=64, le=4096)
    temperature: float = Field(0.5, ge=0, le=1.0)
    name: Optional[str] = None

class RunReq(BaseModel):
    task_id: str
    inventory: Union[str, List[str]]
    engine: Literal["runner","ansible"] = "runner"
    verify: Literal["none","syntax","all"] = "all"
    user: str = "rocky"
    ssh_key: Optional[str] = "/home/agent/.ssh/id_rsa"
    limit: Optional[str] = None
    tags: Optional[str] = None
    extra_vars: Optional[Dict[str, Any]] = None
    inventory_file_content: Optional[str] = None

def slugify(text: str) -> str:
    s = re.sub(r"\s+", "-", text.strip())
    s = re.sub(r"[^0-9A-Za-zㄱ-ㅎ가-힣_-]", "", s)
    return s[:48] or "task"

def ensure_unique_name(run_dir: pathlib.Path, base: str) -> str:
    name = f"{base}.yml"
    if not (run_dir / name).exists():
        return name
    suffix = uuid.uuid4().hex[:6]
    return f"{base}-{suffix}.yml"

def write_file(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

def build_inventory(inv_spec: Union[str, List[str]], run_dir: pathlib.Path, user: str, ssh_key: Optional[str], inline_text: Optional[str]) -> pathlib.Path:
    if inline_text:
        inv_path = run_dir / "inventory.ini"
        write_file(inv_path, inline_text)
        return inv_path

    if isinstance(inv_spec, str):
        p = pathlib.Path(inv_spec)
        if p.exists():
            return p.resolve()
        hosts = [h.strip() for h in inv_spec.split(",") if h.strip()]
    else:
        hosts = [h.strip() for h in inv_spec if isinstance(h, str) and h.strip()]

    if not hosts:
        raise ValueError("inventory host list is empty")

    inv_text = "[all]\n" + "\n".join(
        f"{h} ansible_user={user}" + (f" ansible_ssh_private_key_file={ssh_key}" if ssh_key else "")
        for h in hosts
    ) + "\n"
    inv_path = run_dir / "inventory.ini"
    write_file(inv_path, inv_text)
    return inv_path

def zip_result(run_dir: pathlib.Path) -> pathlib.Path:
    z = run_dir / "bundle.zip"
    with zipfile.ZipFile(str(z), "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in run_dir.rglob("*"):
            if p.is_file():
                zf.write(str(p), str(p.relative_to(run_dir)))
    return z

def runner_stream(playbook: pathlib.Path, inv_path: pathlib.Path, verify: str,
                  limit: Optional[str], tags: Optional[str],
                  extra_vars: Optional[Dict[str, Any]], run_dir: pathlib.Path) -> Iterator[bytes]:
    project_dir = run_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    dst_playbook = project_dir / playbook.name
    if playbook.resolve() != dst_playbook.resolve():
        shutil.copy2(playbook, dst_playbook)

    envvars = {"ANSIBLE_STDOUT_CALLBACK": "json", "ANSIBLE_FORCE_COLOR": "false", "ANSIBLE_HOST_KEY_CHECKING":"False"}
    if extra_vars:
        import yaml as _yaml
        ev = run_dir / "env" / "extravars"
        ev.parent.mkdir(parents=True, exist_ok=True)
        ev.write_text(_yaml.safe_dump(extra_vars, allow_unicode=True), encoding="utf-8")

    def send(obj: Dict[str, Any]):
        return (f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8")

    t1, r1 = ansible_runner.run_async(
        private_data_dir=str(run_dir),
        playbook=str(dst_playbook.name),
        inventory=str(inv_path),
        cmdline="--syntax-check",
        quiet=True,
    )
    for ev in r1.events:
        if ev.get("stdout", "").strip():
            yield send({"type":"event","phase":"syntax","stdout":ev.get("stdout")})
    t1.join()
    if r1.rc != 0:
        yield send({"type":"summary","phase":"syntax","rc":r1.rc,"failed":True})
        yield b"event: end\n\n"
        return
    if verify == "syntax":
        yield send({"type":"summary","phase":"syntax","rc":0,"failed":False})
        yield b"event: end\n\n"
        return

    cmdline = []
    if limit: cmdline += ["--limit", str(limit)]
    if tags:  cmdline += ["--tags", str(tags)]
    t2, r2 = ansible_runner.run_async(
        private_data_dir=str(run_dir),
        playbook=str(dst_playbook.name),
        inventory=str(inv_path),
        envvars=envvars,
        cmdline=" ".join(cmdline),
        quiet=True,
    )
    for ev in r2.events:
        if ev.get("stdout", "").strip():
            yield send({"type":"event","phase":"apply","stdout":ev.get("stdout")})
    t2.join()
    if r2.rc != 0:
        yield send({"type":"summary","phase":"apply","rc":r2.rc,"failed":True})
        yield b"event: end\n\n"
        return
    if verify != "all":
        yield send({"type":"summary","phase":"apply","rc":r2.rc,"failed":False})
        yield b"event: end\n\n"
        return

    t3, r3 = ansible_runner.run_async(
        private_data_dir=str(run_dir),
        playbook=str(dst_playbook.name),
        inventory=str(inv_path),
        envvars=envvars,
        cmdline="--check --diff",
        quiet=True,
    )
    for ev in r3.events:
        if ev.get("stdout", "").strip():
            yield send({"type":"event","phase":"idempotency","stdout":ev.get("stdout")})
    t3.join()

    changed = 0
    try:
        stats = r3.stats or {}
        if isinstance(stats.get("changed"), int):
            changed = stats["changed"]
        elif isinstance(stats.get("changed"), dict):
            changed = sum(stats["changed"].values())
    except Exception:
        pass

    yield send({"type":"summary","phase":"idempotency","rc":r3.rc,"failed": r3.rc != 0, "changed": changed})
    yield b"event: end\n\n"

@app.post("/api/v1/generate")
def generate(req: GenerateReq):
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = WORK_DIR / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    base_name = slugify(req.name or "-".join(req.message.split()[:6]))
    proj = run_dir / "project"
    proj.mkdir(parents=True, exist_ok=True)
    pb_name = ensure_unique_name(proj, base_name)
    pb_path = proj / pb_name

    payload = {
        "model": req.model,
        "messages": [
            {"role":"system","content": SYSTEM_PROMPT},
            {"role":"user","content": req.message}
        ],
        "temperature": req.temperature,
        "max_tokens": req.max_token,
    }
    try:
        resp = client.chat.completions.create(**payload)
        text = (resp.choices[0].message.content or "").strip()
        text = sanitize_yaml(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM error: {e}")
    if not text:
        raise HTTPException(status_code=422, detail="LLM returned empty content")

    write_file(pb_path, text)
    preview = "\\n".join(text.splitlines()[:120])
    return {"task_id": run_id, "run_dir": str(run_dir), "playbook_name": pb_name, "playbook_preview": preview}

@app.post("/api/v1/run")
def run_task(req: RunReq):
    run_dir = WORK_DIR / f"run_{req.task_id}"
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="task_id not found")
    project_dir = run_dir / "project"
    ymls = list(project_dir.glob("*.yml"))
    if not ymls:
        raise HTTPException(status_code=400, detail="no playbook in run_dir")
    playbook = ymls[0]

    try:
        inv_path = build_inventory(req.inventory, run_dir, req.user, req.ssh_key, req.inventory_file_content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if req.engine == "ansible":
        def _run(args, logfile):
            with open(logfile, "wb") as lf:
                p = subprocess.run(args, stdout=lf, stderr=subprocess.STDOUT)
                return p.returncode

        logs = run_dir / "logs"
        logs.mkdir(exist_ok=True)
        base_cmd = ["ansible-playbook", str(playbook), "-i", str(inv_path)]
        if req.limit: base_cmd += ["--limit", req.limit]
        if req.tags:  base_cmd += ["--tags", req.tags]
        if req.extra_vars:
            ev = run_dir / "extra_vars.json"
            ev.write_text(json.dumps(req.extra_vars, ensure_ascii=False), encoding="utf-8")
            base_cmd += ["-e", f"@{ev}"]

        rc = _run(base_cmd + ["--syntax-check"], logs / "01_syntax.log")
        if rc != 0:
            bundle = str(zip_result(run_dir))
            return {"task_id": req.task_id, "rc": rc, "summary": {"phase":"syntax","failed":True}, "bundle": bundle}
        if req.verify == "syntax":
            bundle = str(zip_result(run_dir))
            return {"task_id": req.task_id, "rc": 0, "summary": {"phase":"syntax","failed":False}, "bundle": bundle}

        rc2 = _run(base_cmd, logs / "02_apply.log")
        if rc2 != 0 or req.verify != "all":
            bundle = str(zip_result(run_dir))
            return {"task_id": req.task_id, "rc": rc2, "summary": {"phase":"apply","failed": rc2!=0}, "bundle": bundle}

        rc3 = _run(base_cmd + ["--check","--diff"], logs / "03_check_idempotency.log")
        bundle = str(zip_result(run_dir))
        return {"task_id": req.task_id, "rc": rc3, "summary": {"phase":"idempotency","failed": rc3!=0}, "bundle": bundle}

    def stream_and_zip():
        yield from runner_stream(playbook, inv_path, req.verify, req.limit, req.tags, req.extra_vars, run_dir)
        bundle = str(zip_result(run_dir))
        yield f"data: {json.dumps({'type':'bundle','path': bundle})}\n\n".encode("utf-8")

    return StreamingResponse(stream_and_zip(), media_type="text/event-stream")

@app.get("/api/v1/bundle/{task_id}")
def download_bundle(task_id: str):
    run_dir = WORK_DIR / f"run_{task_id}"
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="task not found")
    bundle = run_dir / "bundle.zip"
    if not bundle.exists():
        return JSONResponse(status_code=404, content={"detail":"bundle not ready"})
    return FileResponse(str(bundle), filename=f"bundle-{task_id}.zip")

def sanitize_yaml(text: str) -> str:
    """
    LLM 출력에서 순수 YAML 플레이북만 추출한다.
    - ``` 코드펜스 제거
    - 안내문("Please replace...", "This playbook will", 숫자목록 등) 제거
    - 맨 앞의 '---' 문서구분선 기준으로 본문만 취함
    - YAML 파싱이 안 되면 일반적이 아닌 행을 제거하여 재시도
    """
    # 0) 코드펜스 제거
    #    ```yaml ~ ```  /  ``` ~ ```
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.MULTILINE)

    # 1) '---' 이후만 취함 (앞의 설명/프롬프트 제거)
    if "---" in text:
        text = text[text.index("---"):]
    else:
        # '---'가 없으면 첫 번째 플레이 목록의 시작을 추정
        m = re.search(r"(?m)^\s*-\s*name\s*:", text)
        if m:
            text = text[m.start():]

    # 2) 명백한 설명/주석성 라인 제거
    drop_patterns = [
        r"(?i)^\s*please replace\b",
        r"(?i)^\s*this playbook will\b",
        r"(?i)^\s*note\b[:：]",
        r"(?i)^\s*instructions?:",
        r"^\s*[-*]\s+\w+.*:.*\(.*\)",  # 리스트형 설명
        r"^\s*\d+\.\s+.+",            # 번호목록 1. 2. ...
        r"^\s*#+\s+.+",               # 마크다운 헤더
        r"^\s*>\s+.+",                # 인용문
        r"^\s*<!--.*?-->\s*$",        # HTML 주석
        r"^\s*`{3,}.*$",              # 코드펜스
    ]
    cleaned_lines = []
    for ln in text.splitlines():
        if any(re.search(p, ln) for p in drop_patterns):
            continue
        cleaned_lines.append(ln)
    text = "\n".join(cleaned_lines).strip() + "\n"

    # 3) YAML 유효성 검사 (성공하면 그대로 반환)
    def _is_valid_playbook(s: str) -> bool:
        try:
            data = yaml.safe_load(s)
        except Exception:
            return False
        # 플레이북은 보통 list[ dict(...) ]
        if not isinstance(data, list) or not data:
            return False
        if not all(isinstance(x, dict) for x in data):
            return False
        # 최상위 키가 너무 엉뚱한지 간단검사
        for play in data:
            if not any(k in play for k in ("hosts", "tasks", "roles", "name")):
                return False
            for k in play.keys():
                if k not in _ALLOWED_TOP_KEYS:
                    # 완전 배제하진 않지만, 이상하면 표시만 해둠
                    pass
        return True

    if _is_valid_playbook(text):
        return text

    # 4) 실패 시, YAML에 포함되기 어려운 행 더 강하게 제거 후 재시도
    strict_keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            strict_keep.append(ln)
            continue
        if s.startswith(("#", "...")):
            continue
        # YAML에 흔한 시작 토큰/키워드만 통과
        if re.match(r"^(-\s+name:)|^(hosts:)|^(become:)|^(vars:)|^(tasks:)|^(handlers:)|^(roles:)|^(gather_facts:)|^(pre_tasks:)|^(post_tasks:)|^(vars_files:)", s):
            strict_keep.append(ln)
            continue
        # 들여쓰기된 하위키는 허용
        if ln.startswith("  ") or ln.startswith("\t"):
            strict_keep.append(ln)
            continue
        # 그 외(설명문/문장형)는 버림
    text2 = "\n".join(strict_keep).strip() + "\n"
    if _is_valid_playbook(text2):
        return text2

    # 5) 그래도 실패면 마지막 방어: 첫 번째 플레이만 추출 시도
    #    --- 이후부터 다음 빈줄 전까지
    chunks = re.split(r"\n\s*\n", text2)
    for ch in chunks:
        if _is_valid_playbook(ch + "\n"):
            return ch.strip() + "\n"

    # 6) 최종 실패: 원문 반환(상위에서 에러로 처리되도록)
    return text