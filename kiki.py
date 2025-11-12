#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import re
import sys
import textwrap
from typing import Any, Dict, List, Optional, Union

import requests

# ------------------------------
# Fence strippers (```yaml, ~~~)
# ------------------------------
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
    # 블록으로 못 잡힌 라인형 펜스 제거
    s = re.sub(r"(?m)^\s*(```|~~~).*$", "", s)
    # 남아있는 토큰 제거
    s = s.replace("```", "").replace("~~~", "")
    # 모델이 'yaml' 단독 라인을 넣는 경우 제거
    s = re.sub(r"(?im)^\s*yaml\s*$", "", s)
    return s.strip()

def light_yaml_sanitize(text: str) -> str:
    """첫 '---' 이후만 남기고 앞뒤 공백 정리."""
    s = strip_fences_strong(text)
    if "---" in s:
        s = s[s.index("---"):]
    return (s.strip() + "\n") if s.strip() else s.strip()

# ------------------------------
# HTTP helpers
# ------------------------------
def _post_json(url: str, payload: Dict[str, Any], timeout: Optional[Union[int, float]] = None) -> Dict[str, Any]:
    try:
        r = requests.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as he:
        body = getattr(he.response, "text", "")
        raise SystemExit(f"[HTTP {he.response.status_code}] {url}\n{body}") from he
    except Exception as e:
        raise SystemExit(f"[HTTP ERROR] {url}: {e}") from e

# ------------------------------
# Inventory helpers
# ------------------------------
def parse_inventory_arg(inv: str) -> Union[str, List[str]]:
    """
    사용자가 "node1,node2,node3" 형태로 주면 list[str]로,
    파일 경로를 주면 문자열 그대로 서버에 전달한다.
    """
    inv = (inv or "").strip()
    if not inv:
        return []
    if os.path.exists(inv):
        return inv
    parts = [x.strip() for x in inv.split(",") if x.strip()]
    return parts or inv

def read_file_if_exists(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    p = path.strip()
    if not p:
        return None
    if not os.path.exists(p):
        raise SystemExit(f"[ERROR] inventory file not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return f.read()

# ------------------------------
# CLI
# ------------------------------
def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kiki.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            KIKI Ansible Agent CLI
            - /api/v1/generate: 플레이북 생성
            - /api/v1/run: ansible-playbook 실행
            - 실행 결과(stdout)를 화면에 그대로 출력
            """
        ),
    )
    p.add_argument("--base-url", required=True, help="llama-ansible-agent base URL (e.g., http://127.0.0.1:8082)")
    p.add_argument("--model", default="local-llama", help="model name")
    p.add_argument("--message", required=True, help="LLM 프롬프트(한국어/영어 자유)")
    p.add_argument("--max-token", type=int, default=256, dest="max_tokens", help="max tokens for generation")
    p.add_argument("--temperature", type=float, default=0.5, help="sampling temperature")
    p.add_argument("--name", default=None, help="파일 이름 베이스(미지정 시 message 일부 사용)")

    # Run options
    p.add_argument("--inventory", required=True, help="CSV(host1,host2) 또는 인벤토리 파일 경로")
    p.add_argument("--inventory-file", default=None, help="인벤토리 내용 자체를 파일로 전달하고 싶을 때 사용")
    p.add_argument("--verify", choices=["none", "syntax", "all"], default="all", help="검증 단계")
    p.add_argument("--user", default="rocky", help="ansible_user")
    p.add_argument("--ssh-key", default="/home/agent/.ssh/id_rsa", dest="ssh_key", help="SSH private key path")
    p.add_argument("--limit", default=None, help="ansible --limit")
    p.add_argument("--tags", default=None, help="ansible --tags (comma sep)")
    p.add_argument("--extra-vars", default=None, help='JSON string for -e, ex: \'{"k":"v"}\'')

    return p

# ------------------------------
# Main
# ------------------------------
def main() -> None:
    args = build_argparser().parse_args()

    base_url = args.base_url.rstrip("/")
    generate_url = f"{base_url}/api/v1/generate"
    run_url      = f"{base_url}/api/v1/run"

    # 1) Generate
    gen_payload: Dict[str, Any] = {
        "message": args.message,
        "model": args.model,
        "max_tokens": max(args.max_tokens, 64),
        "temperature": args.temperature,
    }
    if args.name:
        gen_payload["name"] = args.name

    gen = _post_json(generate_url, gen_payload, timeout=180)

    preview_raw = gen.get("playbook_preview", "") or ""
    preview = light_yaml_sanitize(preview_raw)

    print("\n===== Generated Playbook (preview) =====")
    # 펜스 금지, 순수 YAML만
    sys.stdout.write(preview)
    print("========================================")
    print(f"task_id: {gen['task_id']}")
    print(f"playbook: {gen['run_dir']}/project/{gen['playbook_name']}")

    # 2) Run
    inventory = parse_inventory_arg(args.inventory)
    inline_inv = read_file_if_exists(args.inventory_file)

    extra_vars = None
    if args.extra_vars:
        try:
            extra_vars = json.loads(args.extra_vars)
        except Exception as e:
            raise SystemExit(f"[ERROR] --extra-vars JSON parse failed: {e}")

    run_payload: Dict[str, Any] = {
        "task_id": gen["task_id"],
        "inventory": inventory,
        "verify": args.verify,
        "user": args.user,
        "ssh_key": args.ssh_key,
        "limit": args.limit,
        "tags": args.tags,
        "extra_vars": extra_vars,
        "inventory_file_content": inline_inv,
    }

    run = _post_json(run_url, run_payload, timeout=None)

    # 3) Print stdout
    stdout_text = run.get("stdout", "")
    if stdout_text:
        print("\n===== Ansible Output =====")
        # 색상 유지
        sys.stdout.write(stdout_text)
        sys.stdout.flush()
        print("\n==========================")
    else:
        print("\n(서버 응답에 stdout이 비어 있습니다.)")

    # 4) Summary
    print(f"\nSummary: {run.get('summary')}, rc={run.get('rc')}")
    if "bundle" in run:
        print(f"bundle: {run['bundle']}")

if __name__ == "__main__":
    main()

