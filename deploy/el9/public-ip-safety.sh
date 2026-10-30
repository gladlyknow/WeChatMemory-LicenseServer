#!/usr/bin/env bash
# Public IPv4 ownership preflight for the EL9 deployment entrypoints.
#
# This file is sourced by one-click-deploy.sh.  It performs read-only host
# inspection and deliberately has no side effects when sourced, so the same
# production functions can be exercised with a command shim in tests.

wkm_global_ipv4_addresses() {
  local command_output line family address

  if ! command -v ip >/dev/null 2>&1; then
    echo "无法找到 ip 命令，不能核对公网 IPv4 的本机归属。" >&2
    return 1
  fi
  if ! command_output="$(LC_ALL=C ip -o -4 addr show scope global 2>&1)"; then
    echo "读取本机 scope global IPv4 失败，拒绝继续部署。" >&2
    if [[ -n "${command_output}" ]]; then
      printf '%s\n' "${command_output}" >&2
    fi
    return 1
  fi

  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    # `ip -o` produces one address per line.  The first four fields are:
    # index, interface, family and address/prefix.
    read -r _ _ family address _ <<<"${line}"
    [[ "${family:-}" == "inet" && -n "${address:-}" ]] || continue
    address="${address%%/*}"
    if [[ "${address}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
      printf '%s\n' "${address}"
    fi
  done <<<"${command_output}"
}

wkm_assert_public_ip_bound() {
  local public_ip="$1"
  local allow_unbound="${2:-0}"
  local addresses address

  if [[ "${allow_unbound}" == "1" ]]; then
    {
      echo "========================================================================"
      echo "严重警告：已启用 --allow-unbound-public-ip，跳过公网 IP 本机绑定门禁。"
      echo "目标 IP：${public_ip}"
      echo "仅当云平台 1:1 NAT/EIP 映射和外部 TCP/80 探针均已人工验证时才允许继续。"
      echo "========================================================================"
    } >&2
    return 0
  fi

  if ! addresses="$(wkm_global_ipv4_addresses)"; then
    return 1
  fi
  while IFS= read -r address; do
    if [[ "${address}" == "${public_ip}" ]]; then
      return 0
    fi
  done <<<"${addresses}"

  echo "公网 IP ${public_ip} 不在本机 scope global IPv4 列表中，拒绝继续部署。" >&2
  if [[ -n "${addresses}" ]]; then
    echo "本机检测到的 scope global IPv4：" >&2
    while IFS= read -r address; do
      [[ -n "${address}" ]] && printf '  - %s\n' "${address}" >&2
    done <<<"${addresses}"
  else
    echo "本机未检测到任何 scope global IPv4。" >&2
  fi
  echo "请修正 --public-ip；仅对已人工验证的 1:1 NAT/EIP 映射使用 --allow-unbound-public-ip。" >&2
  return 1
}
