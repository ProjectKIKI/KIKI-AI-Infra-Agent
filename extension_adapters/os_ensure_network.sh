#!/usr/bin/env bash
set -euo pipefail
name="${1:-}"
cidr="${2:-}"
if [[ -z "$name" || -z "$cidr" ]]; then
  echo '{"stats":{"localhost":{"changed":0,"failed":1,"unreachable":0}}}'
  exit 1
fi

changed=0
rc=0

if ! openstack network show "$name" -f json >/dev/null 2>&1; then
  if openstack network create "$name" -f json >/dev/null 2>&1; then
    changed=1
  else
    rc=1
  fi
fi

if ! openstack subnet show "${name}-subnet" -f json >/dev/null 2>&1; then
  if openstack subnet create --network "$name" --subnet-range "$cidr" "${name}-subnet" -f json >/dev/null 2>&1; then
    changed=1
  else
    rc=1
  fi
fi

failed=$([[ $rc -ne 0 ]] && echo 1 || echo 0)
echo "{"stats":{"localhost":{"changed":$changed,"failed":$failed,"unreachable":0}}}"
exit $rc
