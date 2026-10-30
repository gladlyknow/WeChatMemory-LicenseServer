#!/usr/bin/env bash
set -Eeuo pipefail

command_name="$(basename -- "$0")"
printf '%s %s\n' "${command_name}" "$*" >>"${WKM_TEST_CALLS}"

case "${command_name}" in
  getenforce)
    printf '%s\n' "${WKM_TEST_SELINUX_MODE}"
    ;;
  semanage)
    if [[ "$*" == "port -l" ]]; then
      cat -- "${WKM_TEST_PORT_LISTING}"
      exit "${WKM_TEST_LIST_EXIT:-0}"
    fi
    if [[ "$*" == "port -a -t http_port_t -p tcp ${WKM_TEST_PUBLIC_PORT:-9000}" ]]; then
      exit "${WKM_TEST_ADD_EXIT:-0}"
    fi
    echo "unexpected semanage arguments: $*" >&2
    exit 97
    ;;
  getsebool)
    printf 'httpd_can_network_connect --> %s\n' "${WKM_TEST_BOOLEAN_STATE:-on}"
    exit "${WKM_TEST_GETSEBOOL_EXIT:-0}"
    ;;
  setsebool|restorecon)
    ;;
  *)
    echo "unexpected fixture command: ${command_name}" >&2
    exit 98
    ;;
esac
