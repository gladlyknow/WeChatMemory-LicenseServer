#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deployment-transaction.sh
. "${SCRIPT_DIR}/deployment-transaction.sh"

TEST_ROOT="$(mktemp -d)"
cleanup() {
  rm -rf -- "${TEST_ROOT}"
}
trap cleanup EXIT

run_compensation_scenario() {
  local checkpoint="$1"
  local scenario_root="${TEST_ROOT}/${checkpoint}"
  local transaction_dir="${scenario_root}/transaction"

  mkdir -p "${scenario_root}/releases/old" \
    "${scenario_root}/releases/candidate" "${scenario_root}/etc" \
    "${scenario_root}/systemd" "${scenario_root}/etc/tls" \
    "${scenario_root}/expected"
  ln -s "${scenario_root}/releases/old" "${scenario_root}/current"
  printf 'OLD_ENV=1\n' >"${scenario_root}/etc/service.env"
  printf 'old unit\n' >"${scenario_root}/systemd/service"
  printf 'old nginx\n' >"${scenario_root}/etc/nginx.conf"
  printf 'old certificate\nwith trailing bytes\000\377' \
    >"${scenario_root}/etc/tls/fullchain.pem"
  printf 'old private key\nwith trailing bytes\000\376' \
    >"${scenario_root}/etc/tls/privkey.pem"
  cp -- "${scenario_root}/etc/tls/fullchain.pem" \
    "${scenario_root}/expected/fullchain.pem"
  cp -- "${scenario_root}/etc/tls/privkey.pem" \
    "${scenario_root}/expected/privkey.pem"
  chmod 0640 "${scenario_root}/etc/nginx.conf"
  chmod 0640 "${scenario_root}/etc/tls/fullchain.pem" \
    "${scenario_root}/etc/tls/privkey.pem"

  wkm_tx_capture_link "${transaction_dir}" "${scenario_root}/current" current
  wkm_tx_capture_file "${transaction_dir}" \
    "${scenario_root}/etc/service.env" environment
  wkm_tx_capture_file "${transaction_dir}" \
    "${scenario_root}/systemd/service" unit
  wkm_tx_capture_file "${transaction_dir}" \
    "${scenario_root}/etc/nginx.conf" nginx
  wkm_tx_capture_file "${transaction_dir}" \
    "${scenario_root}/etc/tls/fullchain.pem" existing-tls-cert
  wkm_tx_capture_file "${transaction_dir}" \
    "${scenario_root}/etc/tls/privkey.pem" existing-tls-key
  wkm_tx_capture_file "${transaction_dir}" \
    "${scenario_root}/systemd/absent" absent

  ln -sfn "${scenario_root}/releases/candidate" \
    "${scenario_root}/current.next"
  mv -Tf "${scenario_root}/current.next" "${scenario_root}/current"
  printf 'NEW_ENV=1\n' >"${scenario_root}/etc/service.env"
  printf 'new unit\n' >"${scenario_root}/systemd/service"
  printf 'new nginx\n' >"${scenario_root}/etc/nginx.conf"
  printf 'new certificate\n' >"${scenario_root}/etc/tls/fullchain.pem"
  printf 'new private key\n' >"${scenario_root}/etc/tls/privkey.pem"
  chmod 0600 "${scenario_root}/etc/nginx.conf"
  printf 'must disappear\n' >"${scenario_root}/systemd/absent"

  set +e
  WKM_DEPLOY_ENABLE_FAULT_INJECTION=1 \
    WKM_DEPLOY_TEST_FAULT="${checkpoint}" \
    wkm_deploy_checkpoint "${checkpoint}" >/dev/null 2>&1
  local fault_status=$?
  set -e
  [[ ${fault_status} -eq 97 ]]

  # Match the production compensation order: restore application state first,
  # then restore the edge proxy.  The two outer checkpoints prove that a
  # failure after application install or after the Nginx switch restores one
  # coherent pre-deploy snapshot rather than half of a release.
  wkm_tx_restore_link "${transaction_dir}" "${scenario_root}/current" current
  wkm_tx_restore_file "${transaction_dir}" \
    "${scenario_root}/etc/service.env" environment
  wkm_tx_restore_file "${transaction_dir}" \
    "${scenario_root}/systemd/service" unit
  wkm_tx_restore_file "${transaction_dir}" \
    "${scenario_root}/systemd/absent" absent
  wkm_tx_restore_file "${transaction_dir}" \
    "${scenario_root}/etc/tls/fullchain.pem" existing-tls-cert
  wkm_tx_restore_file "${transaction_dir}" \
    "${scenario_root}/etc/tls/privkey.pem" existing-tls-key
  wkm_tx_restore_file "${transaction_dir}" \
    "${scenario_root}/etc/nginx.conf" nginx

  [[ "$(readlink "${scenario_root}/current")" == \
    "${scenario_root}/releases/old" ]]
  [[ "$(<"${scenario_root}/etc/service.env")" == "OLD_ENV=1" ]]
  [[ "$(<"${scenario_root}/systemd/service")" == "old unit" ]]
  [[ "$(<"${scenario_root}/etc/nginx.conf")" == "old nginx" ]]
  cmp -s -- "${scenario_root}/expected/fullchain.pem" \
    "${scenario_root}/etc/tls/fullchain.pem"
  cmp -s -- "${scenario_root}/expected/privkey.pem" \
    "${scenario_root}/etc/tls/privkey.pem"
  [[ "$(stat -c '%a' "${scenario_root}/etc/nginx.conf")" == "640" ]]
  [[ ! -e "${scenario_root}/systemd/absent" ]]
}

run_compensation_scenario after_application_switch
run_compensation_scenario after_application_install
run_compensation_scenario after_nginx_switch

set +e
WKM_DEPLOY_TEST_FAULT=after_application_switch \
  wkm_deploy_checkpoint after_application_switch >/dev/null 2>&1
DISABLED_STATUS=$?
set -e
[[ ${DISABLED_STATUS} -eq 2 ]]

wkm_version_at_least 5.4.0 5 4
wkm_version_at_least 5.9.0 5 4
wkm_version_at_least 5.10.0 5 4
wkm_version_at_least 6.0.0 5 4
wkm_version_at_least 005.004.000 5 4
! wkm_version_at_least 5.3.999 5 4
! wkm_version_at_least 4.999.999 5 4
! wkm_version_at_least invalid 5 4

SYMLINK_TARGET="${TEST_ROOT}/transaction-target"
SYMLINK_PATH="${TEST_ROOT}/transaction-symlink"
printf 'target\n' >"${SYMLINK_TARGET}"
ln -s "${SYMLINK_TARGET}" "${SYMLINK_PATH}"
! wkm_tx_capture_file "${TEST_ROOT}/symlink-transaction" \
  "${SYMLINK_PATH}" symlink-file >/dev/null 2>&1

echo "deployment transaction self-test: ok"
