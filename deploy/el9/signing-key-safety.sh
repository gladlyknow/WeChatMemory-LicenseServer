#!/usr/bin/env bash
# Metadata-only gates for production Ed25519 private-key files.  These helpers
# never read or print key bytes.  Cryptographic matching remains the job of the
# release candidate's `signing-key verify` command.

wkm_assert_private_key_file() {
  local path="${1:-}"
  local expected_uid="${2:-}"
  local expected_gid="${3:-}"
  local expected_mode="${4:-}"
  local label="${5:-私钥}"
  local metadata=""

  if [[ -z "${path}" || -L "${path}" || ! -f "${path}" ]]; then
    echo "${label}必须是非软链接普通文件：${path:-<empty>}" >&2
    return 2
  fi
  metadata="$(stat -c '%u:%g:%a' -- "${path}")" || return 2
  if [[ "${metadata}" != "${expected_uid}:${expected_gid}:${expected_mode}" ]]; then
    echo "${label}文件元数据不安全：${path}；要求 uid:gid:mode=${expected_uid}:${expected_gid}:${expected_mode}，实际=${metadata}" >&2
    return 2
  fi
}

wkm_assert_staged_signing_key() {
  wkm_assert_private_key_file "${1:-}" 0 0 600 "首次签名私钥源"
}

wkm_assert_installed_signing_key() {
  local path="${1:-}"
  local group_name="${2:-wkm-license}"
  local group_entry=""
  local group_password=""
  local group_id=""
  local group_members=""
  group_entry="$(getent group "${group_name}")" || {
    echo "找不到已安装签名私钥的运行组：${group_name}" >&2
    return 2
  }
  IFS=: read -r _ group_password group_id group_members <<<"${group_entry}"
  [[ "${group_id}" =~ ^[0-9]+$ ]] || return 2
  wkm_assert_private_key_file "${path}" 0 "${group_id}" 640 "已安装版本化签名私钥"
}
