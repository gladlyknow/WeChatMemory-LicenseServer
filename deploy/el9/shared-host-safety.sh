#!/usr/bin/env bash
# Read-only coexistence checks for an EL9 host where Nginx also serves other
# applications.  The deployment owns exactly one conf.d file and must not
# silently claim another site's server_name or listening port.

WKM_NGINX_OWNER_MARKER="wkm-owner-id: wechat-memory-license"

wkm_assert_owned_nginx_file() {
  local owned_file="$1"
  [[ -e "${owned_file}" ]] || return 0
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
