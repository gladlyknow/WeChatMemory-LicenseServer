#!/usr/bin/env bash
set -Eeuo pipefail
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
CERTBOT_ROOT="/opt/wechat-memory-license-certbot"
FIREWALL_ZONE=""

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
  --firewall-zone ZONE     firewalld 活动 zone；多 zone 主机必须显式指定

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
    --firewall-zone) FIREWALL_ZONE="$2"; shift 2 ;;
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
if [[ ! -f "${SOURCE_DIR}/deploy/el9/deployment-transaction.sh" ]]; then
  echo "源码缺少部署事务库。" >&2
  exit 3
fi
# shellcheck source=deployment-transaction.sh
. "${SOURCE_DIR}/deploy/el9/deployment-transaction.sh"
if ! wkm_version_at_least "${CERTBOT_VERSION}" 5 4; then
  echo "--certbot-version 必须不低于 5.4.0。" >&2
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

# An unhandled transaction directory means a previous shell may have been
# killed before its EXIT compensation ran.  Never overwrite that recovery
# evidence or guess which runtime state is authoritative.
if [[ -d /etc/wechat-memory-license ]]; then
  shopt -s nullglob
  STALE_TRANSACTIONS=(
    /etc/wechat-memory-license/.one-click-transaction.*
    /etc/wechat-memory-license/.install-transaction.*
  )
  shopt -u nullglob
  if (( ${#STALE_TRANSACTIONS[@]} )); then
    echo "发现上次未收尾的部署事务，拒绝覆盖恢复证据：${STALE_TRANSACTIONS[*]}" >&2
    echo "请先按运维手册核对 current、service.env、systemd、Nginx 和 SQLite 备份。" >&2
    exit 5
  fi
fi

dnf install -y nginx firewalld policycoreutils-python-utils sqlite curl openssl \
  python3.11 python3.11-pip

if [[ ! -f "${SOURCE_DIR}/scripts/check_documentation.py" ]]; then
  echo "源码缺少文档契约校验器。" >&2
  exit 3
fi
python3.11 "${SOURCE_DIR}/scripts/check_documentation.py"

install -d -o nginx -g nginx -m 0755 /var/www/wechat-memory-license-acme
install -d -o root -g root -m 0755 /etc/wechat-memory-license

BOOTSTRAP=/etc/nginx/conf.d/wechat-memory-license.conf
if [[ ! -f "${SOURCE_DIR}/deploy/el9/shared-host-safety.sh" ]]; then
  echo "源码缺少共享主机安全门禁。" >&2
  exit 3
fi
# shellcheck source=shared-host-safety.sh
. "${SOURCE_DIR}/deploy/el9/shared-host-safety.sh"
wkm_assert_owned_nginx_file "${BOOTSTRAP}"
wkm_assert_no_foreign_nginx_conflict \
  /etc/nginx "${BOOTSTRAP}" "${PUBLIC_IP}" "${PUBLIC_PORT}"
NGINX_FOREIGN_BASELINE="$(wkm_foreign_nginx_digest /etc/nginx "${BOOTSTRAP}")"
NGINX_CONFIG_SNAPSHOT_TAKEN=0
NGINX_CONFIG_CHANGED=0
NGINX_WAS_ACTIVE=0
NGINX_WAS_ENABLED=0
DEPLOY_COMPLETED=0
APPLICATION_TRANSACTION_DIR=""
APPLICATION_TRANSACTION_ARMED=0
APPLICATION_RUNTIME_CHANGED=0
APPLICATION_INSTALL_ATTEMPTED=0
APPLICATION_INSTALLED=0
ROLLBACK_FAILED=0
FIREWALL_RUNTIME_HTTP_ADDED=0
FIREWALL_PERMANENT_HTTP_ADDED=0
FIREWALL_RUNTIME_PORT_ADDED=0
FIREWALL_PERMANENT_PORT_ADDED=0
SELINUX_PORT_ADDED=0
SELINUX_BOOLEAN_CHANGED=0
ACME_CHALLENGE_FILE=""
CERTBOT_SERVICE_TMP=""

if systemctl is-active --quiet nginx; then NGINX_WAS_ACTIVE=1; fi
if systemctl is-enabled --quiet nginx; then NGINX_WAS_ENABLED=1; fi

backup_nginx_config() {
  if (( ! NGINX_CONFIG_SNAPSHOT_TAKEN )); then
    NGINX_CONFIG_SNAPSHOT_TAKEN=1
    wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
      "${BOOTSTRAP}" nginx-config
  fi
}

restore_nginx_runtime() {
  local failed=0
  set +e
  if (( NGINX_CONFIG_CHANGED )); then
    wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
      "${BOOTSTRAP}" nginx-config || failed=1
  fi
  if (( NGINX_WAS_ENABLED )); then
    systemctl enable nginx >/dev/null 2>&1 || failed=1
  else
    systemctl disable nginx >/dev/null 2>&1 || true
  fi
  if (( NGINX_WAS_ACTIVE )); then
    if wkm_assert_foreign_nginx_unchanged \
        "${NGINX_FOREIGN_BASELINE}" /etc/nginx "${BOOTSTRAP}" && nginx -t; then
      systemctl reload nginx || failed=1
    else
      echo "Nginx 部署前配置恢复后未通过校验，无法安全重载。" >&2
      failed=1
    fi
  else
    systemctl stop nginx >/dev/null 2>&1 || true
  fi
  set -e
  return "${failed}"
}

resolve_firewall_zone() {
  local zones=()
  if ! systemctl is-active --quiet firewalld; then
    echo "firewalld 当前未运行；为避免打断同机业务，部署器不会自动启动它。" >&2
    echo "请先确认现有 nftables/firewalld 策略，然后人工启动 firewalld 或改用受控的外部防火墙流程。" >&2
    return 1
  fi
  if [[ -n "${FIREWALL_ZONE}" ]]; then
    firewall-cmd --get-zones | tr ' ' '\n' | grep -Fxq "${FIREWALL_ZONE}" || {
      echo "firewalld zone 不存在：${FIREWALL_ZONE}" >&2
      return 1
    }
    return 0
  fi
  mapfile -t zones < <(firewall-cmd --get-active-zones | awk '/^[^[:space:]]/{print $1}')
  if (( ${#zones[@]} == 1 )); then
    FIREWALL_ZONE="${zones[0]}"
  elif (( ${#zones[@]} == 0 )); then
    echo "firewalld 没有活动 zone，拒绝把规则误加到未生效的默认 zone。" >&2
    return 1
  else
    echo "检测到多个 firewalld 活动 zone：${zones[*]}" >&2
    echo "请确认公网网卡后使用 --firewall-zone ZONE，部署器不会同时改写多个 zone。" >&2
    return 1
  fi
}

configure_firewall() {
  resolve_firewall_zone
  if ! firewall-cmd --zone="${FIREWALL_ZONE}" --query-service=http >/dev/null; then
    firewall-cmd --zone="${FIREWALL_ZONE}" --add-service=http >/dev/null
    FIREWALL_RUNTIME_HTTP_ADDED=1
  fi
  if ! firewall-cmd --permanent --zone="${FIREWALL_ZONE}" --query-service=http >/dev/null; then
    firewall-cmd --permanent --zone="${FIREWALL_ZONE}" --add-service=http >/dev/null
    FIREWALL_PERMANENT_HTTP_ADDED=1
  fi
  if ! firewall-cmd --zone="${FIREWALL_ZONE}" \
      --query-port="${PUBLIC_PORT}/tcp" >/dev/null; then
    firewall-cmd --zone="${FIREWALL_ZONE}" \
      --add-port="${PUBLIC_PORT}/tcp" >/dev/null
    FIREWALL_RUNTIME_PORT_ADDED=1
  fi
  if ! firewall-cmd --permanent --zone="${FIREWALL_ZONE}" \
      --query-port="${PUBLIC_PORT}/tcp" >/dev/null; then
    firewall-cmd --permanent --zone="${FIREWALL_ZONE}" \
      --add-port="${PUBLIC_PORT}/tcp" >/dev/null
    FIREWALL_PERMANENT_PORT_ADDED=1
  fi
}

selinux_http_port_contains() {
  semanage port -l | awk -v wanted="${PUBLIC_PORT}" '
    $1 == "http_port_t" && $2 == "tcp" {
      for (index = 3; index <= NF; index++) {
        value = $index
        gsub(/,/, "", value)
        if (value == wanted) found = 1
        split(value, bounds, "-")
        if (length(bounds) == 2 && wanted >= bounds[1] && wanted <= bounds[2]) found = 1
      }
    }
    END { exit(found ? 0 : 1) }
  '
}

configure_selinux() {
  if ! selinux_http_port_contains; then
    if semanage port -a -t http_port_t -p tcp "${PUBLIC_PORT}"; then
      SELINUX_PORT_ADDED=1
    else
      echo "${PUBLIC_PORT}/tcp 可能已属于其他 SELinux 类型；为避免破坏同机业务，拒绝改写既有端口类型。" >&2
      return 1
    fi
  fi
  if ! getsebool httpd_can_network_connect | grep -Fq -- '--> on'; then
    setsebool -P httpd_can_network_connect 1
    SELINUX_BOOLEAN_CHANGED=1
  fi
  restorecon -RF /var/www/wechat-memory-license-acme
}

restore_host_prerequisites() {
  local failed=0
  set +e
  if (( FIREWALL_RUNTIME_HTTP_ADDED )); then
    firewall-cmd --zone="${FIREWALL_ZONE}" --remove-service=http >/dev/null || failed=1
  fi
  if (( FIREWALL_PERMANENT_HTTP_ADDED )); then
    firewall-cmd --permanent --zone="${FIREWALL_ZONE}" --remove-service=http >/dev/null || failed=1
  fi
  if (( FIREWALL_RUNTIME_PORT_ADDED )); then
    firewall-cmd --zone="${FIREWALL_ZONE}" \
      --remove-port="${PUBLIC_PORT}/tcp" >/dev/null || failed=1
  fi
  if (( FIREWALL_PERMANENT_PORT_ADDED )); then
    firewall-cmd --permanent --zone="${FIREWALL_ZONE}" \
      --remove-port="${PUBLIC_PORT}/tcp" >/dev/null || failed=1
  fi
  if (( SELINUX_PORT_ADDED )); then
    semanage port -d -p tcp "${PUBLIC_PORT}" || failed=1
  fi
  if (( SELINUX_BOOLEAN_CHANGED )); then
    setsebool -P httpd_can_network_connect 0 || failed=1
  fi
  set -e
  return "${failed}"
}

activate_nginx_configuration() {
  wkm_assert_foreign_nginx_unchanged \
    "${NGINX_FOREIGN_BASELINE}" /etc/nginx "${BOOTSTRAP}"
  nginx -t
  if systemctl is-active --quiet nginx; then
    # enable --now is a no-op for an already-running service and would leave a
    # newly installed ACME virtual host unloaded.  A tested reload is required.
    systemctl reload nginx
  else
    systemctl enable --now nginx
  fi
}

verify_local_acme_route() {
  local token="wkm-preflight-${RANDOM}-${RANDOM}"
  local challenge_dir=/var/www/wechat-memory-license-acme/.well-known/acme-challenge
  ACME_CHALLENGE_FILE="${challenge_dir}/${token}"
  install -d -o nginx -g nginx -m 0755 "${challenge_dir}"
  printf '%s\n' "${token}" >"${ACME_CHALLENGE_FILE}"
  chown nginx:nginx "${ACME_CHALLENGE_FILE}"
  chmod 0644 "${ACME_CHALLENGE_FILE}"
  if ! curl --fail --silent --show-error --connect-timeout 2 --max-time 5 \
      -H "Host: ${PUBLIC_IP}" \
      "http://127.0.0.1/.well-known/acme-challenge/${token}" | grep -Fxq "${token}"; then
    rm -f -- "${ACME_CHALLENGE_FILE}"
    ACME_CHALLENGE_FILE=""
    echo "Nginx 未在本机实际提供 WeChatMemory ACME challenge，拒绝调用 Certbot。" >&2
    return 1
  fi
  rm -f -- "${ACME_CHALLENGE_FILE}"
  ACME_CHALLENGE_FILE=""
}

capture_application_runtime() {
  local current=/opt/wechat-memory-license/current
  local environment=/etc/wechat-memory-license/service.env
  APPLICATION_TRANSACTION_DIR="$(mktemp -d /etc/wechat-memory-license/.one-click-transaction.XXXXXX)"
  wkm_tx_capture_link "${APPLICATION_TRANSACTION_DIR}" "${current}" current
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" "${environment}" environment
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-license.service service-unit
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
    /usr/local/sbin/wkm-license-admin admin-command
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
    /usr/local/sbin/wkm-license-backup backup-command
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-license-backup.service backup-service
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-license-backup.timer backup-timer
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
    /usr/local/sbin/wkm-certbot-deploy certbot-hook
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-certbot-renew.service certbot-service
  wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-certbot-renew.timer certbot-timer
  if [[ "${TLS_MODE}" == "existing" ]]; then
    wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
      /etc/nginx/wechat-memory-license-tls/fullchain.pem existing-tls-cert
    wkm_tx_capture_file "${APPLICATION_TRANSACTION_DIR}" \
      /etc/nginx/wechat-memory-license-tls/privkey.pem existing-tls-key
  fi

  APP_SERVICE_WAS_ACTIVE=0
  APP_SERVICE_WAS_ENABLED=0
  BACKUP_TIMER_WAS_ACTIVE=0
  BACKUP_TIMER_WAS_ENABLED=0
  CERTBOT_TIMER_WAS_ACTIVE=0
  CERTBOT_TIMER_WAS_ENABLED=0
  if systemctl is-active --quiet wechat-memory-license; then APP_SERVICE_WAS_ACTIVE=1; fi
  if systemctl is-enabled --quiet wechat-memory-license; then APP_SERVICE_WAS_ENABLED=1; fi
  if systemctl is-active --quiet wechat-memory-license-backup.timer; then
    BACKUP_TIMER_WAS_ACTIVE=1
  fi
  if systemctl is-enabled --quiet wechat-memory-license-backup.timer; then
    BACKUP_TIMER_WAS_ENABLED=1
  fi
  if systemctl is-active --quiet wechat-memory-certbot-renew.timer; then
    CERTBOT_TIMER_WAS_ACTIVE=1
  fi
  if systemctl is-enabled --quiet wechat-memory-certbot-renew.timer; then
    CERTBOT_TIMER_WAS_ENABLED=1
  fi
  APPLICATION_TRANSACTION_ARMED=1
}

restore_one_click_unit_state() {
  local unit="$1"
  local was_enabled="$2"
  local was_active="$3"
  local failed=0
  if (( was_enabled )); then
    systemctl enable "${unit}" >/dev/null 2>&1 || failed=1
  else
    systemctl disable "${unit}" >/dev/null 2>&1 || true
  fi
  if (( was_active )); then
    systemctl restart "${unit}" || failed=1
  else
    systemctl stop "${unit}" >/dev/null 2>&1 || true
  fi
  return "${failed}"
}

restore_application_runtime() {
  local failed=0
  if (( ! APPLICATION_TRANSACTION_ARMED || ! APPLICATION_RUNTIME_CHANGED )); then
    return 0
  fi
  echo "正在恢复部署前的应用、existing TLS、systemd 单元和服务状态。" >&2
  set +e
  wkm_tx_restore_link "${APPLICATION_TRANSACTION_DIR}" \
    /opt/wechat-memory-license/current current || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/wechat-memory-license/service.env environment || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-license.service service-unit || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /usr/local/sbin/wkm-license-admin admin-command || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /usr/local/sbin/wkm-license-backup backup-command || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-license-backup.service backup-service || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-license-backup.timer backup-timer || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /usr/local/sbin/wkm-certbot-deploy certbot-hook || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-certbot-renew.service certbot-service || failed=1
  wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
    /etc/systemd/system/wechat-memory-certbot-renew.timer certbot-timer || failed=1
  if [[ "${TLS_MODE}" == "existing" ]]; then
    wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
      /etc/nginx/wechat-memory-license-tls/fullchain.pem existing-tls-cert || failed=1
    wkm_tx_restore_file "${APPLICATION_TRANSACTION_DIR}" \
      /etc/nginx/wechat-memory-license-tls/privkey.pem existing-tls-key || failed=1
  fi
  systemctl daemon-reload || failed=1
  restore_one_click_unit_state wechat-memory-license \
    "${APP_SERVICE_WAS_ENABLED}" "${APP_SERVICE_WAS_ACTIVE}" || failed=1
  restore_one_click_unit_state wechat-memory-license-backup.timer \
    "${BACKUP_TIMER_WAS_ENABLED}" "${BACKUP_TIMER_WAS_ACTIVE}" || failed=1
  restore_one_click_unit_state wechat-memory-certbot-renew.timer \
    "${CERTBOT_TIMER_WAS_ENABLED}" "${CERTBOT_TIMER_WAS_ACTIVE}" || failed=1
  set -e
  return "${failed}"
}

on_deploy_exit() {
  local status=$?
  trap - EXIT ERR
  if [[ -n "${ACME_CHALLENGE_FILE}" ]]; then
    rm -f -- "${ACME_CHALLENGE_FILE}" || true
  fi
  if [[ -n "${CERTBOT_SERVICE_TMP}" ]]; then
    rm -f -- "${CERTBOT_SERVICE_TMP}" || true
  fi
  if (( ! DEPLOY_COMPLETED )); then
    if ! restore_application_runtime; then
      ROLLBACK_FAILED=1
    fi
    if ! restore_nginx_runtime; then
      ROLLBACK_FAILED=1
    fi
    if ! restore_host_prerequisites; then
      ROLLBACK_FAILED=1
    fi
    if (( APPLICATION_INSTALLED )); then
      echo "警告：候选版本已执行 SQLite init/migration。应用与入口已补偿回滚，数据库不会在可能有新写入后自动降级；请核对升级前备份和 schema 兼容性。" >&2
    elif (( APPLICATION_INSTALL_ATTEMPTED )); then
      echo "警告：底层发布器未完成，可能已进入 SQLite init/migration；请查看底层回滚日志和升级前备份，不要假定数据库已自动降级。" >&2
    fi
    if (( ! ROLLBACK_FAILED )) && [[ -n "${APPLICATION_TRANSACTION_DIR}" ]]; then
      rm -rf -- "${APPLICATION_TRANSACTION_DIR}"
    fi
    if (( ROLLBACK_FAILED )); then
      echo "部署补偿回滚未完整成功，事务快照保留在 ${APPLICATION_TRANSACTION_DIR}。" >&2
      echo "请立即按运维手册人工恢复；下次部署会失败关闭。" >&2
      if (( status == 0 )); then status=5; fi
    else
      echo "部署失败，应用和 Nginx 运行态已恢复到部署前快照。" >&2
    fi
  fi
  exit "${status}"
}
trap on_deploy_exit EXIT
capture_application_runtime
configure_firewall
configure_selinux
ACME_ROUTE_VERIFIED=0

if [[ -f "${BOOTSTRAP}" ]] && nginx -t >/dev/null 2>&1; then
  echo "已存在通过校验的 Nginx 配置，升级期间保持当前 9000 入口。"
  if ! grep -Eq "server_name[[:space:]]+${PUBLIC_IP//./\\.}[[:space:]]*;" "${BOOTSTRAP}" || \
     ! grep -Fq '/var/www/wechat-memory-license-acme' "${BOOTSTRAP}"; then
    echo "已有 WeChatMemory Nginx 配置与当前 --public-ip/ACME webroot 不一致，拒绝猜测覆盖。" >&2
    exit 4
  fi
  if systemctl is-active --quiet nginx && verify_local_acme_route; then
    ACME_ROUTE_VERIFIED=1
    echo "当前 Nginx 运行态已提供专属 ACME 路由，不执行无需的共享服务 reload。"
  else
    activate_nginx_configuration
  fi
else
  backup_nginx_config
  sed "s/104\.224\.90\.172/${PUBLIC_IP}/g" \
    "${SOURCE_DIR}/deploy/el9/nginx-bootstrap.conf" >"${BOOTSTRAP}.tmp"
  NGINX_CONFIG_CHANGED=1
  install -o root -g root -m 0644 "${BOOTSTRAP}.tmp" "${BOOTSTRAP}"
  rm -f -- "${BOOTSTRAP}.tmp"
  activate_nginx_configuration
fi
if (( ! ACME_ROUTE_VERIFIED )); then
  verify_local_acme_route
fi

TLS_CERT=""
TLS_KEY=""
if [[ "${TLS_MODE}" == "letsencrypt" ]]; then
  if [[ ! -x "${CERTBOT_ROOT}/bin/certbot" ]]; then
    python3.11 -m venv "${CERTBOT_ROOT}"
  fi
  "${CERTBOT_ROOT}/bin/python" -m pip install --upgrade pip
  "${CERTBOT_ROOT}/bin/pip" install --upgrade "certbot==${CERTBOT_VERSION}"
  TLS_CERT="/etc/letsencrypt/live/${PUBLIC_IP}/fullchain.pem"
  TLS_KEY="/etc/letsencrypt/live/${PUBLIC_IP}/privkey.pem"
  if [[ ! -s "${TLS_CERT}" || ! -s "${TLS_KEY}" ]]; then
    if [[ -z "${EMAIL}" ]]; then
      echo "首次申请 Let's Encrypt IP 证书必须提供 --email。" >&2
      exit 3
    fi
    "${CERTBOT_ROOT}/bin/certbot" certonly --non-interactive --agree-tos \
      --email "${EMAIL}" --preferred-profile shortlived \
      --webroot --webroot-path /var/www/wechat-memory-license-acme \
      --ip-address "${PUBLIC_IP}" --cert-name "${PUBLIC_IP}"
  fi
  # The ACME account/certificate itself is external state, but the captured
  # local hook and timer units are part of this deployment's runtime state.
  APPLICATION_RUNTIME_CHANGED=1
  install -o root -g root -m 0755 \
    "${SOURCE_DIR}/deploy/el9/certbot-deploy-hook.sh" \
    /usr/local/sbin/wkm-certbot-deploy
  CERTBOT_SERVICE_TMP=/etc/systemd/system/.wechat-memory-certbot-renew.service.tmp
  sed "s/104\\.224\\.90\\.172/${PUBLIC_IP}/g" \
    "${SOURCE_DIR}/deploy/el9/wechat-memory-certbot-renew.service" \
    >"${CERTBOT_SERVICE_TMP}"
  install -o root -g root -m 0644 \
    "${CERTBOT_SERVICE_TMP}" \
    /etc/systemd/system/wechat-memory-certbot-renew.service
  rm -f -- "${CERTBOT_SERVICE_TMP}"
  CERTBOT_SERVICE_TMP=""
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/deploy/el9/wechat-memory-certbot-renew.timer" \
    /etc/systemd/system/wechat-memory-certbot-renew.timer
else
  if [[ ! -f "${EXISTING_CERT}" || ! -f "${EXISTING_KEY}" ]]; then
    echo "existing 模式必须提供 --tls-cert 和 --tls-key。" >&2
    exit 3
  fi
  # Arm compensation before the first write.  If certificate validation or
  # any later acceptance check fails, old Nginx configuration must continue
  # to see the exact certificate and key it used before this deployment.
  APPLICATION_RUNTIME_CHANGED=1
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

APPLICATION_RUNTIME_CHANGED=1
APPLICATION_INSTALL_ATTEMPTED=1
bash "${SOURCE_DIR}/deploy/el9/install-release.sh" \
  "${SOURCE_DIR}" "${SIGNING_KEY}" "${KEY_ID}"
APPLICATION_INSTALLED=1
wkm_deploy_checkpoint after_application_install

PRODUCTION_TMP=/etc/nginx/conf.d/.wechat-memory-license.production.tmp
backup_nginx_config
sed -e "s#ssl_certificate     /etc/letsencrypt/live/104.224.90.172/fullchain.pem;#ssl_certificate     ${TLS_CERT};#" \
    -e "s#ssl_certificate_key /etc/letsencrypt/live/104.224.90.172/privkey.pem;#ssl_certificate_key ${TLS_KEY};#" \
    -e "s/104\.224\.90\.172/${PUBLIC_IP}/g" \
    -e "s/listen 9000 ssl;/listen ${PUBLIC_PORT} ssl;/g" \
    -e "s/listen \[::\]:9000 ssl;/listen [::]:${PUBLIC_PORT} ssl;/g" \
    -e "s/X-Forwarded-Port 9000;/X-Forwarded-Port ${PUBLIC_PORT};/g" \
    "${SOURCE_DIR}/deploy/el9/nginx-production.conf" >"${PRODUCTION_TMP}"
NGINX_CONFIG_CHANGED=1
install -o root -g root -m 0644 "${PRODUCTION_TMP}" "${BOOTSTRAP}"
rm -f -- "${PRODUCTION_TMP}"
activate_nginx_configuration
wkm_deploy_checkpoint after_nginx_switch
systemctl daemon-reload
if [[ "${TLS_MODE}" == "letsencrypt" ]]; then
  systemctl enable --now wechat-memory-certbot-renew.timer
fi

curl --fail --silent --show-error --connect-timeout 3 --max-time 15 \
  http://127.0.0.1:9100/readyz/deep >/dev/null
curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
  "https://${PUBLIC_IP}:${PUBLIC_PORT}/readyz" >/dev/null
PUBLIC_KEY_JSON="$(curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
  "https://${PUBLIC_IP}:${PUBLIC_PORT}/v1/keys")"

DEPLOY_COMPLETED=1
trap - EXIT ERR
if [[ -n "${APPLICATION_TRANSACTION_DIR}" ]]; then
  rm -rf -- "${APPLICATION_TRANSACTION_DIR}"
fi

echo
echo "部署完成：https://${PUBLIC_IP}:${PUBLIC_PORT}"
echo "授权服务：$(systemctl is-active wechat-memory-license)"
echo "数据库备份定时器：$(systemctl is-enabled wechat-memory-license-backup.timer)"
if [[ "${TLS_MODE}" == "letsencrypt" ]]; then
  echo "证书续期定时器：$(systemctl is-enabled wechat-memory-certbot-renew.timer)"
fi
echo "签名公钥响应：${PUBLIC_KEY_JSON}"
echo "新增用户：sudo wkm-license-admin add --username <用户名> --plan free"
