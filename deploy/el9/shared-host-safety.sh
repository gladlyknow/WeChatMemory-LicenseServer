#!/usr/bin/env bash
# Coexistence and explicit retirement safeguards for an EL9 host where Nginx
# also serves other applications.  The deployment owns exactly one conf.d file
# and must never silently claim or remove another site's virtual host.

WKM_NGINX_OWNER_MARKER="wkm-owner-id: wechat-memory-license"

# Deployment-scoped output from wkm_retire_nginx_conf.  The caller keeps these
# paths so an EXIT compensation can restore the exact original file.  They are
# deliberately outside conf.d: Nginx must stop loading the retired site while
# the byte-identical recovery copy remains available to the operator.
WKM_RETIRED_NGINX_DIR=""
WKM_RETIRED_NGINX_FILE=""
WKM_RETIRED_NGINX_RECORD=""
WKM_RETIRED_NGINX_SHA256=""
WKM_RETIRED_NGINX_AT=""

wkm_validate_retire_nginx_conf() {
  local candidate="${1:-}"
  local owned_file="${2:-}"
  local allowed_dir="${3:-/etc/nginx/conf.d}"
  local public_ip="${4:-}"
  local public_port="${5:-}"
  local canonical=""
  local canonical_dir=""
  local basename=""
  local content=""
  local escaped_ip="${public_ip//./\\.}"

  if [[ -z "${candidate}" || "${candidate}" != /* ]]; then
    echo "--retire-nginx-conf 必须是绝对路径。" >&2
    return 2
  fi
  if [[ -L "${candidate}" || ! -f "${candidate}" ]]; then
    echo "只允许退役已存在的 Nginx 普通文件，拒绝软链接或其他路径：${candidate}" >&2
    return 2
  fi
  canonical="$(readlink -f -- "${candidate}")" || return 2
  canonical_dir="$(readlink -f -- "${allowed_dir}")" || return 2
  if [[ "$(dirname -- "${canonical}")" != "${canonical_dir}" ]]; then
    echo "退役文件必须直接位于 ${canonical_dir}：${candidate}" >&2
    return 2
  fi
  basename="$(basename -- "${canonical}")"
  if [[ ! "${basename}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*\.conf$ ]]; then
    echo "退役文件名必须是安全的 .conf 名称：${basename}" >&2
    return 2
  fi
  if [[ "${canonical}" == "${owned_file}" ]] || \
     { [[ -e "${owned_file}" ]] && [[ "${canonical}" -ef "${owned_file}" ]]; }; then
    echo "拒绝退役 WeChatMemory 自身的 Nginx 配置：${canonical}" >&2
    return 2
  fi
  if grep -Fq "${WKM_NGINX_OWNER_MARKER}" "${canonical}"; then
    echo "拒绝退役带有 WeChatMemory owner marker 的配置：${canonical}" >&2
    return 2
  fi
  if [[ -z "${public_ip}" || ! "${public_port}" =~ ^[0-9]+$ ]]; then
    echo "退役校验缺少目标公网 IP 或端口。" >&2
    return 2
  fi
  content="$(sed 's/[[:space:]]*#.*$//' "${canonical}" | tr '\n' ' ')"
  if ! grep -Eq \
      "server_name[[:space:]]+([^;]*[[:space:]])?${escaped_ip}([[:space:]]|;)" \
      <<<"${content}" && \
     ! grep -Eq \
      "listen[[:space:]]+(\\[::\\]:)?${public_port}([[:space:]]|;)" \
      <<<"${content}"; then
    echo "退役文件未声明目标 IP ${public_ip} 也未监听 ${public_port}，拒绝移动无关站点：${canonical}" >&2
    return 2
  fi
  printf '%s\n' "${canonical}"
}

wkm_write_retired_nginx_record() {
  local status="${1:-}"
  local original_path="${2:-}"
  local public_ip="${3:-}"
  local public_port="${4:-}"
  local temporary=""
  [[ "${status}" == "pending" || "${status}" == "committed" ]] || return 2
  [[ -n "${WKM_RETIRED_NGINX_RECORD}" ]] || return 2
  temporary="${WKM_RETIRED_NGINX_RECORD}.tmp.$$"
  rm -f -- "${temporary}"
  if ! (
    umask 077
    {
      printf 'schema=1\n'
      printf 'status=%s\n' "${status}"
      printf 'retired_at_utc=%s\n' "${WKM_RETIRED_NGINX_AT}"
      printf 'original_path=%s\n' "${original_path}"
      printf 'retired_path=%s\n' "${WKM_RETIRED_NGINX_FILE}"
      printf 'sha256=%s\n' "${WKM_RETIRED_NGINX_SHA256}"
      printf 'replacement_endpoint=https://%s:%s\n' "${public_ip}" "${public_port}"
    } >"${temporary}"
  ); then
    rm -f -- "${temporary}"
    return 2
  fi
  if ! chmod 0600 "${temporary}" || \
     ! mv -Tf -- "${temporary}" "${WKM_RETIRED_NGINX_RECORD}"; then
    rm -f -- "${temporary}"
    return 2
  fi
}

wkm_retire_nginx_conf() {
  local transaction_dir="$1"
  local source="$2"
  local retirement_root="$3"
  local label="$4"
  local public_ip="$5"
  local public_port="$6"
  local basename=""
  local moved_hash=""

  WKM_RETIRED_NGINX_DIR=""
  WKM_RETIRED_NGINX_FILE=""
  WKM_RETIRED_NGINX_RECORD=""
  WKM_RETIRED_NGINX_SHA256=""
  WKM_RETIRED_NGINX_AT=""
  if [[ -L "${source}" || ! -f "${source}" ]]; then
    echo "退役源路径在事务开始前已不是普通文件：${source}" >&2
    return 2
  fi
  basename="$(basename -- "${source}")"
  if [[ -L "${retirement_root}" ]] || \
     { [[ -e "${retirement_root}" ]] && [[ ! -d "${retirement_root}" ]]; }; then
    echo "Nginx 退役备份根路径必须是真实目录，拒绝软链接或其他类型：${retirement_root}" >&2
    return 2
  fi
  install -d -m 0700 "${retirement_root}"
  chmod 0700 "${retirement_root}"
  WKM_RETIRED_NGINX_DIR="$(mktemp -d \
    "${retirement_root}/retirement.XXXXXXXX")"
  chmod 0700 "${WKM_RETIRED_NGINX_DIR}"
  WKM_RETIRED_NGINX_FILE="${WKM_RETIRED_NGINX_DIR}/${basename}"
  WKM_RETIRED_NGINX_RECORD="${WKM_RETIRED_NGINX_DIR}/RETIREMENT.txt"
  WKM_RETIRED_NGINX_SHA256="$(sha256sum -- "${source}" | awk '{print $1}')"
  WKM_RETIRED_NGINX_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  if ! wkm_tx_capture_file "${transaction_dir}" "${source}" "${label}"; then
    rmdir -- "${WKM_RETIRED_NGINX_DIR}" 2>/dev/null || true
    WKM_RETIRED_NGINX_DIR=""
    WKM_RETIRED_NGINX_FILE=""
    WKM_RETIRED_NGINX_RECORD=""
    return 2
  fi
  if ! mv -- "${source}" "${WKM_RETIRED_NGINX_FILE}"; then
    rmdir -- "${WKM_RETIRED_NGINX_DIR}" 2>/dev/null || true
    WKM_RETIRED_NGINX_DIR=""
    WKM_RETIRED_NGINX_FILE=""
    WKM_RETIRED_NGINX_RECORD=""
    return 2
  fi
  if [[ -L "${WKM_RETIRED_NGINX_FILE}" || ! -f "${WKM_RETIRED_NGINX_FILE}" ]]; then
    echo "退役源路径在捕获与移动之间变为非普通文件，拒绝部署并保留取证副本：${WKM_RETIRED_NGINX_FILE}" >&2
    return 2
  fi
  moved_hash="$(sha256sum -- "${WKM_RETIRED_NGINX_FILE}" | awk '{print $1}')"
  if [[ "${moved_hash}" != "${WKM_RETIRED_NGINX_SHA256}" ]]; then
    echo "退役文件在捕获与移动之间发生变化，拒绝部署并保留取证副本：${WKM_RETIRED_NGINX_FILE}" >&2
    return 2
  fi
  wkm_write_retired_nginx_record pending "${source}" "${public_ip}" "${public_port}"
}

wkm_finalize_retired_nginx_conf() {
  local original_path="$1"
  local public_ip="$2"
  local public_port="$3"
  local actual_hash=""
  [[ -n "${WKM_RETIRED_NGINX_FILE}" ]] || return 0
  if [[ -L "${WKM_RETIRED_NGINX_FILE}" || ! -f "${WKM_RETIRED_NGINX_FILE}" ]]; then
    echo "退役副本不再是普通文件，拒绝提交：${WKM_RETIRED_NGINX_FILE}" >&2
    return 2
  fi
  actual_hash="$(sha256sum -- "${WKM_RETIRED_NGINX_FILE}" | awk '{print $1}')"
  if [[ "${actual_hash}" != "${WKM_RETIRED_NGINX_SHA256}" ]]; then
    echo "退役配置在部署期间发生字节变化，拒绝提交：${WKM_RETIRED_NGINX_FILE}" >&2
    return 2
  fi
  wkm_write_retired_nginx_record committed \
    "${original_path}" "${public_ip}" "${public_port}"
}

wkm_restore_retired_nginx_conf() {
  local transaction_dir="$1"
  local original_path="$2"
  local label="$3"
  local expected_hash="${WKM_RETIRED_NGINX_SHA256}"
  local retired_hash=""

  [[ -n "${WKM_RETIRED_NGINX_FILE}" ]] || return 0
  if [[ -e "${original_path}" || -L "${original_path}" ]]; then
    echo "退役后原路径已被其他进程重建，拒绝覆盖；恢复副本保留在：${WKM_RETIRED_NGINX_FILE}" >&2
    return 2
  fi
  if [[ -L "${WKM_RETIRED_NGINX_FILE}" || ! -f "${WKM_RETIRED_NGINX_FILE}" ]]; then
    retired_hash=""
  else
    retired_hash="$(sha256sum -- "${WKM_RETIRED_NGINX_FILE}" | awk '{print $1}')"
  fi
  wkm_tx_restore_file "${transaction_dir}" "${original_path}" "${label}" || return
  if [[ "$(sha256sum -- "${original_path}" | awk '{print $1}')" != "${expected_hash}" ]]; then
    echo "退役配置回滚后字节校验失败，保留恢复副本：${WKM_RETIRED_NGINX_FILE}" >&2
    return 2
  fi
  if [[ "${retired_hash}" != "${expected_hash}" ]]; then
    echo "退役副本在部署期间发生变化；原路径已从事务快照恢复，变化副本继续保留：${WKM_RETIRED_NGINX_FILE}" >&2
    return 2
  fi
  rm -f -- "${WKM_RETIRED_NGINX_FILE}" "${WKM_RETIRED_NGINX_RECORD}"
  rmdir -- "${WKM_RETIRED_NGINX_DIR}" || return 2
  WKM_RETIRED_NGINX_DIR=""
  WKM_RETIRED_NGINX_FILE=""
  WKM_RETIRED_NGINX_RECORD=""
}

wkm_assert_owned_nginx_file() {
  local owned_file="$1"
  [[ -e "${owned_file}" || -L "${owned_file}" ]] || return 0
  if [[ ! -f "${owned_file}" || -L "${owned_file}" ]]; then
    echo "Nginx 目标必须是普通文件，拒绝覆盖：${owned_file}" >&2
    return 1
  fi
  if grep -Fq "${WKM_NGINX_OWNER_MARKER}" "${owned_file}"; then
    return 0
  fi
  # Compatibility for configurations installed by releases before the owner
  # marker was introduced.  Both strings are deliberately project-specific.
  if grep -Fq 'wechat-memory-license-acme.access.log' "${owned_file}" && \
     grep -Fq '/var/www/wechat-memory-license-acme' "${owned_file}"; then
    return 0
  fi
  echo "Nginx 目标文件不属于 WeChatMemory，拒绝覆盖：${owned_file}" >&2
  return 1
}

wkm_foreign_nginx_digest() {
  local nginx_root="$1"
  local owned_file="$2"
  local path
  {
    while IFS= read -r -d '' path; do
      [[ "${path}" == "${owned_file}" ]] && continue
      sha256sum -- "${path}"
    done < <(
      find "${nginx_root}" -xdev \( -type f -o -type l \) \
        \( -name nginx.conf -o -name '*.conf' \) -print0 | sort -z
    )
  } | sha256sum | awk '{print $1}'
}

wkm_assert_no_foreign_nginx_conflict() {
  local nginx_root="$1"
  local owned_file="$2"
  local public_ip="$3"
  local public_port="$4"
  local escaped_ip="${public_ip//./\\.}"
  local path content conflict=0

  while IFS= read -r -d '' path; do
    [[ "${path}" == "${owned_file}" ]] && continue
    # Nginx comments do not participate in ownership.  This intentionally
    # remains fail-closed rather than trying to implement a complete parser.
    content="$(sed 's/[[:space:]]*#.*$//' "${path}" | tr '\n' ' ')"
    if grep -Eq "server_name[[:space:]]+([^;]*[[:space:]])?${escaped_ip}([[:space:]]|;)" \
        <<<"${content}"; then
      echo "其他 Nginx 配置已声明 ${public_ip}，拒绝抢占：${path}" >&2
      conflict=1
    fi
    if grep -Eq "listen[[:space:]]+(\\[::\\]:)?${public_port}([[:space:]]|;)" \
        <<<"${content}"; then
      echo "其他 Nginx 配置已监听 ${public_port}，拒绝抢占：${path}" >&2
      conflict=1
    fi
  done < <(
    find "${nginx_root}" -xdev \( -type f -o -type l \) \
      \( -name nginx.conf -o -name '*.conf' \) -print0 | sort -z
  )
  (( conflict == 0 ))
}

wkm_assert_foreign_nginx_unchanged() {
  local expected="$1"
  local nginx_root="$2"
  local owned_file="$3"
  local actual
  actual="$(wkm_foreign_nginx_digest "${nginx_root}" "${owned_file}")"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "检测到非 WeChatMemory 的 Nginx 配置在部署期间发生变化，拒绝热加载。" >&2
    return 1
  fi
}
