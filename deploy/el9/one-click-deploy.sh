#!/usr/bin/env bash
set -euo pipefail
umask 077

PUBLIC_IP="104.224.90.172"
PUBLIC_PORT="9000"
SOURCE_DIR="/data/WeChatMemory-LicenseServer"
SIGNING_KEY="/root/wkm-signing-ed25519.pem"
KEY_ID="2026-10-30"
EMAIL="gladlygrady@tutamail.com"
TLS_MODE="letsencrypt"
EXISTING_CERT=""
EXISTING_KEY=""
CERTBOT_VERSION="5.9.0"

usage() {
  cat <<'EOF'
用法：sudo bash deploy/el9/one-click-deploy.sh [选项]

必要选项（首次部署）：
  --signing-key PATH       与客户端内置公钥配对的 Ed25519 私钥 PEM
  --email ADDRESS          Let's Encrypt 通知邮箱，默认 gladlygrady@tutamail.com

常用选项：
  --source PATH            当前服务端源码目录，默认 /data/WeChatMemory-LicenseServer
  --public-ip IP           公网 IPv4，默认 104.224.90.172
  --public-port PORT       公网 HTTPS 端口，默认 9000
  --key-id ID              Ed25519 key id，默认 2026-10-30
  --tls-mode MODE          letsencrypt 或 existing
  --tls-cert PATH          existing 模式的完整证书链
  --tls-key PATH           existing 模式的 TLS 私钥
  --certbot-version VER    默认 5.9.0，最低必须为 5.4

升级时服务器已经安装签名私钥和 TLS 证书，可省略 --signing-key 和 --email。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) SOURCE_DIR="$2"; shift 2 ;;
    --signing-key) SIGNING_KEY="$2"; shift 2 ;;
    --key-id) KEY_ID="$2"; shift 2 ;;
    --email) EMAIL="$2"; shift 2 ;;
    --public-ip) PUBLIC_IP="$2"; shift 2 ;;
    --public-port) PUBLIC_PORT="$2"; shift 2 ;;
    --tls-mode) TLS_MODE="$2"; shift 2 ;;
    --tls-cert) EXISTING_CERT="$2"; shift 2 ;;
    --tls-key) EXISTING_KEY="$2"; shift 2 ;;
    --certbot-version) CERTBOT_VERSION="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "请通过 sudo 或 root 执行。" >&2
  exit 2
fi
if [[ ! "${PUBLIC_IP}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "--public-ip 当前只接受 IPv4。" >&2
  exit 2
fi
IFS=. read -r -a IP_PARTS <<<"${PUBLIC_IP}"
for part in "${IP_PARTS[@]}"; do
  if (( 10#${part} < 0 || 10#${part} > 255 )); then
    echo "公网 IPv4 不合法。" >&2
    exit 2
  fi
done
if [[ ! "${PUBLIC_PORT}" =~ ^[0-9]+$ ]] || (( PUBLIC_PORT < 1 || PUBLIC_PORT > 65535 )); then
  echo "公网端口不合法。" >&2
  exit 2
fi
if [[ "${TLS_MODE}" != "letsencrypt" && "${TLS_MODE}" != "existing" ]]; then
  echo "--tls-mode 只能是 letsencrypt 或 existing。" >&2
  exit 2
fi
if [[ ! "${KEY_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "--key-id 只能包含字母、数字、点、下划线和短横线。" >&2
  exit 2
fi
if [[ ! "${CERTBOT_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "--certbot-version 必须是 x.y.z 格式。" >&2
  exit 2
fi
SOURCE_DIR="$(readlink -f "${SOURCE_DIR}")"
if [[ ! -f "${SOURCE_DIR}/pyproject.toml" || ! -f "${SOURCE_DIR}/license_server/app.py" ]]; then
  echo "--source 不是有效的授权服务源码目录。" >&2
  exit 2
fi
if [[ ! -f /etc/os-release ]]; then
  echo "无法识别操作系统。" >&2
  exit 3
fi
. /etc/os-release
if [[ "${VERSION_ID%%.*}" != "9" ]]; then
  echo "该脚本仅支持 Enterprise Linux 9，当前 VERSION_ID=${VERSION_ID:-unknown}。" >&2
  exit 3
fi
VERSIONED_KEY="/etc/wechat-memory-license/signing-ed25519-${KEY_ID}.pem"
LEGACY_KEY=/etc/wechat-memory-license/signing-ed25519.pem
if [[ ! -f "${VERSIONED_KEY}" && ! -f "${LEGACY_KEY}" && ! -f "${SIGNING_KEY}" ]]; then
  echo "找不到 key-id=${KEY_ID} 的私钥；首次部署默认从 /root/wkm-signing-ed25519.pem 读取。" >&2
  exit 3
fi

dnf install -y nginx firewalld policycoreutils-python-utils sqlite curl openssl \
  python3.11 python3.11-pip
systemctl enable --now firewalld

install -d -o nginx -g nginx -m 0755 /var/www/wechat-memory-license-acme
install -d -o root -g root -m 0755 /etc/wechat-memory-license

firewall-cmd --permanent --add-service=http >/dev/null
firewall-cmd --permanent --add-port="${PUBLIC_PORT}/tcp" >/dev/null
firewall-cmd --reload >/dev/null
semanage port -a -t http_port_t -p tcp "${PUBLIC_PORT}" 2>/dev/null || \
  semanage port -m -t http_port_t -p tcp "${PUBLIC_PORT}"
setsebool -P httpd_can_network_connect 1
restorecon -RF /var/www/wechat-memory-license-acme

BOOTSTRAP=/etc/nginx/conf.d/wechat-memory-license.conf
sed "s/104\.224\.90\.172/${PUBLIC_IP}/g" \
  "${SOURCE_DIR}/deploy/el9/nginx-bootstrap.conf" >"${BOOTSTRAP}.tmp"
install -o root -g root -m 0644 "${BOOTSTRAP}.tmp" "${BOOTSTRAP}"
rm -f -- "${BOOTSTRAP}.tmp"
nginx -t
systemctl enable --now nginx

TLS_CERT=""
TLS_KEY=""
if [[ "${TLS_MODE}" == "letsencrypt" ]]; then
  if [[ ! -x /opt/certbot/bin/certbot ]]; then
    python3.11 -m venv /opt/certbot
  fi
  /opt/certbot/bin/python -m pip install --upgrade pip
  /opt/certbot/bin/pip install --upgrade "certbot==${CERTBOT_VERSION}"
  TLS_CERT="/etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem"
  TLS_KEY="/etc/letsencrypt/live/${PUBLIC_IP}/privkey.pem"
  if [[ ! -s "${TLS_CERT}" || ! -s "${TLS_KEY}" ]]; then
    if [[ -z "${EMAIL}" ]]; then
      echo "首次申请 Let's Encrypt IP 证书必须提供 --email。" >&2
      exit 3
    fi
    /opt/certbot/bin/certbot certonly --non-interactive --agree-tos \
      --email "${EMAIL}" --preferred-profile shortlived \
      --webroot --webroot-path /var/www/wechat-memory-license-acme \
      --ip-address "${PUBLIC_IP}" --cert-name "${PUBLIC_IP}"
  fi
  install -o root -g root -m 0755 \
    "${SOURCE_DIR}/deploy/el9/certbot-deploy-hook.sh" \
    /usr/local/sbin/wkm-certbot-deploy
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/deploy/el9/certbot-renew.service" \
    /etc/systemd/system/certbot-renew.service
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/deploy/el9/certbot-renew.timer" \
    /etc/systemd/system/certbot-renew.timer
else
  if [[ ! -f "${EXISTING_CERT}" || ! -f "${EXISTING_KEY}" ]]; then
    echo "existing 模式必须提供 --tls-cert 和 --tls-key。" >&2
    exit 3
  fi
  install -d -o root -g nginx -m 0750 /etc/nginx/wechat-memory-license-tls
  install -o root -g nginx -m 0640 "${EXISTING_CERT}" \
    /etc/nginx/wechat-memory-license-tls/fullchain.pem
  install -o root -g nginx -m 0640 "${EXISTING_KEY}" \
    /etc/nginx/wechat-memory-license-tls/privkey.pem
  TLS_CERT=/etc/nginx/wechat-memory-license-tls/fullchain.pem
  TLS_KEY=/etc/nginx/wechat-memory-license-tls/privkey.pem
fi

openssl x509 -in "${TLS_CERT}" -noout -checkend 172800 >/dev/null || {
  echo "TLS 证书将在 48 小时内过期，拒绝上线。" >&2
  exit 4
}
openssl x509 -in "${TLS_CERT}" -noout -text | grep -F "IP Address:${PUBLIC_IP}" >/dev/null || {
  echo "TLS 证书不包含目标 IP SAN。" >&2
  exit 4
}

export WKM_PUBLIC_BASE_URL="https://${PUBLIC_IP}:${PUBLIC_PORT}"
bash "${SOURCE_DIR}/deploy/el9/install-release.sh" \
  "${SOURCE_DIR}" "${SIGNING_KEY}" "${KEY_ID}"

PRODUCTION_TMP=/etc/nginx/conf.d/.wechat-memory-license.production.tmp
sed -e "s#ssl_certificate     /etc/letsencrypt/live/104.224.90.172/fullchain.pem;#ssl_certificate     ${TLS_CERT};#" \
    -e "s#ssl_certificate_key /etc/letsencrypt/live/104.224.90.172/privkey.pem;#ssl_certificate_key ${TLS_KEY};#" \
    -e "s/104\.224\.90\.172/${PUBLIC_IP}/g" \
    -e "s/listen 9000 ssl;/listen ${PUBLIC_PORT} ssl;/g" \
    -e "s/listen \[::\]:9000 ssl;/listen [::]:${PUBLIC_PORT} ssl;/g" \
    -e "s/X-Forwarded-Port 9000;/X-Forwarded-Port ${PUBLIC_PORT};/g" \
    "${SOURCE_DIR}/deploy/el9/nginx-production.conf" >"${PRODUCTION_TMP}"
install -o root -g root -m 0644 "${PRODUCTION_TMP}" "${BOOTSTRAP}"
rm -f -- "${PRODUCTION_TMP}"
nginx -t
systemctl reload nginx
systemctl daemon-reload
if [[ "${TLS_MODE}" == "letsencrypt" ]]; then
  systemctl enable --now certbot-renew.timer
fi

curl --fail --silent --show-error --connect-timeout 3 --max-time 15 \
  http://127.0.0.1:9100/readyz >/dev/null
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
  "https://${PUBLIC_IP}:${PUBLIC_PORT}/readyz" >/dev/null
PUBLIC_KEY_JSON="$(curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
  "https://${PUBLIC_IP}:${PUBLIC_PORT}/v1/keys")"

echo
echo "部署完成：https://${PUBLIC_IP}:${PUBLIC_PORT}"
echo "授权服务：$(systemctl is-active wechat-memory-license)"
echo "数据库备份定时器：$(systemctl is-enabled wechat-memory-license-backup.timer)"
if [[ "${TLS_MODE}" == "letsencrypt" ]]; then
  echo "证书续期定时器：$(systemctl is-enabled certbot-renew.timer)"
fi
echo "签名公钥响应：${PUBLIC_KEY_JSON}"
echo "新增用户：sudo wkm-license-admin add --username <用户名> --plan free"
