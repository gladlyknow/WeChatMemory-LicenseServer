#!/usr/bin/env bash
set -euo pipefail
umask 077

DATABASE_PATH="${DATABASE_PATH:-/var/lib/wechat-memory-license/license.sqlite3}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/lib/wechat-memory-license/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

if [[ ${EUID} -ne 0 ]]; then
  echo "请以 root 执行数据库备份。" >&2
  exit 2
fi
if [[ ! -f "${DATABASE_PATH}" ]]; then
  echo "找不到授权数据库。" >&2
  exit 3
fi
if [[ ! "${RETENTION_DAYS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "BACKUP_RETENTION_DAYS 必须是正整数。" >&2
  exit 3
fi

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
echo "备份完成：${DESTINATION}"
