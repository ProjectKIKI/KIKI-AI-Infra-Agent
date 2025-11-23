#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
kiki_ai_healthd.py

컨테이너 안에서 주기적으로 `kiki health-collect`와 동일한 로직을 실행하는 데몬.

예시:

  python kiki_ai_healthd.py     --interval 60     --db /data/kiki/metrics.db     --inventory "k8s-master[1:3],k8s-worker[1:5]"     --source k8s-node
"""

import argparse
import signal
import sys
import time

from kiki import cmd_health_collect


_stop_flag = False


def _handle_sigterm(signum, frame):
    global _stop_flag
    _stop_flag = True


def main() -> None:
    p = argparse.ArgumentParser(description="KIKI Health Daemon (kiki-ai-healthd)")
    p.add_argument("--interval", type=int, default=60, help="수집 주기(초)")
    p.add_argument("--db", default="/data/kiki/metrics.db", help="SQLite DB 경로")
    p.add_argument("--inventory", required=True, help="Ansible 인벤토리 표현(설명용 텍스트도 가능)")
    p.add_argument("--source", default="k8s-node", help="수집 소스 태그")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--profile", default="basic", help="내장 헬스 프로파일 이름")
    g.add_argument("--playbook", help="사용자 정의 health playbook 경로")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    print(f"[KIKI][healthd] start: interval={args.interval}, source={args.source}, db={args.db}")

    while not _stop_flag:
        start = time.time()
        try:
            collect_args = argparse.Namespace(
                db=args.db,
                inventory=args.inventory,
                source=args.source,
                profile=args.profile,
                playbook=args.playbook,
                debug=args.debug,
            )
            cmd_health_collect(collect_args)
        except Exception as e:
            print(f"[KIKI][healthd] ERROR during collect: {e}", file=sys.stderr)

        elapsed = time.time() - start
        sleep_sec = max(1, args.interval - int(elapsed))
        for _ in range(sleep_sec):
            if _stop_flag:
                break
            time.sleep(1)

    print("[KIKI][healthd] stopped.")


if __name__ == "__main__":
    main()
