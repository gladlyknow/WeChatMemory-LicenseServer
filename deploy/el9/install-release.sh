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

APP_ROOT=/opt/wechat-memory-license
STATE_ROOT=/var/lib/wechat-memory-license
CONFIG_ROOT=/etc/wechat-memory-license
LOG_ROOT=/var/log/wechat-memory-license
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)"
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

cp -a "${SOURCE_DIR}/." "${RELEASE_DIR}/"
rm -rf "${RELEASE_DIR}/.git" "${RELEASE_DIR}/.venv" "${RELEASE_DIR}/.secrets" \
  "${RELEASE_DIR}/__pycache__" "${RELEASE_DIR}/tests/__pycache__"
find "${RELEASE_DIR}" -type d -exec chmod 0755 {} +
find "${RELEASE_DIR}" -type f -exec chmod 0644 {} +

if [[ ! -x "${APP_ROOT}/venv/bin/python" ]]; then
  python3.11 -m venv "${APP_ROOT}/venv"
fi
"${APP_ROOT}/venv/bin/pip" install --upgrade pip
"${APP_ROOT}/venv/bin/pip" install --no-cache-dir "${RELEASE_DIR}"

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

KEY_PATH="${CONFIG_ROOT}/signing-ed25519.pem"
if [[ ! -f "${KEY_PATH}" ]]; then
  if [[ -z "${STAGED_KEY}" || ! -f "${STAGED_KEY}" ]]; then
    echo "首次部署必须提供与客户端内置公钥配对的私钥 PEM。" >&2
    exit 3
  fi
  install -o root -g wkm-license -m 0640 "${STAGED_KEY}" "${KEY_PATH}"
fi

ENV_PATH="${CONFIG_ROOT}/service.env"
if [[ ! -f "${ENV_PATH}" ]]; then
  cat >"${ENV_PATH}" <<EOF
PUBLIC_BASE_URL=${PUBLIC_BASE_URL}
APP_BIND=127.0.0.1
APP_PORT=9100
DATABASE_PATH=${STATE_ROOT}/license.sqlite3
LICENSE_ISSUER=wechat-memory-license
LICENSE_AUDIENCE=wechat-memory-client
REFRESH_TOKEN_DAYS=365
LICENSE_TICKET_HOURS=24
RATE_LIMIT_PER_MINUTE=20
SIGNING_KEY_ID=${KEY_ID}
SIGNING_PRIVATE_KEY_PATH=${KEY_PATH}
EOF
  chown root:wkm-license "${ENV_PATH}"
  chmod 0640 "${ENV_PATH}"
fi

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
systemctl enable --now wechat-memory-license
systemctl enable --now wechat-memory-license-backup.timer
if ! curl --fail --silent --show-error http://127.0.0.1:9100/readyz >/dev/null; then
  if [[ -n "${PREVIOUS_TARGET}" ]]; then
    ln -sfn "${PREVIOUS_TARGET}" "${CURRENT_LINK}.rollback"
    mv -Tf "${CURRENT_LINK}.rollback" "${CURRENT_LINK}"
    systemctl restart wechat-memory-license
  fi
  echo "健康检查失败，已尝试回滚。" >&2
  exit 4
fi

echo "授权 API 已就绪：${RELEASE_DIR}"
curl --silent http://127.0.0.1:9100/v1/keys
echo
