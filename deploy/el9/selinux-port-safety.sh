#!/usr/bin/env bash
# SELinux coexistence helpers for the one-click EL9 deployment.
#
# This file is sourced by one-click-deploy.sh.  It intentionally does not set
# shell options or mutate host state when sourced, which also lets the test
# suite exercise the exact production functions with fixture command shims.

wkm_selinux_types_for_tcp_port() {
  local wanted="$1"
  awk -v wanted="${wanted}" '
    $2 == "tcp" {
      port_type = $1
      for (field_idx = 3; field_idx <= NF; field_idx++) {
        value = $field_idx
        gsub(/,/, "", value)
        if (value !~ /^[0-9]+(-[0-9]+)?$/) {
          continue
        }
        bound_count = split(value, bounds, "-")
        lower = bounds[1] + 0
        upper = (bound_count == 2 ? bounds[2] + 0 : lower)
        if ((wanted + 0) >= lower && (wanted + 0) <= upper) {
          matching_types[port_type] = 1
        }
      }
    }
    END {
      for (port_type in matching_types) {
        print port_type
      }
    }
  '
}

configure_selinux() {
  local selinux_mode port_listing assigned_types boolean_state assigned_type

  if ! selinux_mode="$(getenforce 2>/dev/null)"; then
    echo "无法确定 SELinux 状态，拒绝修改端口标签。" >&2
    return 1
  fi
  case "${selinux_mode}" in
    Disabled)
      echo "SELinux 已禁用；跳过端口、布尔值和文件标签操作。"
      return 0
      ;;
    Enforcing|Permissive)
      ;;
    *)
      echo "无法识别 SELinux 状态：${selinux_mode:-<empty>}，拒绝继续。" >&2
      return 1
      ;;
  esac

  # Read the complete assignment table before attempting an add.  semanage
  # treats `port -a` on an already assigned port as a modifying operation on
  # some EL9 builds; pre-classification prevents that unsafe fallback.
  if ! port_listing="$(LC_ALL=C semanage port -l)"; then
    echo "无法读取 SELinux 端口类型表，拒绝修改 ${PUBLIC_PORT}/tcp。" >&2
    return 1
  fi
  assigned_types="$(
    printf '%s\n' "${port_listing}" |
      wkm_selinux_types_for_tcp_port "${PUBLIC_PORT}"
  )"

  if [[ -n "${assigned_types}" ]]; then
    while IFS= read -r assigned_type; do
      [[ -n "${assigned_type}" ]] || continue
      if [[ "${assigned_type}" != "http_port_t" ]]; then
        echo "${PUBLIC_PORT}/tcp 已属于 SELinux 类型 ${assigned_type}；为避免破坏同机业务，拒绝新增或改写。" >&2
        return 1
      fi
    done <<<"${assigned_types}"
  else
    semanage port -a -t http_port_t -p tcp "${PUBLIC_PORT}"
    SELINUX_PORT_ADDED=1
  fi

  if ! boolean_state="$(LC_ALL=C getsebool httpd_can_network_connect)"; then
    echo "无法读取 SELinux 布尔值 httpd_can_network_connect。" >&2
    return 1
  fi
  if grep -Eq -- '-->[[:space:]]+on[[:space:]]*$' <<<"${boolean_state}"; then
    :
  elif grep -Eq -- '-->[[:space:]]+off[[:space:]]*$' <<<"${boolean_state}"; then
    setsebool -P httpd_can_network_connect 1
    SELINUX_BOOLEAN_CHANGED=1
  else
    echo "SELinux 布尔值返回了无法识别的状态，拒绝继续：${boolean_state}" >&2
    return 1
  fi
  restorecon -RF /var/www/wechat-memory-license-acme
}
