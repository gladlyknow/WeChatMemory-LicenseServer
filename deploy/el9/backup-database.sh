#!/usr/bin/env bash
set -euo pipefail
umask 077

DATABASE_PATH="${DATABASE_PATH:-/var/lib/wechat-memory-license/license.sqlite3}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/lib/wechat-memory-license/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
SESSION_RETENTION_DAYS="${REFRESH_SESSION_RETENTION_DAYS:-90}"
SESSION_BATCH_SIZE="${REFRESH_SESSION_PRUNE_BATCH_SIZE:-500}"
SESSION_MAX_BATCHES="${REFRESH_SESSION_PRUNE_MAX_BATCHES:-20}"
DEPLOY_LOCK_PATH=/run/wechat-memory-license-deploy.lock

if [[ ${EUID} -ne 0 ]]; then
  echo "请以 root 执行数据库备份。" >&2
  exit 2
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "缺少 flock，无法确认数据库当前可安全备份。" >&2
  exit 3
fi
if [[ -L "${DEPLOY_LOCK_PATH}" ]] || \
   { [[ -e "${DEPLOY_LOCK_PATH}" ]] && [[ ! -f "${DEPLOY_LOCK_PATH}" ]]; }; then
  echo "部署锁路径不是受控普通文件。" >&2
  exit 3
fi
exec {BACKUP_LOCK_FD}<>"${DEPLOY_LOCK_PATH}"
chmod 0600 "${DEPLOY_LOCK_PATH}"
if ! flock -s -n "${BACKUP_LOCK_FD}"; then
  echo "授权服务正在部署或迁移，本次备份未启动。" >&2
  exit 5
fi
if [[ ! -f "${DATABASE_PATH}" ]]; then
  echo "找不到授权数据库。" >&2
  exit 3
fi
if [[ ! "${RETENTION_DAYS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "BACKUP_RETENTION_DAYS 必须是正整数。" >&2
  exit 3
fi
for value in "${SESSION_RETENTION_DAYS}" "${SESSION_BATCH_SIZE}" "${SESSION_MAX_BATCHES}"; do
  if [[ ! "${value}" =~ ^[1-9][0-9]*$ ]]; then
    echo "刷新会话清理参数必须是正整数。" >&2
    exit 3
  fi
done

install -d -o wkm-license -g wkm-license -m 0700 "${BACKUP_ROOT}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TEMPORARY="${BACKUP_ROOT}/.license-${STAMP}.sqlite3.tmp"
DESTINATION="${BACKUP_ROOT}/license-${STAMP}.sqlite3"

/usr/sbin/runuser -u wkm-license -- \
  /usr/bin/sqlite3 "${DATABASE_PATH}" ".backup '${TEMPORARY}'"
if [[ "$(/usr/sbin/runuser -u wkm-license -- /usr/bin/sqlite3 "${TEMPORARY}" 'PRAGMA integrity_check;')" != "ok" ]]; then
  rm -f -- "${TEMPORARY}"
  echo "SQLite 备份完整性检查失败。" >&2
  exit 4
fi
mv -- "${TEMPORARY}" "${DESTINATION}"
chown wkm-license:wkm-license "${DESTINATION}"
chmod 0600 "${DESTINATION}"

find "${BACKUP_ROOT}" -maxdepth 1 -type f -name 'license-*.sqlite3' \
  -mtime "+${RETENTION_DAYS}" -delete

# Cleanup only after a verified backup exists.  The administrator command has
# hard batch ceilings, uses short write transactions and never deletes active
# sessions, so this timer cannot turn into an unbounded request-path scan.
/usr/local/sbin/wkm-license-admin --json prune-refresh-sessions \
  --retention-days "${SESSION_RETENTION_DAYS}" \
  --batch-size "${SESSION_BATCH_SIZE}" \
  --max-batches "${SESSION_MAX_BATCHES}"
echo "备份完成：${DESTINATION}"
