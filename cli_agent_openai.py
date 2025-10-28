import os
import json
import yaml
import shutil
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

from rich import print, box
from rich.panel import Panel
from rich.console import Console
from rich.table import Table

from openai import OpenAI
import ansible_runner

from prompts import SYSTEM_PROMPT, GEN_PLAYBOOK_PROMPT, FIX_PLAYBOOK_PROMPT

console = Console()

class OpenAIChatLLM:
    def __init__(self, base_url: str = "http://127.0.0.1:8000/v1", api_key: str = "sk-noauth", model: str = "local-llama") -> None:
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    def generate(self, system: str, prompt: str) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            top_p=0.9,
            max_tokens=1024,
        )
        text = resp.choices[0].message.content or ""
        return text.strip()

class PlaybookAgent:
    def __init__(
        self,
        llm: OpenAIChatLLM,
        inventory_path: str = "example_inventory.ini",
        work_dir: str = "workdir",
    ):
        self.llm = llm
        self.inventory_path = inventory_path
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def draft_playbook(self, task_desc: str) -> str:
        prompt = GEN_PLAYBOOK_PROMPT.format(task=task_desc)
        result = self.llm.generate(SYSTEM_PROMPT, prompt)
        return self._only_yaml(result)

    def fix_playbook(self, task_desc: str, summary: str, errors: str) -> str:
        prompt = FIX_PLAYBOOK_PROMPT.format(task=task_desc, summary=summary, errors=errors)
        result = self.llm.generate(SYSTEM_PROMPT, prompt)
        return self._only_yaml(result)

    def validate_yaml(self, text: str) -> Tuple[bool, Optional[str]]:
        try:
            obj = yaml.safe_load(text)
            if not isinstance(obj, list):
                return False, "Top-level must be a list of plays."
            for play in obj:
                if "hosts" not in play:
                    return False, "Each play must define 'hosts'."
                if "tasks" not in play or not isinstance(play["tasks"], list):
                    return False, "Each play must include a 'tasks' list."
                for t in play["tasks"]:
                    if not isinstance(t, dict):
                        return False, "Task entries must be dictionaries."
                    if "name" not in t:
                        return False, "Every task must have a 'name'."
            return True, None
        except Exception as e:
            return False, f"YAML parse error: {e}"

    def lint_heuristics(self, text: str) -> List[str]:
        warnings = []
        try:
            obj = yaml.safe_load(text) or []
            for play in obj:
                for t in play.get("tasks", []):
                    if any(m in t for m in ("shell", "command")):
                        warnings.append("Task uses 'shell/command'. Prefer native modules if possible.")
                    if t.get("become") is False and any(k in t for k in ("package","service","copy","template","file")):
                        warnings.append("Privileged operations without become: true may fail.")
        except Exception:
            pass
        return list(dict.fromkeys(warnings))

    def _runner(self, playbook_name: str, cmdline: Optional[str] = None) -> ansible_runner.Runner:
        artifacts_dir = self.work_dir / "artifacts"
        if artifacts_dir.exists():
            shutil.rmtree(artifacts_dir)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["ANSIBLE_STDOUT_CALLBACK"] = "json"
        env["ANSIBLE_CALLBACKS_ENABLED"] = "ansible.posix.json"

        r = ansible_runner.run(
            private_data_dir=str(self.work_dir),
            playbook=playbook_name,
            inventory=str(self.inventory_path),
            envvars=env,
            quiet=True,
            json_mode=True,
            cmdline=cmdline,
        )
        return r

    def run_syntax_check(self, playbook_name: str) -> Tuple[bool, str]:
        r = self._runner(playbook_name, cmdline="--syntax-check")
        ok = (r.rc == 0)
        return ok, f"rc={r.rc}, status={r.status}"

    def run_check_mode(self, playbook_name: str) -> Tuple[bool, Dict[str, Any], List[str]]:
        r = self._runner(playbook_name, cmdline="--check --diff")
        summary, errors = self._collect_summary_errors(r)
        ok = (r.rc == 0 and all(s.get("failures", s.get("failed",0)) == 0 for s in summary["stats"].values()))
        return ok, summary, errors

    def run_playbook(self, playbook_name: str) -> Tuple[bool, Dict[str, Any], List[str]]:
        r = self._runner(playbook_name)
        summary, errors = self._collect_summary_errors(r)

        # summary가 None일 때도 안전하게 처리
        stats = (summary or {}).get("stats")

        if isinstance(stats, dict):
          ok = (r.rc == 0 and all(
            (s.get("failures", s.get("failed", 0)) == 0) and
            (s.get("unreachable", 0) == 0)
            for s in stats.values()
        ))
        else:
            ok = (r.rc == 0)

        return ok, summary, errors

    def run_idempotency_check(self, playbook_name: str) -> Tuple[bool, List[Dict[str, Any]]]:
        summaries: List[Dict[str, Any]] = []

        # 1st run
        r1 = self._runner(playbook_name)
        summary1, errors1 = self._collect_summary_errors(r1)

        # 2nd run (idempotency check)
        r2 = self._runner(playbook_name)
        summary2, errors2 = self._collect_summary_errors(r2)

        # ---- Safe guard: summary2 may be None ----
        stats2 = (summary2 or {}).get("stats")

        if isinstance(stats2, dict):
            # changed==0 이고, 실패/도달불가도 0이어야 idem OK
            all_zero = all(
                (s.get("changed", 0) == 0) and
                (s.get("failures", s.get("failed", 0)) == 0) and
                (s.get("unreachable", 0) == 0)
                for s in stats2.values()
            )
            idem_ok = (r2.rc == 0) and all_zero
        else:
            # 통계를 못 얻으면 rc만으로 판정 (최소한 크래시는 피한다)
            idem_ok = (r2.rc == 0)

        return idem_ok, {"first": summary1, "second": summary2}


    def _collect_summary_errors(self, r: ansible_runner.Runner) -> Tuple[Dict[str,Any], List[str]]:
        summary = {"status": r.status, "rc": r.rc, "stats": r.stats}
        errors = []
        for ev in r.events:
            if ev.get("event") in ("runner_on_failed", "runner_on_unreachable", "runner_on_async_failed"):
                res = ev.get("event_data", {}).get("res", {})
                stderr = res.get("stderr") or res.get("msg") or str(res)[:500]
                task = ev.get("event_data", {}).get("task")
                host = ev.get("event_data", {}).get("host")
                errors.append(f"[{host}] {task}: {stderr}")
        return summary, errors

    def run_task(self, task_desc: str, verify: str = "all", max_attempts: int = 2) -> None:
        console.rule("[bold cyan]llama.cpp × Ansible Agent (OpenAI-compatible)")
        print(Panel.fit(task_desc, title="Task", border_style="cyan", box=box.ROUNDED))

        pb_text = self.draft_playbook(task_desc)
        ok, reason = self.validate_yaml(pb_text)
        if not ok:
            print(Panel(f"초안 YAML 검증 실패: {reason}\nLLM 결과:\n{pb_text[:1200]}", title="Validator", style="red"))
            return

        warns = self.lint_heuristics(pb_text)
        if warns:
            print(Panel("\n".join(warns), title="Heuristic Lint Warnings", style="yellow"))

        pb_path = self.work_dir / "generated.yaml"
        pb_path.write_text(pb_text)

        syn_ok, syn_out = self.run_syntax_check(pb_path.name)
        print(Panel(syn_out, title="Syntax Check", style=("green" if syn_ok else "red")))
        if not syn_ok and verify in ("syntax", "all"):
            pb_text = self.fix_playbook(task_desc, "syntax-error", syn_out)
            pb_path.write_text(pb_text)

        dry_ok, dry_sum, dry_err = self.run_check_mode(pb_path.name)
        self._print_summary(dry_sum, dry_err, title="Dry-Run Summary")
        if not dry_ok and verify in ("dry", "all"):
            compact_summary = json.dumps(dry_sum, ensure_ascii=False)
            compact_errors = "\n".join(dry_err)[:1500]
            pb_text = self.fix_playbook(task_desc, compact_summary, compact_errors)
            pb_path.write_text(pb_text)

        attempt = 1
        while attempt <= max_attempts:
            ok_run, summary, errors = self.run_playbook(pb_path.name)
            self._print_summary(summary, errors, title=f"Attempt #{attempt} - Execution Summary")
            if ok_run:
                print(Panel("✅ 1차 실행 성공", style="green"))
                break
            attempt += 1
            if attempt > max_attempts:
                print(Panel("최대 재시도 도달. 종료", style="yellow"))
                return
            compact_summary = json.dumps(summary, ensure_ascii=False)
            compact_errors = "\n".join(errors)[:1500]
            fixed = self.fix_playbook(task_desc, compact_summary, compact_errors)
            ok_val, reason2 = self.validate_yaml(fixed)
            if ok_val:
                pb_path.write_text(fixed)
            else:
                print(Panel(f"수정 YAML 검증 실패: {reason2}", style="red"))
                return

        idem_ok, idem_summaries = self.run_idempotency_check(pb_path.name)
        tstyle = "green" if idem_ok else "yellow"
        print(Panel(json.dumps(idem_summaries.get("second", {}), ensure_ascii=False, indent=2),
                    title="Idempotency (2nd run) Summary", style=tstyle))

    def _only_yaml(self, text: str) -> str:
        t = text.strip()
        fences = ("```yaml", "```yml", "```",)
        for f in fences:
            if t.startswith(f):
                t = t[len(f):]
        if t.endswith("```"):
            t = t[:-3]
        return t.strip()

    def _print_summary(self, summary: Dict[str, Any], errors: List[str], title: str = "Ansible Summary"):
        table = Table(title=title, box=box.SIMPLE_HEAVY)
        table.add_column("Host", style="cyan")
        table.add_column("ok", justify="right")
        table.add_column("changed", justify="right")
        table.add_column("failed", justify="right")
        table.add_column("unreachable", justify="right")
        for host, s in (summary.get("stats") or {}).items():
            table.add_row(
                host,
                str(s.get("ok", 0)),
                str(s.get("changed", 0)),
                str(s.get("failures", s.get("failed", 0))),
                str(s.get("unreachable", 0)),
            )
        console.print(table)
        if errors:
            err = "\n".join(errors[:10])
            print(Panel(err, title="Errors (truncated)", style="red"))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--base_url", default="http://127.0.0.1:8000/v1", help="OpenAI-compatible base URL (llama.cpp server)" )
    parser.add_argument("--api_key", default="sk-noauth", help="Dummy key if server doesn't require auth" )
    parser.add_argument("--model", default="local-llama", help="Model name to pass to API (llama.cpp usually ignores)" )
    parser.add_argument("--inventory", default="example_inventory.ini")
    parser.add_argument("--task", required=True, help="자연어 작업 설명")
    parser.add_argument("--workdir", default="workdir")
    parser.add_argument("--verify", choices=["none","syntax","dry","all"], default="all")
    args = parser.parse_args()

    llm = OpenAIChatLLM(base_url=args.base_url, api_key=args.api_key, model=args.model)
    agent = PlaybookAgent(llm, inventory_path=args.inventory, work_dir=args.workdir)
    agent.run_task(args.task, verify=args.verify, max_attempts=2)