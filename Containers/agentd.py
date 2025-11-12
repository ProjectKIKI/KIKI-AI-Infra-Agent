#!/usr/bin/env python3
import os, re, io, json, uuid, shutil, pathlib, subprocess, zipfile
from datetime import datetime
from typing import List, Optional, Literal, Union, Dict, Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from openai import OpenAI
from unidecode import unidecode

# ------------------------------------------------------------------------------
# App & globals
# ------------------------------------------------------------------------------
app = FastAPI(title="llama-ansible-agent", version="0.5.1-ansible-only+stdout")

MODEL_URL = os.getenv("MODEL_URL", "http://127.0.0.1:8080/v1").rstrip("/")
API_KEY   = os.getenv("API_KEY", "sk-noauth")
WORK_DIR  = pathlib.Path(os.getenv("WORK_DIR", "/work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

client = OpenAI(base_url=MODEL_URL, api_key=API_KEY)

_ALLOWED_TOP_KEYS = {
    "name","hosts","become","vars","tasks","handlers","roles",
    "gather_facts","vars_files","pre_tasks","post_tasks",
}

SYSTEM_PROMPT = (
    "You are an Ansible playbook generator.\n"
    "Output ONLY valid YAML for a complete Ansible playbook.\n"
    "No markdown fences, no explanations, no comments or notes.\n"
    "Use idempotent modules. YAML only.\n"
)

# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------
class GenerateReq(BaseModel):
    message: str
    model: str = "local-llama"
    max_token: int = Field(256, ge=64, le=4096, alias="max_tokens")
    temperature: float = Field(0.5, ge=0.0, le=1.0)
    name: Optional[str] = None
    class Config:
        allow_population_by_field_name = True

class RunReq(BaseModel):
    task_id: str
    inventory: Union[str, List[str]]
    verify: Literal["none","syntax","all"] = "all"
    user: str = "rocky"
    ssh_key: Optional[str] = "/home/agent/.ssh/id_rsa"
    limit: Optional[str] = None
    tags: Optional[str] = None
    extra_vars: Optional[Dict[str, Any]] = None
    inventory_file_content: Optional[str] = None

# ------------------------------------------------------------------------------
# Utils
# ------------------------------------------------------------------------------
FENCE_ANY = re.compile(
    r"""^\s*(```|~~~)\s*[a-zA-Z0-9._-]*\s*\r?\n(.*?)\r?\n\s*(```|~~~)\s*$""",
    re.MULTILINE | re.DOTALL,
)

def strip_fences_strong(text: str) -> str:
    """```yaml / ``` yaml / ~~~ 등 모든 변형 펜스를 제거 (블록/잔여 모두)."""
    s = str(text or "").replace("\r\n", "\n")
    blocks = FENCE_ANY.findall(s)
    if blocks:
        s = "\n\n".join(b[1].strip() for b in blocks if b[1].strip())
    s = re.sub(r"(?m)^\s*(```|~~~).*$", "", s)
    s = s.replace("```","").replace("~~~","")
    s = re.sub(r"(?im)^\s*yaml\s*$", "", s)
    return s.strip()

def slugify(text: str) -> str:
    """한글을 로마자로 변환해 영문 파일 이름 생성"""
    s = unidecode(text or "")
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"[^0-9A-Za-z_-]", "", s)
    return (s.lower()[:48] or "task")

def ensure_unique_name(run_dir: pathlib.Path, base: str) -> str:
    name = f"{base}.yml"
    if not (run_dir / name).exists():
        return name
    return f"{base}-{uuid.uuid4().hex[:6]}.yml"

def write_file(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)

def build_inventory(inv_spec: Union[str, List[str]], run_dir: pathlib.Path,
                    user: str, ssh_key: Optional[str], inline_text: Optional[str]) -> pathlib.Path:
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

def sanitize_yaml(text: str) -> str:
    """
    Keep only a valid Ansible playbook YAML.
    Never raise; best-effort cleanup.
    """
    if not text:
        return ""
    text = strip_fences_strong(text)
    text = text.replace("\r\n","\n").strip()
    if "---" in text:
        text = text[text.index("---"):]
    text = text.strip() + "\n"

    drop_patterns = [
        r"(?i)^\s*please replace\b", r"(?i)^\s*replace\b", r"(?i)^\s*note\b[:：]?",
        r"(?i)^\s*remember\b", r"(?i)^\s*make sure\b", r"(?i)^\s*you should\b",
        r"(?i)^\s*for example\b", r"(?i)^\s*this playbook will\b",
        r"(?i)^\s*instructions?:", r"^\s*[-*]\s+\w+.*:.*\(.*\)", r"^\s*\d+\.\s+.+",
        r"^\s*#+\s+.+", r"^\s*>\s+.+", r"^\s*<!--.*?-->\s*$", r"^\s*`{3,}.*$",
    ]
    keep = []
    for ln in text.splitlines():
        if any(re.search(p, ln) for p in drop_patterns):
            continue
        keep.append(ln)
    text = "\n".join(keep).strip() + "\n"

    def _valid(s: str) -> bool:
        try:
            data = yaml.safe_load(s)
        except Exception:
            return False
        if not isinstance(data, list) or not data or not all(isinstance(x, dict) for x in data):
            return False
        for play in data:
            if not any(k in play for k in ("hosts","tasks","roles","name")):
                return False
        return True

    if _valid(text):
        return text

    # conservative filter
    keep2 = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s: keep2.append(ln); continue
        if s.startswith(("#","...")): continue
        if re.match(r"^(-\s+name:|hosts:|become:|vars:|tasks:|handlers:|roles:|gather_facts:|pre_tasks:|post_tasks:|vars_files:)", s):
            keep2.append(ln); continue
        if ln.startswith("  ") or ln.startswith("\t"):
            keep2.append(ln); continue
    text2 = "\n".join(keep2).strip() + "\n"
    return text2 or text

def ensure_ansible_cfg(project_dir: pathlib.Path) -> pathlib.Path:
    cfg = """[defaults]
stdout_callback = default
# callbacks_enabled =
bin_ansible_callbacks = False
host_key_checking = False
retry_files_enabled = False
deprecation_warnings = False
"""
    path = project_dir / "ansible.cfg"
    write_file(path, cfg)
    return path

# ------------------------------------------------------------------------------
# API
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
            {"role":"system","content": SYSTEM_PROMPT},
            {"role":"user","content": req.message},
        ],
        "temperature": req.temperature,
        "max_tokens": req.max_token,
    }

    try:
        resp = client.chat.completions.create(**payload)
        text = (resp.choices[0].message.content or "")
        text = strip_fences_strong(text)
        text = sanitize_yaml(text)
        text = strip_fences_strong(text)   # 저장 직전 이중 안전망
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"LLM error: {e}")

    if not text.strip():
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
    ymls = sorted(project_dir.glob("*.yml"))
    if not ymls:
        raise HTTPException(status_code=400, detail="no playbook in run_dir")
    playbook = ymls[0]

    try:
        inv_path = build_inventory(req.inventory, run_dir, req.user, req.ssh_key, req.inventory_file_content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # per-run ansible.cfg (force default callback)
    cfg_path = ensure_ansible_cfg(project_dir)

    def _run(args, logfile) -> Dict[str, Any]:
        env = dict(os.environ)
        for k in ("ANSIBLE_CALLBACKS_ENABLED", "DEFAULT_CALLBACK_WHITELIST",
                  "ANSIBLE_LOAD_CALLBACK_PLUGINS"):
            env.pop(k, None)
        env.update({
            "ANSIBLE_CONFIG": str(cfg_path),
            "ANSIBLE_STDOUT_CALLBACK": "default",
            "ANSIBLE_CALLBACKS_ENABLED": "False",
            "ANSIBLE_FORCE_COLOR": "true",   # 콘솔 색상 유지 원하면 true
            "ANSIBLE_HOST_KEY_CHECKING": "False",
        })
        buf = io.StringIO()
        with open(logfile, "wb") as lf:
            print(f"\n>>> Running: {' '.join(args)}\n")
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            # 실시간 출력 + 파일 기록 + 버퍼 저장
            for line in proc.stdout:
                print(line, end="")
                buf.write(line)
                lf.write(line.encode("utf-8", errors="ignore"))
            proc.wait()
            return {"rc": proc.returncode, "stdout": buf.getvalue()}

    logs = run_dir / "logs"
    logs.mkdir(exist_ok=True)

    base_cmd = ["ansible-playbook", str(playbook), "-i", str(inv_path)]
    if req.limit: base_cmd += ["--limit", req.limit]
    if req.tags:  base_cmd += ["--tags", req.tags]
    if req.extra_vars:
        ev = run_dir / "extra_vars.json"
        ev.write_text(json.dumps(req.extra_vars, ensure_ascii=False), encoding="utf-8")
        base_cmd += ["-e", f"@{ev}"]

    # 1) syntax
    r1 = _run(base_cmd + ["--syntax-check"], logs / "01_syntax.log")
    if r1["rc"] != 0:
        bundle = str(zip_result(run_dir))
        return {"task_id": req.task_id, "rc": r1["rc"], "summary": {"phase":"syntax","failed":True}, "stdout": r1["stdout"], "bundle": bundle}
    if req.verify == "syntax":
        bundle = str(zip_result(run_dir))
        return {"task_id": req.task_id, "rc": 0, "summary": {"phase":"syntax","failed":False}, "stdout": r1["stdout"], "bundle": bundle}

    # 2) apply
    r2 = _run(base_cmd, logs / "02_apply.log")
    if r2["rc"] != 0 or req.verify != "all":
        bundle = str(zip_result(run_dir))
        return {"task_id": req.task_id, "rc": r2["rc"], "summary": {"phase":"apply","failed": r2["rc"]!=0}, "stdout": r2["stdout"], "bundle": bundle}

    # 3) idempotency
    r3 = _run(base_cmd + ["--check","--diff"], logs / "03_check_idempotency.log")
    bundle = str(zip_result(run_dir))
    return {"task_id": req.task_id, "rc": r3["rc"], "summary": {"phase":"idempotency","failed": r3["rc"]!=0}, "stdout": r3["stdout"], "bundle": bundle}

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
# Local debug
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("agentd:app", host="0.0.0.0", port=8082, reload=False)

