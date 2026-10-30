#!/usr/bin/env bash
set -euo pipefail
umask 077

SOURCE_DIR="/data/WeChatMemory-LicenseServer"
ADMIN_EMAIL="gladlygrady@tutamail.com"
SIGNING_KEY="/root/wkm-signing-ed25519.pem"
KEY_ID="2026-10-30"
PUBLIC_IP="104.244.90.172"
PUBLIC_PORT="9000"
FIREWALL_ZONE=""
RETIRE_NGINX_CONF=""
ALLOW_UNBOUND_PUBLIC_IP=0
UPDATE_SOURCE=1

usage() {
  cat <<'EOF'
用法：sudo bash /data/WeChatMemory-LicenseServer/deploy/el9/deploy-from-data.sh [选项]

首次部署：
  --signing-key PATH       与客户端公钥配对的 Ed25519 私钥 PEM

可选：
  --source PATH            源码目录，默认 /data/WeChatMemory-LicenseServer
  --email ADDRESS          证书通知邮箱，默认 gladlygrady@tutamail.com
  --key-id ID              签名密钥 ID，默认 2026-10-30
  --public-ip IP           默认 104.244.90.172
  --public-port PORT       默认 9000
  --firewall-zone ZONE     多 firewalld zone 主机的公网 zone
  --retire-nginx-conf PATH 显式退役抢占目标 IP 的已停用 conf.d 普通文件
  --allow-unbound-public-ip
                           仅限已人工验证的 1:1 NAT/EIP；跳过本机 IP 归属门禁
  --skip-update            不从 origin/main 执行 fast-forward 更新

脚本只接受干净的 main 工作树。它不生成、输出或提交私钥。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE_DIR="$2"; shift 2 ;;
    --email) ADMIN_EMAIL="$2"; shift 2 ;;
    --signing-key) SIGNING_KEY="$2"; shift 2 ;;
    --key-id) KEY_ID="$2"; shift 2 ;;
    --public-ip) PUBLIC_IP="$2"; shift 2 ;;
    --public-port) PUBLIC_PORT="$2"; shift 2 ;;
    --firewall-zone) FIREWALL_ZONE="$2"; shift 2 ;;
    --retire-nginx-conf) RETIRE_NGINX_CONF="$2"; shift 2 ;;
    --allow-unbound-public-ip) ALLOW_UNBOUND_PUBLIC_IP=1; shift ;;
    --skip-update) UPDATE_SOURCE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "请通过 sudo 或 root 执行。" >&2
  exit 2
fi
SOURCE_DIR="$(readlink -f "${SOURCE_DIR}")"
if [[ "${SOURCE_DIR}" != "/data/WeChatMemory-LicenseServer" ]]; then
  echo "警告：当前使用非默认源码目录 ${SOURCE_DIR}" >&2
fi
if [[ ! -d "${SOURCE_DIR}/.git" || ! -f "${SOURCE_DIR}/deploy/el9/one-click-deploy.sh" ]]; then
  echo "找不到完整的服务端 Git 工作树：${SOURCE_DIR}" >&2
  exit 3
fi
if [[ -n "$(git -C "${SOURCE_DIR}" status --porcelain)" ]]; then
  echo "源码工作树不干净，拒绝部署未审核的本地变更。" >&2
  exit 3
fi
if [[ "$(git -C "${SOURCE_DIR}" branch --show-current)" != "main" ]]; then
  echo "只允许从 main 分支部署。" >&2
  exit 3
fi
if (( UPDATE_SOURCE )); then
  git -C "${SOURCE_DIR}" fetch --prune origin main
  git -C "${SOURCE_DIR}" merge --ff-only FETCH_HEAD
fi

arguments=(
  --source "${SOURCE_DIR}"
  --email "${ADMIN_EMAIL}"
  --key-id "${KEY_ID}"
  --public-ip "${PUBLIC_IP}"
  --public-port "${PUBLIC_PORT}"
  --signing-key "${SIGNING_KEY}"
)
if [[ -n "${FIREWALL_ZONE}" ]]; then
  arguments+=(--firewall-zone "${FIREWALL_ZONE}")
fi
if [[ -n "${RETIRE_NGINX_CONF}" ]]; then
  arguments+=(--retire-nginx-conf "${RETIRE_NGINX_CONF}")
fi
if (( ALLOW_UNBOUND_PUBLIC_IP )); then
  arguments+=(--allow-unbound-public-ip)
fi

exec bash "${SOURCE_DIR}/deploy/el9/one-click-deploy.sh" "${arguments[@]}"
