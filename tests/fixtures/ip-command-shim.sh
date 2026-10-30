#!/usr/bin/env bash
set -Eeuo pipefail

printf 'ip %s\n' "$*" >>"${WKM_TEST_CALLS}"
if [[ "$*" != "-o -4 addr show scope global" ]]; then
  echo "unexpected ip arguments: $*" >&2
  exit 97
fi
if [[ -n "${WKM_TEST_IP_OUTPUT:-}" ]]; then
  printf '%s\n' "${WKM_TEST_IP_OUTPUT}"
fi
exit "${WKM_TEST_IP_EXIT:-0}"
