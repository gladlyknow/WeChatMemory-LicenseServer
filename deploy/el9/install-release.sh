#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
  echo "请以 root 执行。" >&2
  exit 2
fi

SOURCE_DIR="${1:-}"
STAGED_KEY="${2:-}"
KEY_ID="${3:-2026-10-30}"
if [[ -z "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/pyproject.toml" ]]; then
  echo "用法：$0 <已上传的源码目录> [首次部署的 Ed25519 PEM] [key-id]" >&2
  exit 2
fi
if [[ ! "${KEY_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "key-id 不合法。" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "${SCRIPT_DIR}/deployment-transaction.sh" ]]; then
  echo "缺少部署事务库：${SCRIPT_DIR}/deployment-transaction.sh" >&2
  exit 3
fi
# shellcheck source=deployment-transaction.sh
. "${SCRIPT_DIR}/deployment-transaction.sh"
if [[ ! -f "${SCRIPT_DIR}/signing-key-safety.sh" ]]; then
  echo "缺少签名私钥文件安全门禁：${SCRIPT_DIR}/signing-key-safety.sh" >&2
  exit 3
fi
# shellcheck source=signing-key-safety.sh
. "${SCRIPT_DIR}/signing-key-safety.sh"
if [[ -n "${STAGED_KEY}" ]] && \
   { [[ -e "${STAGED_KEY}" ]] || [[ -L "${STAGED_KEY}" ]]; }; then
  # Validate the first-use source before creating users, directories, release
  # candidates or backups.  The helper reads metadata only, never PEM bytes.
  wkm_assert_staged_signing_key "${STAGED_KEY}"
fi

APP_ROOT=/opt/wechat-memory-license
STATE_ROOT=/var/lib/wechat-memory-license
CONFIG_ROOT=/etc/wechat-memory-license
LOG_ROOT=/var/log/wechat-memory-license
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RELEASE_DIR="${APP_ROOT}/releases/${RELEASE_ID}"
RELEASE_VENV="${RELEASE_DIR}/.venv"
RELEASE_TEST_VENV="${RELEASE_DIR}/.test-venv"
CURRENT_LINK="${APP_ROOT}/current"
DATABASE_PATH="${STATE_ROOT}/license.sqlite3"
BACKUP_PATH=""

if ! id wkm-license >/dev/null 2>&1; then
  useradd --system --home-dir "${STATE_ROOT}" --shell /sbin/nologin wkm-license
fi
install -d -o root -g root -m 0755 "${APP_ROOT}" "${APP_ROOT}/releases"
install -d -o root -g wkm-license -m 0750 "${CONFIG_ROOT}"
install -d -o wkm-license -g wkm-license -m 0700 "${STATE_ROOT}" "${STATE_ROOT}/backups"
install -d -o wkm-license -g wkm-license -m 0750 "${LOG_ROOT}"

shopt -s nullglob
STALE_INSTALL_TRANSACTIONS=("${CONFIG_ROOT}"/.install-transaction.*)
shopt -u nullglob
if (( ${#STALE_INSTALL_TRANSACTIONS[@]} )); then
  echo "发现上次未收尾的底层发布事务，拒绝继续：${STALE_INSTALL_TRANSACTIONS[*]}" >&2
  echo "请先核对应用运行态和 SQLite 备份，不要直接删除事务目录。" >&2
  exit 5
fi
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
  LICENSE
  THIRD_PARTY_NOTICES.md
  README.md
  docs/README.md
  docs/DOCUMENTATION_POLICY.md
  docs/ACCEPTANCE_CRITERIA.md
  docs/LEGAL_AND_SBOM_RELEASE_GATE.md
  docs/SERVER_OPERATIONS_RUNBOOK.md
  docs/LICENSE_SERVER_ARCHITECTURE.md
  docs/ONE_CLICK_DEPLOY_AND_ADMIN.md
  docs/MANUAL_DEPLOY_EL9_IP_CERT.md
  docs/SIGNING_KEY_OPERATIONS.md
  docs/DEPLOYMENT_104.244.90.172.md
  deploy/el9/deployment-transaction.sh
  deploy/el9/signing-key-safety.sh
  deploy/el9/shared-host-safety.sh
  deploy/el9/test-deployment-transaction.sh
  sbom/server-runtime.spdx.json
  scripts/check_documentation.py
  scripts/generate_sbom.py
  requirements.lock
)
for required_document in "${REQUIRED_RELEASE_DOCUMENTS[@]}"; do
  if [[ ! -f "${RELEASE_DIR}/${required_document}" ]]; then
    echo "发布物缺少必需文档或校验器：${required_document}" >&2
    exit 3
  fi
done

# A Git archive is the preferred boundary, but a tracked private key would
# still be copied by Git itself. Reject standard PEM/OpenSSH private-key
# markers without printing key material. Public keys are intentionally not
# matched.
if [[ -e "${RELEASE_DIR}/.secrets" ]]; then
  echo "发布物含 .secrets 目录，未读取其内容并拒绝部署。" >&2
  exit 3
fi
PRIVATE_KEY_HITS="$(LC_ALL=C grep -RIlE \
  -e '-----BEGIN (RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----' \
  --exclude-dir=.git --exclude-dir=.secrets --exclude-dir=.venv \
  --exclude-dir=.test-venv \
  -- "${RELEASE_DIR}" 2>/dev/null || true)"
if [[ -n "${PRIVATE_KEY_HITS}" ]]; then
  echo "发布物中检测到私钥标记，拒绝部署；请从 Git 历史和发布包中清除。" >&2
  exit 3
fi
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
  echo "升级前一致备份已创建：${BACKUP_PATH}"
fi

# Dependencies are part of the immutable candidate.  No command below mutates
# the virtual environment of the currently serving release.
python3.11 -m venv "${RELEASE_VENV}"
REQUIREMENTS_LOCK="${RELEASE_DIR}/requirements.lock"
"${RELEASE_VENV}/bin/python" -m pip install --no-cache-dir \
  --constraint "${REQUIREMENTS_LOCK}" "setuptools==84.2.1"
"${RELEASE_VENV}/bin/python" -m pip install --no-cache-dir \
  --no-build-isolation --constraint "${REQUIREMENTS_LOCK}" "${RELEASE_DIR}"
"${RELEASE_VENV}/bin/python" -m pip check
"${RELEASE_VENV}/bin/python" "${RELEASE_DIR}/scripts/generate_sbom.py" \
  --check "${RELEASE_DIR}/sbom/server-runtime.spdx.json"

# Validate the exact runtime environment without installing test-only packages
# into the production venv recorded by the SBOM.  The disposable copy starts
# from the candidate runtime and is removed before any runtime mutation.
cp -a -- "${RELEASE_VENV}" "${RELEASE_TEST_VENV}"
"${RELEASE_TEST_VENV}/bin/python" -m pip install --no-cache-dir \
  --no-build-isolation --constraint "${REQUIREMENTS_LOCK}" \
  "${RELEASE_DIR}[test]"
(
  cd "${RELEASE_DIR}"
  PYTHONWARNINGS=error::SyntaxWarning \
    PYTHONPYCACHEPREFIX="${RELEASE_DIR}/.compile-cache" \
    "${RELEASE_VENV}/bin/python" -m compileall -q \
      license_server register_user.py license_admin.py scripts tests
  PYTHONDONTWRITEBYTECODE=1 "${RELEASE_TEST_VENV}/bin/python" -m pytest \
    -q -p no:cacheprovider tests
  bash -n deploy/el9/*.sh deploy/el9/wkm-license-admin
  bash deploy/el9/test-deployment-transaction.sh
)
rm -rf -- "${RELEASE_TEST_VENV}" "${RELEASE_DIR}/.compile-cache"
wkm_deploy_checkpoint after_candidate_validation

MANIFEST_PATH="${RELEASE_DIR}/config/signing-public-keys.json"
KEY_PATH="${CONFIG_ROOT}/signing-ed25519-${KEY_ID}.pem"
LEGACY_KEY_PATH="${CONFIG_ROOT}/signing-ed25519.pem"
KEY_SOURCE=""
if [[ -e "${KEY_PATH}" || -L "${KEY_PATH}" ]]; then
  wkm_assert_installed_signing_key "${KEY_PATH}" wkm-license
  KEY_SOURCE="${KEY_PATH}"
  if [[ -n "${STAGED_KEY}" && -f "${STAGED_KEY}" ]] && \
      ! cmp -s -- "${STAGED_KEY}" "${KEY_PATH}"; then
    echo "同一 key-id 已安装不同私钥，拒绝覆盖。" >&2
    exit 3
  fi
elif [[ -n "${STAGED_KEY}" && -f "${STAGED_KEY}" ]]; then
  KEY_SOURCE="${STAGED_KEY}"
elif [[ -e "${LEGACY_KEY_PATH}" || -L "${LEGACY_KEY_PATH}" ]]; then
  wkm_assert_installed_signing_key "${LEGACY_KEY_PATH}" wkm-license
  KEY_SOURCE="${LEGACY_KEY_PATH}"
else
  echo "找不到 key-id=${KEY_ID} 的 Ed25519 私钥。" >&2
  exit 3
fi

(
  cd "${RELEASE_DIR}"
  "${RELEASE_VENV}/bin/python" -m license_server signing-key verify \
    --path "${KEY_SOURCE}" --key-id "${KEY_ID}" \
    --manifest "${MANIFEST_PATH}" >/dev/null
)
if [[ ! -f "${KEY_PATH}" ]]; then
  install -o root -g wkm-license -m 0640 "${KEY_SOURCE}" "${KEY_PATH}"
fi
wkm_assert_installed_signing_key "${KEY_PATH}" wkm-license

ENV_PATH="${CONFIG_ROOT}/service.env"
SERVICE_UNIT=/etc/systemd/system/wechat-memory-license.service
ADMIN_COMMAND=/usr/local/sbin/wkm-license-admin
BACKUP_COMMAND=/usr/local/sbin/wkm-license-backup
BACKUP_SERVICE_UNIT=/etc/systemd/system/wechat-memory-license-backup.service
BACKUP_TIMER_UNIT=/etc/systemd/system/wechat-memory-license-backup.timer
INSTALL_TRANSACTION_DIR="$(mktemp -d "${CONFIG_ROOT}/.install-transaction.${RELEASE_ID}.XXXXXX")"
INSTALL_DEPLOY_COMMITTED=0
INSTALL_RUNTIME_ARMED=0
DATABASE_MIGRATION_ATTEMPTED=0

wkm_tx_capture_link "${INSTALL_TRANSACTION_DIR}" "${CURRENT_LINK}" current
wkm_tx_capture_file "${INSTALL_TRANSACTION_DIR}" "${ENV_PATH}" environment
wkm_tx_capture_file "${INSTALL_TRANSACTION_DIR}" "${SERVICE_UNIT}" service-unit
wkm_tx_capture_file "${INSTALL_TRANSACTION_DIR}" "${ADMIN_COMMAND}" admin-command
wkm_tx_capture_file "${INSTALL_TRANSACTION_DIR}" "${BACKUP_COMMAND}" backup-command
wkm_tx_capture_file "${INSTALL_TRANSACTION_DIR}" "${BACKUP_SERVICE_UNIT}" backup-service
wkm_tx_capture_file "${INSTALL_TRANSACTION_DIR}" "${BACKUP_TIMER_UNIT}" backup-timer

SERVICE_WAS_ACTIVE=0
SERVICE_WAS_ENABLED=0
BACKUP_TIMER_WAS_ACTIVE=0
BACKUP_TIMER_WAS_ENABLED=0
if systemctl is-active --quiet wechat-memory-license; then SERVICE_WAS_ACTIVE=1; fi
if systemctl is-enabled --quiet wechat-memory-license; then SERVICE_WAS_ENABLED=1; fi
if systemctl is-active --quiet wechat-memory-license-backup.timer; then
  BACKUP_TIMER_WAS_ACTIVE=1
fi
if systemctl is-enabled --quiet wechat-memory-license-backup.timer; then
  BACKUP_TIMER_WAS_ENABLED=1
fi

restore_unit_state() {
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

rollback_install_runtime() {
  local failed=0
  echo "候选发布失败，正在恢复应用指针、配置、systemd 单元和服务状态。" >&2
  set +e
  wkm_tx_restore_link "${INSTALL_TRANSACTION_DIR}" "${CURRENT_LINK}" current || failed=1
  wkm_tx_restore_file "${INSTALL_TRANSACTION_DIR}" "${ENV_PATH}" environment || failed=1
  wkm_tx_restore_file "${INSTALL_TRANSACTION_DIR}" "${SERVICE_UNIT}" service-unit || failed=1
  wkm_tx_restore_file "${INSTALL_TRANSACTION_DIR}" "${ADMIN_COMMAND}" admin-command || failed=1
  wkm_tx_restore_file "${INSTALL_TRANSACTION_DIR}" "${BACKUP_COMMAND}" backup-command || failed=1
  wkm_tx_restore_file "${INSTALL_TRANSACTION_DIR}" "${BACKUP_SERVICE_UNIT}" backup-service || failed=1
  wkm_tx_restore_file "${INSTALL_TRANSACTION_DIR}" "${BACKUP_TIMER_UNIT}" backup-timer || failed=1
  systemctl daemon-reload || failed=1
  restore_unit_state wechat-memory-license "${SERVICE_WAS_ENABLED}" \
    "${SERVICE_WAS_ACTIVE}" || failed=1
  restore_unit_state wechat-memory-license-backup.timer \
    "${BACKUP_TIMER_WAS_ENABLED}" "${BACKUP_TIMER_WAS_ACTIVE}" || failed=1
  set -e
  if (( DATABASE_MIGRATION_ATTEMPTED )); then
    echo "警告：SQLite 已进入候选版本的 init/migration 阶段，部署器不会在服务可能产生新写入后自动降级数据库。" >&2
    if [[ -n "${BACKUP_PATH}" ]]; then
      echo "升级前一致备份：${BACKUP_PATH}" >&2
    else
      echo "本次是首次建库，不存在可自动恢复的旧库。" >&2
    fi
  fi
  if (( failed )); then
    echo "回滚未完整成功，事务快照将保留在 ${INSTALL_TRANSACTION_DIR}。" >&2
  else
    echo "应用运行态已恢复到部署前快照。" >&2
  fi
  return "${failed}"
}

on_install_exit() {
  local status=$?
  local rollback_failed=0
  trap - EXIT
  if (( ! INSTALL_DEPLOY_COMMITTED && INSTALL_RUNTIME_ARMED )); then
    if ! rollback_install_runtime; then
      rollback_failed=1
    fi
  fi
  if (( ! rollback_failed )); then
    rm -rf -- "${INSTALL_TRANSACTION_DIR}"
  else
    echo "请保留事务快照并立即按运维手册人工恢复；下次部署会失败关闭。" >&2
    if (( status == 0 )); then status=5; fi
  fi
  exit "${status}"
}
INSTALL_RUNTIME_ARMED=1
trap on_install_exit EXIT

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
DATABASE_PATH=${STATE_ROOT}/license.sqlite3
LICENSE_ISSUER=$(env_value LICENSE_ISSUER wechat-memory-license)
LICENSE_AUDIENCE=$(env_value LICENSE_AUDIENCE wechat-memory-client)
REFRESH_TOKEN_DAYS=$(env_value REFRESH_TOKEN_DAYS 365)
LICENSE_TICKET_HOURS=$(env_value LICENSE_TICKET_HOURS 24)
RATE_LIMIT_PER_MINUTE=$(env_value RATE_LIMIT_PER_MINUTE 20)
RATE_LIMIT_GLOBAL_PER_MINUTE=$(env_value RATE_LIMIT_GLOBAL_PER_MINUTE 1000)
RATE_LIMIT_MAX_KEYS=$(env_value RATE_LIMIT_MAX_KEYS 4096)
SIGNING_KEY_ID=${KEY_ID}
SIGNING_PRIVATE_KEY_PATH=${KEY_PATH}
SIGNING_PUBLIC_KEYS_PATH=${CURRENT_LINK}/config/signing-public-keys.json
EOF
install -o root -g wkm-license -m 0640 "${ENV_TMP}" "${ENV_PATH}"
rm -f -- "${ENV_TMP}"
wkm_deploy_checkpoint after_environment_install

DATABASE_MIGRATION_ATTEMPTED=1
/usr/sbin/runuser -u wkm-license -- "${RELEASE_VENV}/bin/python" \
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

ln -sfn "${RELEASE_DIR}" "${CURRENT_LINK}.next"
mv -Tf "${CURRENT_LINK}.next" "${CURRENT_LINK}"
systemctl daemon-reload
wkm_deploy_checkpoint after_application_switch
systemctl enable wechat-memory-license
if ! systemctl restart wechat-memory-license; then
  echo "授权服务启动失败。" >&2
  exit 4
fi
wkm_deploy_checkpoint after_service_restart
systemctl enable --now wechat-memory-license-backup.timer
if ! curl --fail --silent --show-error --connect-timeout 3 --max-time 15 \
    http://127.0.0.1:9100/readyz/deep >/dev/null; then
  echo "候选版本的本机深度健康检查失败。" >&2
  exit 4
fi

INSTALL_DEPLOY_COMMITTED=1
trap - EXIT
rm -rf -- "${INSTALL_TRANSACTION_DIR}"

echo "授权 API 已就绪：${RELEASE_DIR}"
curl --silent http://127.0.0.1:9100/v1/keys
echo
