#!/usr/bin/env python3
import os, re, json, uuid, shutil, pathlib, subprocess, zipfile
from datetime import datetime
from typing import List, Optional, Literal, Union, Dict, Any, Iterator

from unidecode import unidecode
import re

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from pydantic import BaseModel, Field
from openai import OpenAI
import ansible_runner


# App & globals
# ------------------------------------------------------------------------------
app = FastAPI(title="llama-ansible-agent", version="0.4.1")

MODEL_URL = os.getenv("MODEL_URL", "http://127.0.0.1:8080/v1").rstrip("/")
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
    "Never include markdown fences or any instructional text (e.g., 'Please replace', 'Note:').\n"
    "If the user asks anything else, still respond only with YAML.\n"
    "Use idempotent modules and proper indentation.\n"
    "Language: YAML only.\n"
)

FENCE_ANY = re.compile(
    r"""
    ^\s*(```|~~~)\s*            # 여는 펜스: ``` 또는 ~~~ (공백 허용)
    [a-zA-Z0-9._-]*\s*          # 언어 토큰(예: yaml, yml 등) + 공백(선택)
    \r?\n                       # 줄바꿈
    (.*?)                       # <- 내용 캡쳐 (non-greedy)
    \r?\n\s*(```|~~~)\s*$       # 닫는 펜스
    """,
    re.MULTILINE | re.DOTALL | re.VERBOSE,
)


# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------
class GenerateReq(BaseModel):
    message: str
    model: str = "local-llama"
    # allow both "max_token" and "max_tokens"
    max_token: int = Field(256, ge=64, le=4096, alias="max_tokens")
    temperature: float = Field(0.5, ge=0.0, le=1.0)
    name: Optional[str] = None

    class Config:
        allow_population_by_field_name = True  # accept field name & alias

class RunReq(BaseModel):
    task_id: str
    inventory: Union[str, List[str]]
    engine: Literal["runner","ansible"] = "ansible"  # default to plain ansible
    verify: Literal["none","syntax","all"] = "all"
    user: str = "rocky"
    ssh_key: Optional[str] = "/home/agent/.ssh/id_rsa"
    limit: Optional[str] = None
    tags: Optional[str] = None
    extra_vars: Optional[Dict[str, Any]] = None
    inventory_file_content: Optional[str] = None

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
FENCE_BLOCK = re.compile(r"```(?:[a-zA-Z0-9._-]+)?\s*\n([\s\S]*?)\n```", re.MULTILINE)

def strip_fences(text: str) -> str:
    """
    Remove markdown code fences like ```yaml ... ```
    If multiple fenced blocks exist, join their contents.
    """
    s = (text or "").strip()
    if "```" not in s:
        return s
    blocks = FENCE_BLOCK.findall(s)
    if blocks:
        return "\n\n".join(b.strip() for b in blocks if b.strip())
    # fallback: remove fence markers only
    s = re.sub(r"```[a-zA-Z0-9._-]*\s*", "", s)
    s = s.replace("```", "")
    return s.strip()

FENCE_ANY = re.compile(
    r"""
    ^\s*(```|~~~)\s*            # 여는 펜스: ``` 또는 ~~~ (공백 허용)
    [a-zA-Z0-9._-]*\s*          # 언어 토큰(예: yaml, yml 등) + 공백(선택)
    \r?\n                       # 줄바꿈
    (.*?)                       # <- 내용 캡쳐 (non-greedy)
    \r?\n\s*(```|~~~)\s*$       # 닫는 펜스
    """,
    re.MULTILINE | re.DOTALL | re.VERBOSE,
)

def strip_fences_strong(text: str) -> str:
    """``` yaml / ```yaml / ``` yml / ~~~ 등 모든 변형 펜스를 제거."""
    s = str(text or "").replace("\r\n", "\n")
    if "```" in s or "~~~" in s:
        blocks = FENCE_ANY.findall(s)
        if blocks:
            # 캡쳐된 내용들만 이어붙임
            return "\n\n".join(b[1].strip() for b in blocks if b[1].strip())
        # 블록 매칭이 안 되면 펜스가 있는 줄 자체를 제거
        s = re.sub(r"(?m)^\s*(```|~~~).*$", "", s)
        s = s.replace("```", "").replace("~~~", "")
    # 일부 모델이 첫 줄에 'yaml' 단독 라인을 넣는 경우 제거
    s = re.sub(r"(?im)^\s*yaml\s*$", "", s).strip()
    return s


def slugify(text: str) -> str:
    """한글을 로마자로 변환해 영문 파일 이름 생성"""
    s = unidecode(text or "")  # 예: "설치" -> "Seolchi"
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"[^0-9A-Za-z_-]", "", s)
    return s.lower()[:48] or "task"

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
    """
    Build a minimal INI inventory.
    - If inline_text is provided, use it as-is.
    - If inv_spec is a file path, use it.
    - Otherwise treat inv_spec as CSV or list of hosts.
    Adds both [all] and [webserver] groups to tolerate LLM's common 'hosts: webserver'.
    """
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

    lines_all = [
        f"{h} ansible_user={user}" + (f" ansible_ssh_private_key_file={ssh_key}" if ssh_key else "")
        for h in hosts
    ]
    inv_text = "[all]\n" + "\n".join(lines_all) + "\n\n[webserver]\n" + "\n".join(hosts) + "\n"

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
    """
    Stream ansible-runner events as text/event-stream.
    Uses default callback (not json) to avoid 'json_indent' KeyError issues.
    """
    project_dir = run_dir / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    dst_playbook = project_dir / playbook.name
    if playbook.resolve() != dst_playbook.resolve():
        shutil.copy2(playbook, dst_playbook)
    """
    이렇게 코드는 누더기가 되어가구나 ㅋㅋㅋㅋ
    """

    cfg = """[defaults]
    stdout_callback = default
    callbacks_enabled =
    bin_ansible_callbacks = False
    """
    write_file(project_dir / "ansible.cfg", cfg)

    envvars = {
        "ANSIBLE_CONFIG": str(project_dir / "ansible.cfg"),
        "ANSIBLE_STDOUT_CALLBACK": "default",
        "ANSIBLE_CALLBACKS_ENABLED": "",
        "ANSIBLE_FORCE_COLOR": "false",
        "ANSIBLE_HOST_KEY_CHECKING": "False",
    }
    if extra_vars:
        ev = run_dir / "env" / "extravars"
        ev.parent.mkdir(parents=True, exist_ok=True)
        ev.write_text(yaml.safe_dump(extra_vars, allow_unicode=True), encoding="utf-8")

    def send(obj: Dict[str, Any]):
        return (f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8")

    # syntax check
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

    # apply
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

    # idempotency check
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

# ------------------------------------------------------------------------------
# YAML sanitization
# ------------------------------------------------------------------------------
def sanitize_yaml(text: str) -> str:
    if not text:
        return ""
    text = strip_fences_strong(text)
    text = text.replace("\r\n", "\n").strip()
    text = str(text)
    text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"```$", "", text.strip(), flags=re.MULTILINE)
    text = text.replace("```yaml", "").replace("```yml", "").replace("```", "")

    # normalize newlines
    text = text.replace("\r\n", "\n").strip() + "\n"

    try:
        # 0) remove fences
        text = strip_fences(text)

        # normalize newlines
        text = (text or "").replace("\r\n", "\n").strip()

        # 1) prefer after '---'
        if "---" in text:
            text = text[text.index("---") :]
        text = text.strip() + "\n"

        # 2) drop obvious instructional/commentary lines
        drop_patterns = [
            r"(?i)^\s*please replace\b",
            r"(?i)^\s*replace\b",
            r"(?i)^\s*note\b[:：]?",
            r"(?i)^\s*remember\b",
            r"(?i)^\s*make sure\b",
            r"(?i)^\s*you should\b",
            r"(?i)^\s*for example\b",
            r"(?i)^\s*this playbook will\b",
            r"(?i)^\s*instructions?:",
            r"^\s*[-*]\s+\w+.*:.*\(.*\)",  # bullet hints
            r"^\s*\d+\.\s+.+",            # ordered lists
            r"^\s*#+\s+.+",               # markdown headers
            r"^\s*>\s+.+",                # blockquote
            r"^\s*<!--.*?-->\s*$",        # HTML comments
            r"^\s*`{3,}.*$",              # leftover fences
        ]
        cleaned_lines = []
        for ln in text.splitlines():
            if any(re.search(p, ln) for p in drop_patterns):
                continue
            cleaned_lines.append(ln)
        text = "\n".join(cleaned_lines).strip() + "\n"

        # 3) quick validity check
        def _is_valid_pb(s: str) -> bool:
            try:
                data = yaml.safe_load(s)
            except Exception:
                return False
            if not isinstance(data, list) or not data:
                return False
            if not all(isinstance(x, dict) for x in data):
                return False
            for play in data:
                if not any(k in play for k in ("hosts", "tasks", "roles", "name")):
                    return False
            return True

        if _is_valid_pb(text):
            return text

        # 4) conservative keep: only YAML-ish lines
        keep = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                keep.append(ln); continue
            if s.startswith(("#","...")):
                continue
            if re.match(r"^(-\s+name:|hosts:|become:|vars:|tasks:|handlers:|roles:|gather_facts:|pre_tasks:|post_tasks:|vars_files:)", s):
                keep.append(ln); continue
            if ln.startswith("  ") or ln.startswith("\t"):
                keep.append(ln); continue
        text2 = "\n".join(keep).strip() + "\n"

        if _is_valid_pb(text2):
            return text2

        # 5) try first chunk
        chunks = re.split(r"\n\s*\n", text2)
        for ch in chunks:
            chs = ch.strip() + "\n"
            if _is_valid_pb(chs):
                return chs

        # 6) last resort: return the initial cleaned text to let caller decide
        return text
    except Exception as e:
        print(f"[sanitize_yaml error] {e}")
        return text or ""

# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------
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
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": req.message},
        ],
        "temperature": req.temperature,
        "max_tokens": req.max_token,
    }

    try:
        resp = client.chat.completions.create(**payload)
        text = (resp.choices[0].message.content or "")
        text = strip_fences(text)     # remove ```yaml fences
        text = strip_fences_strong(text)
        text = sanitize_yaml(text)    # robust YAML-only extraction
        text = re.sub(r"```[a-zA-Z0-9_-]*\s*", "", text)
        text = text.replace("```", "").strip()
    except Exception as e:
        detail = f"LLM error: {e}"
        raise HTTPException(status_code=500, detail=detail)

    if not text or not text.strip():
        raise HTTPException(status_code=422, detail="LLM returned empty content")

    write_file(pb_path, text)
    preview = "\n".join(text.splitlines()[:120])
    return {
        "task_id": run_id,
        "run_dir": str(run_dir),
        "playbook_name": pb_name,
        "playbook_preview": preview
    }

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

    # Plain ansible-playbook path
    if req.engine == "ansible":
        def _run(args, logfile):
            env = dict(os.environ)
            env["ANSIBLE_STDOUT_CALLBACK"] = "default"
            env["ANSIBLE_CALLBACKS_ENABLED"] = ""
            env["ANSIBLE_FORCE_COLOR"] = "false"
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

    # ansible-runner SSE stream
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

# ------------------------------------------------------------------------------
# (optional) local run
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    # For local debugging only:
    import uvicorn
    uvicorn.run("agentd:app", host="0.0.0.0", port=8082, reload=False)
