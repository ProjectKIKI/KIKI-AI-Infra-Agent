#!/usr/bin/env bash
set -euo pipefail
mf="${1:-}"
if [[ -z "$mf" || ! -f "$mf" ]]; then
  echo '{"stats":{"localhost":{"changed":0,"failed":1,"unreachable":0}}}'
  exit 1
fi

changed=0
if kubectl diff -f "$mf" >/dev/null 2>&1; then
  changed=1
fi

rc=0
if ! kubectl apply --server-side --field-manager=agent -f "$mf" >/dev/null 2>&1; then
  rc=1
fi

failed=$([[ $rc -ne 0 ]] && echo 1 || echo 0)
echo "{"stats":{"localhost":{"changed":$changed,"failed":$failed,"unreachable":0}}}"
exit $rc
