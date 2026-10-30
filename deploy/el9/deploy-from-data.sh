#!/usr/bin/env bash
set -euo pipefail
umask 077

SOURCE_DIR="/data/WeChatMemory-LicenseServer"
ADMIN_EMAIL="gladlygrady@tutamail.com"
SIGNING_KEY="/root/wkm-signing-ed25519.pem"
KEY_ID="2026-10-30"
PUBLIC_IP="104.244.90.172"
PUBLIC_PORT="9000"
TLS_MODE="http"
TLS_CERT=""
TLS_KEY=""
CERTBOT_VERSION="5.9.0"
FIREWALL_ZONE=""
RETIRE_NGINX_CONF=""
ALLOW_UNBOUND_PUBLIC_IP=0
ACK_CLIENT_HTTPS_ORIGIN_READY=0
UPDATE_SOURCE=1

usage() {
  cat <<'EOF'
用法：sudo bash /data/WeChatMemory-LicenseServer/deploy/el9/deploy-from-data.sh [选项]

首次部署：
  --signing-key PATH       与客户端公钥配对的 Ed25519 私钥 PEM

可选：
  --source PATH            源码目录，默认 /data/WeChatMemory-LicenseServer
  --email ADDRESS          letsencrypt 模式的证书通知邮箱
  --key-id ID              签名密钥 ID，默认 2026-10-30
  --public-ip IP           默认 104.244.90.172
  --public-port PORT       默认 9000
  --tls-mode MODE          http（默认）、letsencrypt 或 existing
  --tls-cert PATH          existing 模式的完整证书链
  --tls-key PATH           existing 模式的 TLS 私钥
  --certbot-version VER    letsencrypt 模式的 Certbot 版本
  --firewall-zone ZONE     多 firewalld zone 主机的公网 zone
  --retire-nginx-conf PATH 显式退役抢占目标 IP 的已停用 conf.d 普通文件
  --allow-unbound-public-ip
                           仅限已人工验证的 1:1 NAT/EIP；跳过本机 IP 归属门禁
  --ack-client-https-origin-ready
                           TLS 专用；确认已先发布匹配 HTTPS origin/
                           信任策略的客户端；当前 0.3.5 不兼容
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
    --tls-mode) TLS_MODE="$2"; shift 2 ;;
    --tls-cert) TLS_CERT="$2"; shift 2 ;;
    --tls-key) TLS_KEY="$2"; shift 2 ;;
    --certbot-version) CERTBOT_VERSION="$2"; shift 2 ;;
    --firewall-zone) FIREWALL_ZONE="$2"; shift 2 ;;
    --retire-nginx-conf) RETIRE_NGINX_CONF="$2"; shift 2 ;;
    --allow-unbound-public-ip) ALLOW_UNBOUND_PUBLIC_IP=1; shift ;;
    --ack-client-https-origin-ready) ACK_CLIENT_HTTPS_ORIGIN_READY=1; shift ;;
    --skip-update) UPDATE_SOURCE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ "${TLS_MODE}" != "http" && "${TLS_MODE}" != "letsencrypt" && \
      "${TLS_MODE}" != "existing" ]]; then
  echo "--tls-mode 只能是 http、letsencrypt 或 existing。" >&2
  exit 2
fi
if [[ "${TLS_MODE}" != "http" && "${ACK_CLIENT_HTTPS_ORIGIN_READY}" != "1" ]]; then
  echo "TLS 模式与当前 Client 0.3.5 的固定 HTTP origin 不兼容。" >&2
  echo "先发布匹配 HTTPS origin/信任策略的新客户端，再显式传入 --ack-client-https-origin-ready。" >&2
  exit 2
fi

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
  --tls-mode "${TLS_MODE}"
  --signing-key "${SIGNING_KEY}"
)
if [[ "${TLS_MODE}" == "existing" ]]; then
  arguments+=(--tls-cert "${TLS_CERT}" --tls-key "${TLS_KEY}")
elif [[ "${TLS_MODE}" == "letsencrypt" ]]; then
  arguments+=(--certbot-version "${CERTBOT_VERSION}")
fi
if [[ -n "${FIREWALL_ZONE}" ]]; then
  arguments+=(--firewall-zone "${FIREWALL_ZONE}")
fi
if [[ -n "${RETIRE_NGINX_CONF}" ]]; then
  arguments+=(--retire-nginx-conf "${RETIRE_NGINX_CONF}")
fi
if (( ALLOW_UNBOUND_PUBLIC_IP )); then
  arguments+=(--allow-unbound-public-ip)
fi
if (( ACK_CLIENT_HTTPS_ORIGIN_READY )); then
  arguments+=(--ack-client-https-origin-ready)
fi

exec bash "${SOURCE_DIR}/deploy/el9/one-click-deploy.sh" "${arguments[@]}"
