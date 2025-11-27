import os
import re
import json
import textwrap
from typing import Optional

import requests


def debug(msg: str, enabled: bool = False) -> None:
    if enabled:
        print(f"[DEBUG] {msg}")


def resolve_llm_endpoint(base_url: str) -> str:
    if re.search(r"/v\d+/|/api/", base_url):
        return base_url
    return base_url.rstrip("/") + "/v1/chat/completions"


def call_llm_chat(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    api_key: Optional[str] = None,
    debug_enabled: bool = False,
) -> str:
    endpoint = resolve_llm_endpoint(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    if debug_enabled:
        debug(f"endpoint={endpoint}", True)
        debug(f"payload={json.dumps(payload)[:200]}...", True)

    resp = requests.post(endpoint, headers=headers, data=json.dumps(payload), timeout=600)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def strip_markdown_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def extract_yaml_from_text(text: str) -> str:
    t = text.strip()
    lines = t.splitlines()

    start_idx = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("---") or stripped.startswith("heat_template_version"):
            start_idx = i
            break

    if start_idx is not None:
        return "\n".join(lines[start_idx:]).strip()
    return t


def build_ansible_ai_system_prompt(target: str, verify: str, inventory: Optional[str]) -> str:
    base = ""

    if target == "ansible":
        base = textwrap.dedent("""
        You are an expert Ansible Playbook generator.
        - Output ONLY valid Ansible YAML. No markdown, no explanations.
        - The top-level must be a list of plays.
        - Use idempotent ansible.builtin modules whenever possible.
        - Never wrap the YAML in any markdown code fences such as triple backticks.
        - The output MUST start with a line containing only '---'.
        """)
    elif target == "k8s":
        base = textwrap.dedent("""
        You are an expert Ansible Playbook generator for Kubernetes.
        - Output ONLY valid Ansible YAML. No markdown, no explanations.
        - Use kubernetes.core collection modules (k8s, k8s_info, etc.).
        - The playbook should apply Kubernetes resources based on the user request.
        - Never wrap the YAML in any markdown code fences such as triple backticks.
        - The output MUST start with a line containing only '---'.
        """)
    elif target == "osp":
        base = textwrap.dedent("""
        You are an expert Ansible Playbook generator for OpenStack.
        - Output ONLY valid Ansible YAML. No markdown, no explanations.
        - Use openstack.cloud collection modules, not legacy os_* modules.
        - Never wrap the YAML in any markdown code fences such as triple backticks.
        - The output MUST start with a line containing only '---'.
        """)
    elif target == "heat":
        base = textwrap.dedent("""
        You are an expert OpenStack Heat template author.
        - Output ONLY valid YAML for a single Heat template (no markdown, no explanations).
        - Include heat_template_version, description, parameters, resources, and outputs if appropriate.
        - Never wrap the YAML in any markdown code fences such as triple backticks.
        - The output MUST start with 'heat_template_version:' on the first line.
        """)
    else:
        base = "You are an infrastructure as code generator. Output only valid YAML."

    if verify in ("syntax", "all"):
        base += "\n- The YAML must be syntactically valid.\n"

    if verify == "all":
        base += "\n- Make playbooks idempotent and use best practices.\n"

    if inventory:
        base += f"\n- Inventory context (host names or groups): {inventory}\n"

    return base.strip()


def llm_chat_simple(prompt: str) -> str:
    base_url = os.environ.get("KIKI_LLM_BASE_URL", "http://127.0.0.1:8082")
    model = os.environ.get("KIKI_LLM_MODEL", "local-model")
    api_key = os.environ.get("KIKI_LLM_API_KEY")
    system_prompt = (
        "You are KIKI, an infra/DevOps assistant. "
        "답변은 한국어로 해도 좋고, 코드 블록은 올바른 형식을 유지하세요."
    )
    reply = call_llm_chat(
        base_url=base_url,
        model=model,
        system_prompt=system_prompt,
        user_prompt=prompt,
        api_key=api_key,
    )
    return reply


def llm_ansible_ai(prompt: str, target: str, inventory: Optional[str], verify: str) -> str:
    base_url = os.environ.get("KIKI_LLM_BASE_URL", "http://127.0.0.1:8082")
    model = os.environ.get("KIKI_LLM_MODEL", "local-model")
    api_key = os.environ.get("KIKI_LLM_API_KEY")

    system_prompt = build_ansible_ai_system_prompt(target, verify, inventory)

    extra = []
    if inventory:
        extra.append(f"Inventory: {inventory}")
    if target in ("ansible", "k8s", "osp"):
        extra.append("결과는 Ansible playbook 전체 YAML로 출력해줘.")
    elif target == "heat":
        extra.append("결과는 Heat 템플릿 YAML 한 개만 출력해줘.")

    if extra:
        prompt = prompt + "\n\n[Context]\n" + "\n".join(extra)

    raw = call_llm_chat(
        base_url=base_url,
        model=model,
        system_prompt=system_prompt,
        user_prompt=prompt,
        api_key=api_key,
    )
    clean = strip_markdown_fences(raw)
    yaml_text = extract_yaml_from_text(clean)
    return yaml_text
