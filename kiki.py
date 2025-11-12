#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse, json, os, re, sys, textwrap
from typing import Any, Dict, List, Optional, Tuple, Union
import requests

FENCE_ANY = re.compile(r"""^\s*(```|~~~)\s*[a-zA-Z0-9._-]*\s*\r?\n(.*?)\r?\n\s*(```|~~~)\s*$""", re.MULTILINE | re.DOTALL)

def strip_fences_strong(text: str) -> str:
    s = str(text or "").replace("\r\n", "\n")
    blocks = FENCE_ANY.findall(s)
    if blocks:
        s = "\n\n".join(b[1].strip() for b in blocks if b[1].strip())
    s = re.sub(r"(?m)^\s*(```|~~~).*$", "", s)
    s = s.replace("```", "").replace("~~~", "")
    s = re.sub(r"(?im)^\s*yaml\s*$", "", s)
    return s.strip()

def light_yaml_sanitize(text: str) -> str:
    s = strip_fences_strong(text)
    if "---" in s:
        s = s[s.index("---"):]
    return (s.strip() + "\n") if s.strip() else s.strip()

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

def resolve_inventory_arg(inv: str) -> Tuple[Union[str, List[str]], Optional[str]]:
    if not inv:
        return [], None
    s = inv.strip()
    if s.startswith("@"):
        path = s[1:].strip()
        if not os.path.exists(path):
            raise SystemExit(f"[ERROR] inventory file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return [], f.read()
    if os.path.exists(s) and os.path.isfile(s):
        with open(s, "r", encoding="utf-8") as f:
            return [], f.read()
    if "\n" in s or s.lstrip().startswith("["):
        return [], s
    if "," in s:
        hosts = [h.strip() for h in s.split(",") if h.strip()]
        return hosts, None
    return [s], None

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kiki.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""KIKI Ansible Agent CLI"""),
    )
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", default="local-llama")
    p.add_argument("--message", required=True)
    p.add_argument("--max-token", type=int, default=256, dest="max_tokens")
    p.add_argument("--temperature", type=float, default=0.5)
    p.add_argument("--name", default=None)
    p.add_argument("--layout", choices=["playbook","role"], default="playbook")
    p.add_argument("--role-name", default=None)
    p.add_argument("--role-hosts", default="all")
    p.add_argument("--inventory", required=True)
    p.add_argument("--verify", choices=["none","syntax","all"], default="all")
    p.add_argument("--user", default="rocky")
    p.add_argument("--ssh-key", default="/home/agent/.ssh/id_rsa", dest="ssh_key")
    p.add_argument("--limit", default=None)
    p.add_argument("--tags", default=None)
    p.add_argument("--extra-vars", default=None)
    return p

def main() -> None:
    args = build_argparser().parse_args()
    base_url = args.base_url.rstrip("/")
    generate_url = f"{base_url}/api/v1/generate"
    run_url      = f"{base_url}/api/v1/run"

    gen_payload: Dict[str, Any] = {
        "message": args.message,
        "model": args.model,
        "max_tokens": max(args.max_tokens, 64),
        "temperature": args.temperature,
        "layout": args.layout,
        "role_name": args.role_name,
        "role_hosts": args.role_hosts,
    }
    if args.name:
        gen_payload["name"] = args.name

    gen = _post_json(generate_url, gen_payload, timeout=180)
    preview_raw = gen.get("playbook_preview", "") or ""
    preview = light_yaml_sanitize(preview_raw)

    print("\n===== Generated Playbook (preview) =====")
    sys.stdout.write(preview)
    print("========================================")
    print(f"task_id: {gen['task_id']}")
    print(f"entry:   {gen['run_dir']}/project/{gen['playbook_name']}")

    inventory, inline_inv = resolve_inventory_arg(args.inventory)

    extra_vars = None
    if args.extra_vars:
        try:
            extra_vars = json.loads(args.extra_vars)
        except Exception as e:
            raise SystemExit(f"[ERROR] --extra-vars JSON parse failed: {e}")

    run_payload: Dict[str, Any] = {
        "task_id": gen["task_id"],
        "inventory": inventory,
        "inventory_file_content": inline_inv,
        "verify": args.verify,
        "user": args.user,
        "ssh_key": args.ssh_key,
        "limit": args.limit,
        "tags": args.tags,
        "extra_vars": extra_vars,
    }

    run = _post_json(run_url, run_payload, timeout=None)
    stdout_text = run.get("stdout", "")
    if stdout_text:
        print("\n===== Ansible Output =====")
        sys.stdout.write(stdout_text)
        sys.stdout.flush()
        print("\n==========================")
    else:
        print("\n(서버 응답에 stdout이 비어 있습니다.)")

    print(f"\nSummary: {run.get('summary')}, rc={run.get('rc')}")
    if "bundle" in run:
        print(f"bundle: {run['bundle']}")

if __name__ == "__main__":
    main()
