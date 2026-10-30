#!/usr/bin/env bash
# Shared, dependency-free primitives for deployment compensation.
#
# This file is sourced by the EL9 installers and by the executable transaction
# self-test.  It deliberately manages only files and symbolic links; callers
# remain responsible for restoring systemd service state in the correct order.

wkm_normalize_decimal() {
  local value="${1:-}"
  local zero_prefix=""
  [[ "${value}" =~ ^[0-9]+$ ]] || return 2
  zero_prefix="${value%%[!0]*}"
  value="${value#"${zero_prefix}"}"
  printf '%s' "${value:-0}"
}

wkm_decimal_at_least() {
  local left=""
  local right=""
  local LC_ALL=C
  left="$(wkm_normalize_decimal "${1:-}")" || return 2
  right="$(wkm_normalize_decimal "${2:-}")" || return 2
  if (( ${#left} != ${#right} )); then
    (( ${#left} > ${#right} ))
    return
  fi
  [[ "${left}" == "${right}" || "${left}" > "${right}" ]]
}

wkm_version_at_least() {
  local version="${1:-}"
  local minimum_major="${2:-}"
  local minimum_minor="${3:-}"
  local major=""
  local minor=""
  [[ "${version}" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || return 2
  major="${BASH_REMATCH[1]}"
  minor="${BASH_REMATCH[2]}"
  if ! wkm_decimal_at_least "${major}" "${minimum_major}"; then
    return 1
  fi
  if ! wkm_decimal_at_least "${minimum_major}" "${major}"; then
    return 0
  fi
  wkm_decimal_at_least "${minor}" "${minimum_minor}"
}

wkm_tx_require_name() {
  local name="${1:-}"
  if [[ ! "${name}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "部署事务标签不合法：${name}" >&2
    return 2
  fi
}

wkm_tx_capture_file() {
  local transaction_dir="$1"
  local path="$2"
  local name="$3"
  wkm_tx_require_name "${name}" || return
  install -d -m 0700 "${transaction_dir}"
  if [[ -f "${path}" ]]; then
    printf 'present\n' >"${transaction_dir}/${name}.state"
    cp -a -- "${path}" "${transaction_dir}/${name}.backup"
  elif [[ -e "${path}" || -L "${path}" ]]; then
    echo "部署事务只能捕获普通文件：${path}" >&2
    return 2
  else
    printf 'absent\n' >"${transaction_dir}/${name}.state"
  fi
}

wkm_tx_restore_file() {
  local transaction_dir="$1"
  local path="$2"
  local name="$3"
  local state=""
  local temporary="${path}.wkm-rollback.$$"
  wkm_tx_require_name "${name}" || return
  state="$(<"${transaction_dir}/${name}.state")"
  case "${state}" in
    present)
      rm -f -- "${temporary}"
      cp -a -- "${transaction_dir}/${name}.backup" "${temporary}"
      mv -Tf -- "${temporary}" "${path}"
      ;;
    absent)
      if [[ -d "${path}" && ! -L "${path}" ]]; then
        echo "拒绝在回滚时删除目录：${path}" >&2
        return 2
      fi
      rm -f -- "${path}"
      ;;
    *)
      echo "部署事务状态损坏：${name}=${state}" >&2
      return 2
      ;;
  esac
}

wkm_tx_capture_link() {
  local transaction_dir="$1"
  local path="$2"
  local name="$3"
  wkm_tx_require_name "${name}" || return
  install -d -m 0700 "${transaction_dir}"
  if [[ -L "${path}" ]]; then
    printf 'present\n' >"${transaction_dir}/${name}.state"
    readlink -- "${path}" >"${transaction_dir}/${name}.target"
  elif [[ -e "${path}" ]]; then
    echo "期望软链接但遇到其他路径：${path}" >&2
    return 2
  else
    printf 'absent\n' >"${transaction_dir}/${name}.state"
  fi
}

wkm_tx_restore_link() {
  local transaction_dir="$1"
  local path="$2"
  local name="$3"
  local state=""
  local target=""
  local temporary="${path}.wkm-rollback.$$"
  wkm_tx_require_name "${name}" || return
  state="$(<"${transaction_dir}/${name}.state")"
  case "${state}" in
    present)
      target="$(<"${transaction_dir}/${name}.target")"
      rm -f -- "${temporary}"
      ln -s -- "${target}" "${temporary}"
      mv -Tf -- "${temporary}" "${path}"
      ;;
    absent)
      if [[ -d "${path}" && ! -L "${path}" ]]; then
        echo "拒绝在回滚时删除目录：${path}" >&2
        return 2
      fi
      rm -f -- "${path}"
      ;;
    *)
      echo "部署事务状态损坏：${name}=${state}" >&2
      return 2
      ;;
  esac
}

wkm_deploy_checkpoint() {
  local checkpoint="$1"
  local requested="${WKM_DEPLOY_TEST_FAULT:-}"
  if [[ -z "${requested}" ]]; then
    return 0
  fi
  if [[ "${WKM_DEPLOY_ENABLE_FAULT_INJECTION:-0}" != "1" ]]; then
    echo "检测到故障注入参数，但未显式启用测试模式。" >&2
    return 2
  fi
  if [[ "${requested}" == "${checkpoint}" ]]; then
    echo "已在部署检查点注入故障：${checkpoint}" >&2
    return 97
  fi
}
