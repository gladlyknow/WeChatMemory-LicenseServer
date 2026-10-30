#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "请以 root 执行。" >&2
  exit 2
fi

SOURCE_DIR="${1:-}"
STAGED_KEY="${2:-}"
KEY_ID="${3:-2026-10-30}"
PUBLIC_BASE_URL="${WKM_PUBLIC_BASE_URL:-https://104.224.90.172:9000}"
if [[ -z "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/pyproject.toml" ]]; then
  echo "用法：$0 <已上传的源码目录> [首次部署的 Ed25519 PEM] [key-id]" >&2
  exit 2
fi
if [[ ! "${KEY_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "key-id 不合法。" >&2
  exit 2
fi

APP_ROOT=/opt/wechat-memory-license
STATE_ROOT=/var/lib/wechat-memory-license
CONFIG_ROOT=/etc/wechat-memory-license
LOG_ROOT=/var/log/wechat-memory-license
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RELEASE_DIR="${APP_ROOT}/releases/${RELEASE_ID}"
CURRENT_LINK="${APP_ROOT}/current"
PREVIOUS_TARGET=""
DATABASE_PATH="${STATE_ROOT}/license.sqlite3"

if ! id wkm-license >/dev/null 2>&1; then
  useradd --system --home-dir "${STATE_ROOT}" --shell /sbin/nologin wkm-license
fi
install -d -o root -g root -m 0755 "${APP_ROOT}" "${APP_ROOT}/releases"
install -d -o root -g wkm-license -m 0750 "${CONFIG_ROOT}"
install -d -o wkm-license -g wkm-license -m 0700 "${STATE_ROOT}" "${STATE_ROOT}/backups"
install -d -o wkm-license -g wkm-license -m 0750 "${LOG_ROOT}"
install -d -o root -g root -m 0755 "${RELEASE_DIR}"

GIT_TOPLEVEL="$(git -C "${SOURCE_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ "${GIT_TOPLEVEL}" == "${SOURCE_DIR}" ]]; then
  # A clean Git object database is a stronger release boundary than copying a
  # worktree: ignored local secrets and databases never enter the stream.
  git -C "${SOURCE_DIR}" archive --format=tar HEAD | \
    tar -C "${RELEASE_DIR}" -xf -
else
  # Uploaded source bundles may intentionally omit .git.  Keep this fallback
  # fail-safe by excluding both common build state and every private-key form.
  tar -C "${SOURCE_DIR}" \
    --exclude='./.git' \
    --exclude='./.venv' \
    --exclude='./.secrets' \
    --exclude='./.pytest_cache' \
    --exclude='./.mypy_cache' \
    --exclude='./.env' \
    --exclude='./data' \
    --exclude='./backups' \
    --exclude='./certs' \
    --exclude='./build' \
    --exclude='./dist' \
    --exclude='__pycache__' \
    --exclude='*.py[co]' \
    --exclude='*.sqlite3*' \
    --exclude='*.db' \
    --exclude='*.log' \
    --exclude='*.pem' \
    --exclude='*.key' \
    --exclude='*.p8' \
    --exclude='*.p12' \
    --exclude='*.pfx' \
    -cf - . | tar -C "${RELEASE_DIR}" -xf -
fi
find "${RELEASE_DIR}" -type d -exec chmod 0755 {} +
find "${RELEASE_DIR}" -type f -exec chmod 0644 {} +

REQUIRED_RELEASE_DOCUMENTS=(
  README.md
  docs/README.md
  docs/DOCUMENTATION_POLICY.md
  docs/SERVER_OPERATIONS_RUNBOOK.md
  docs/LICENSE_SERVER_ARCHITECTURE.md
  docs/ONE_CLICK_DEPLOY_AND_ADMIN.md
  docs/MANUAL_DEPLOY_EL9_IP_CERT.md
  docs/SIGNING_KEY_OPERATIONS.md
  docs/DEPLOYMENT_104.224.90.172.md
  scripts/check_documentation.py
)
for required_document in "${REQUIRED_RELEASE_DOCUMENTS[@]}"; do
  if [[ ! -f "${RELEASE_DIR}/${required_document}" ]]; then
    echo "发布物缺少必需文档或校验器：${required_document}" >&2
    exit 3
  fi
done
python3.11 "${RELEASE_DIR}/scripts/check_documentation.py"

if [[ -f "${DATABASE_PATH}" ]]; then
  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "升级前备份需要 sqlite3 命令。" >&2
    exit 3
  fi
  BACKUP_PATH="${STATE_ROOT}/backups/license-${RELEASE_ID}.sqlite3"
  /usr/sbin/runuser -u wkm-license -- /usr/bin/sqlite3 \
    "${DATABASE_PATH}" ".backup '${BACKUP_PATH}'"
  if [[ "$(/usr/sbin/runuser -u wkm-license -- /usr/bin/sqlite3 \
    "${BACKUP_PATH}" 'PRAGMA integrity_check;')" != "ok" ]]; then
    echo "升级前 SQLite 备份完整性检查失败。" >&2
    exit 3
  fi
  chmod 0600 "${BACKUP_PATH}"
fi

if [[ ! -x "${APP_ROOT}/venv/bin/python" ]]; then
  python3.11 -m venv "${APP_ROOT}/venv"
fi
"${APP_ROOT}/venv/bin/pip" install --upgrade pip
"${APP_ROOT}/venv/bin/pip" install --no-cache-dir "${RELEASE_DIR}"

MANIFEST_PATH="${RELEASE_DIR}/config/signing-public-keys.json"
KEY_PATH="${CONFIG_ROOT}/signing-ed25519-${KEY_ID}.pem"
LEGACY_KEY_PATH="${CONFIG_ROOT}/signing-ed25519.pem"
KEY_SOURCE=""
if [[ -f "${KEY_PATH}" ]]; then
  KEY_SOURCE="${KEY_PATH}"
  if [[ -n "${STAGED_KEY}" && -f "${STAGED_KEY}" ]] && \
      ! cmp -s -- "${STAGED_KEY}" "${KEY_PATH}"; then
    echo "同一 key-id 已安装不同私钥，拒绝覆盖。" >&2
    exit 3
  fi
elif [[ -n "${STAGED_KEY}" && -f "${STAGED_KEY}" ]]; then
  KEY_SOURCE="${STAGED_KEY}"
elif [[ -f "${LEGACY_KEY_PATH}" ]]; then
  KEY_SOURCE="${LEGACY_KEY_PATH}"
else
  echo "找不到 key-id=${KEY_ID} 的 Ed25519 私钥。" >&2
  exit 3
fi

"${APP_ROOT}/venv/bin/python" -m license_server signing-key verify \
  --path "${KEY_SOURCE}" --key-id "${KEY_ID}" --manifest "${MANIFEST_PATH}" >/dev/null
if [[ ! -f "${KEY_PATH}" ]]; then
  install -o root -g wkm-license -m 0640 "${KEY_SOURCE}" "${KEY_PATH}"
fi

ENV_PATH="${CONFIG_ROOT}/service.env"
ENV_ROLLBACK="${CONFIG_ROOT}/.service.env.rollback.${RELEASE_ID}"
ENV_HAD_EXISTING=0
if [[ -f "${ENV_PATH}" ]]; then
  install -o root -g wkm-license -m 0640 "${ENV_PATH}" "${ENV_ROLLBACK}"
  ENV_HAD_EXISTING=1
fi
env_value() {
  local name="$1"
  local fallback="$2"
  local value=""
  if [[ -f "${ENV_PATH}" ]]; then
    value="$(sed -n "s/^${name}=//p" "${ENV_PATH}" | tail -n 1)"
  fi
  printf '%s' "${value:-${fallback}}"
}
ENV_TMP="${CONFIG_ROOT}/.service.env.${RELEASE_ID}.tmp"
cat >"${ENV_TMP}" <<EOF
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
APP_BIND=$(env_value APP_BIND 127.0.0.1)
APP_PORT=$(env_value APP_PORT 9100)
DATABASE_PATH=${STATE_ROOT}/license.sqlite3
LICENSE_ISSUER=$(env_value LICENSE_ISSUER wechat-memory-license)
LICENSE_AUDIENCE=$(env_value LICENSE_AUDIENCE wechat-memory-client)
REFRESH_TOKEN_DAYS=$(env_value REFRESH_TOKEN_DAYS 365)
LICENSE_TICKET_HOURS=$(env_value LICENSE_TICKET_HOURS 24)
RATE_LIMIT_PER_MINUTE=$(env_value RATE_LIMIT_PER_MINUTE 20)
LOG_LEVEL=$(env_value LOG_LEVEL INFO)
SIGNING_KEY_ID=${KEY_ID}
SIGNING_PRIVATE_KEY_PATH=${KEY_PATH}
SIGNING_PUBLIC_KEYS_PATH=${CURRENT_LINK}/config/signing-public-keys.json
EOF
install -o root -g wkm-license -m 0640 "${ENV_TMP}" "${ENV_PATH}"
rm -f -- "${ENV_TMP}"

/usr/sbin/runuser -u wkm-license -- "${APP_ROOT}/venv/bin/python" \
  "${RELEASE_DIR}/register_user.py" init --db "${DATABASE_PATH}"

install -o root -g root -m 0644 \
  "${RELEASE_DIR}/deploy/el9/wechat-memory-license.service.example" \
  /etc/systemd/system/wechat-memory-license.service
install -o root -g root -m 0755 \
  "${RELEASE_DIR}/deploy/el9/wkm-license-admin" \
  /usr/local/sbin/wkm-license-admin
install -o root -g root -m 0755 \
  "${RELEASE_DIR}/deploy/el9/backup-database.sh" \
  /usr/local/sbin/wkm-license-backup
install -o root -g root -m 0644 \
  "${RELEASE_DIR}/deploy/el9/wechat-memory-license-backup.service" \
  /etc/systemd/system/wechat-memory-license-backup.service
install -o root -g root -m 0644 \
  "${RELEASE_DIR}/deploy/el9/wechat-memory-license-backup.timer" \
  /etc/systemd/system/wechat-memory-license-backup.timer

if [[ -L "${CURRENT_LINK}" ]]; then
  PREVIOUS_TARGET="$(readlink -f "${CURRENT_LINK}")"
fi
ln -sfn "${RELEASE_DIR}" "${CURRENT_LINK}.next"
mv -Tf "${CURRENT_LINK}.next" "${CURRENT_LINK}"
systemctl daemon-reload
rollback_runtime() {
  if [[ -n "${PREVIOUS_TARGET}" ]]; then
    ln -sfn "${PREVIOUS_TARGET}" "${CURRENT_LINK}.rollback"
    mv -Tf "${CURRENT_LINK}.rollback" "${CURRENT_LINK}"
  else
    rm -f -- "${CURRENT_LINK}"
  fi
  if (( ENV_HAD_EXISTING )); then
    install -o root -g wkm-license -m 0640 "${ENV_ROLLBACK}" "${ENV_PATH}"
  else
    rm -f -- "${ENV_PATH}"
  fi
  systemctl daemon-reload
  if [[ -n "${PREVIOUS_TARGET}" ]]; then
    systemctl restart wechat-memory-license || true
  else
    systemctl stop wechat-memory-license || true
  fi
  rm -f -- "${ENV_ROLLBACK}"
}
systemctl enable wechat-memory-license
if ! systemctl restart wechat-memory-license; then
  rollback_runtime
  echo "授权服务启动失败，已尝试回滚代码和服务配置。" >&2
  exit 4
fi
systemctl enable --now wechat-memory-license-backup.timer
if ! curl --fail --silent --show-error --connect-timeout 3 --max-time 15 \
    http://127.0.0.1:9100/readyz >/dev/null; then
  rollback_runtime
  echo "健康检查失败，已尝试回滚代码和服务配置。" >&2
  exit 4
fi
rm -f -- "${ENV_ROLLBACK}"

echo "授权 API 已就绪：${RELEASE_DIR}"
curl --silent http://127.0.0.1:9100/v1/keys
echo
