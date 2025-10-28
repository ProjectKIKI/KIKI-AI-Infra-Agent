#!/usr/bin/env python3
import argparse, requests, sys, os, json
from pathlib import Path

def load_vars(path: str):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        print(f"[warn] extra-vars file not found: {p}", file=sys.stderr)
        return None
    text = p.read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text)
    except Exception:
        try:
            return json.loads(text)
        except Exception:
            print("[warn] failed to parse extra-vars (YAML/JSON)", file=sys.stderr)
            return None

def main():
    ap = argparse.ArgumentParser(prog="kiki", description="LLM→Ansible playbook generator & runner (daemon client)")
    ap.add_argument("--message", required=True)
    ap.add_argument("--model", default="local-llama")
    ap.add_argument("--max-token", type=int, default=256)
    ap.add_argument("--temperature", type=float, default=0.5)

    ap.add_argument("--inventory", required=True, help="파일 경로 또는 'host1,host2'")
    ap.add_argument("--inventory-file", help="inventory 파일을 본문으로 업로드(마운트 불필요). 제공 시 --inventory는 무시")

    ap.add_argument("--name", help="플레이북 베이스 이름(확장자 제외)")
    ap.add_argument("--engine", choices=["runner","ansible"], default="runner")
    ap.add_argument("--verify", choices=["none","syntax","all"], default="all")
    ap.add_argument("--user", default="rocky")
    ap.add_argument("--ssh-key", default=os.path.expanduser("~/.ssh/id_rsa"))
    ap.add_argument("--base-url", default="http://127.0.0.1:8082")
    ap.add_argument("--extra-vars-file", help="json|yaml")
    ap.add_argument("--yes", action="store_true", help="리뷰 없이 바로 실행")
    ap.add_argument("--no-follow", action="store_true", help="스트리밍 끄기(runner)")
    ap.add_argument("--out", help="번들 저장 경로 (기본: ./bundle-<task>.zip)")
    args = ap.parse_args()

    gen_payload = {
        "message": args.message,
        "model": args.model,
        "max_token": args.max_token,
        "temperature": args.temperature,
        "name": args.name,
    }
    r = requests.post(f"{args.base_url}/api/v1/generate", json=gen_payload, timeout=600)
    r.raise_for_status()
    gen = r.json()
    task_id = gen["task_id"]
    run_dir = gen["run_dir"]
    playbook_name = gen["playbook_name"]

    print("\n===== Generated Playbook (preview) =====")
    print(gen["playbook_preview"])
    print("========================================")
    print(f"file: {run_dir}/project/{playbook_name}")

    if not args.yes:
        try:
            ans = input("Execute this playbook now? [y/N]: ").strip().lower()
            if ans not in ("y","yes"):
                print("Cancelled. You can run later inside agent container.")
                return
        except KeyboardInterrupt:
            print("\nCancelled.")
            return

    extra_vars = load_vars(args.extra_vars_file)
    run_payload = {
        "task_id": task_id,
        "inventory": args.inventory,
        "engine": args.engine,
        "verify": args.verify,
        "user": args.user,
        "ssh_key": args.ssh_key,
        "extra_vars": extra_vars,
    }

    if args.inventory_file:
        invp = Path(args.inventory_file)
        if not invp.exists():
            print(f"[error] --inventory-file 경로 없음: {invp}", file=sys.stderr)
            sys.exit(2)
        run_payload["inventory_file_content"] = invp.read_text(encoding="utf-8")

    if args.engine == "ansible" or args.no_follow:
        r2 = requests.post(f"{args.base_url}/api/v1/run", json=run_payload, timeout=None)
        r2.raise_for_status()
        out = r2.json()
        print("\n=== Run Summary ===")
        print(json.dumps(out.get("summary", {}), ensure_ascii=False, indent=2))
        print("RC:", out.get("rc"))
        bundle = out.get("bundle")
        if bundle:
            dst = args.out or f"./bundle-{task_id}.zip"
            try:
                with requests.get(f"{args.base_url}/api/v1/bundle/{task_id}", stream=True) as resp:
                    resp.raise_for_status()
                    with open(dst, "wb") as fh:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk: fh.write(chunk)
                print(f"bundle saved: {dst}")
            except Exception as e:
                print(f"[warn] bundle download failed: {e}")
        return

    with requests.post(f"{args.base_url}/api/v1/run", json=run_payload, stream=True) as s:
        s.raise_for_status()
        print("\n=== Streaming ansible-runner events ===")
        for line in s.iter_lines():
            if not line:
                continue
            if line.startswith(b"data: "):
                payload = json.loads(line[len(b"data: "):].decode("utf-8"))
                t = payload.get("type")
                if t == "event":
                    out = payload.get("stdout") or ""
                    if out.strip():
                        print(out, flush=True)
                elif t == "summary":
                    print("\n=== Run Summary ===")
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                elif t == "bundle":
                    dst = args.out or f"./bundle-{task_id}.zip"
                    try:
                        with requests.get(f"{args.base_url}/api/v1/bundle/{task_id}", stream=True) as resp:
                            resp.raise_for_status()
                            with open(dst, "wb") as fh:
                                for chunk in resp.iter_content(chunk_size=65536):
                                    if chunk: fh.write(chunk)
                        print(f"\nBundle saved: {dst}")
                    except Exception as e:
                        print(f"[warn] bundle download failed: {e}")

if __name__ == "__main__":
    main()
