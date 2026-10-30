#!/usr/bin/env bash
# Deterministic permission policy for immutable release candidates.
#
# Candidate trees can come from either ``git archive`` or an uploaded source
# bundle.  Normalize both paths identically: directories are traversable,
# ordinary files are non-executable, and only this audited allow-list regains
# an execute bit.  Keeping the list here also prevents archive metadata or a
# bundle's incoming modes from deciding what root will execute.

WKM_RELEASE_EXECUTABLE_PATHS=(
  deploy/el9/backup-database.sh
  deploy/el9/deploy-from-data.sh
  deploy/el9/deployment-transaction.sh
  deploy/el9/install-release.sh
  deploy/el9/one-click-deploy.sh
  deploy/el9/test-deployment-transaction.sh
  deploy/el9/wkm-license-admin
  register_user.py
  scripts/check_documentation.py
  tests/fixtures/ip-command-shim.sh
  tests/fixtures/selinux-command-shim.sh
)

wkm_release_executable_paths() {
  printf '%s\n' "${WKM_RELEASE_EXECUTABLE_PATHS[@]}"
}

wkm_assert_git_release_executable_contract() {
  local source_dir="$1"
  local tree_listing actual expected

  if ! tree_listing="$(git -C "${source_dir}" ls-tree -r --full-tree HEAD)"; then
    echo "无法读取 Git HEAD 的可执行文件清单，拒绝生成候选发布。" >&2
    return 1
  fi
  actual="$(
    printf '%s\n' "${tree_listing}" |
      awk '$1 == "100755" { sub(/^[^\t]*\t/, ""); print }' |
      LC_ALL=C sort
  )"
  expected="$(wkm_release_executable_paths | LC_ALL=C sort)"
  if [[ "${actual}" != "${expected}" ]]; then
    echo "Git HEAD 的 100755 文件与审核白名单不一致，拒绝生成候选发布。" >&2
    echo "审核白名单：" >&2
    printf '%s\n' "${expected}" >&2
    echo "Git HEAD：" >&2
    printf '%s\n' "${actual}" >&2
    return 1
  fi
}

wkm_normalize_release_permissions() {
  local release_dir="$1"
  local relative_path target

  if [[ ! -d "${release_dir}" || -L "${release_dir}" ]]; then
    echo "候选发布目录不是普通目录：${release_dir}" >&2
    return 1
  fi

  # Never trust executable bits supplied by an archive or uploaded bundle.
  find "${release_dir}" -type d -exec chmod 0755 -- {} + || return 1
  find "${release_dir}" -type f -exec chmod 0644 -- {} + || return 1

  for relative_path in "${WKM_RELEASE_EXECUTABLE_PATHS[@]}"; do
    case "${relative_path}" in
      ""|/*|.|..|../*|*/../*|*/..)
        echo "发布可执行白名单含不安全路径：${relative_path:-<empty>}" >&2
        return 1
        ;;
    esac
    target="${release_dir}/${relative_path}"
    if [[ ! -f "${target}" || -L "${target}" ]]; then
      echo "发布可执行白名单目标缺失或不是普通文件：${relative_path}" >&2
      return 1
    fi
    chmod 0755 -- "${target}" || return 1
  done
}

wkm_assert_runtime_venv_permissions() {
  local venv_dir="$1"
  local expected_owner_id="$2"
  local expected_group_id="$3"
  local bad_path

  if [[ ! -d "${venv_dir}" || -L "${venv_dir}" ]]; then
    echo "Python venv 不是普通目录：${venv_dir}" >&2
    return 1
  fi
  bad_path="$(
    find "${venv_dir}" -xdev \
      \( -type d -o -type f \) \
      \( ! -uid "${expected_owner_id}" -o ! -gid "${expected_group_id}" \
         -o -perm /0020 -o -perm /0007 \) \
      -print -quit
  )"
  if [[ -n "${bad_path}" ]]; then
    echo "Python venv owner/group 或写权限不符合最小权限约束：${bad_path}" >&2
    return 1
  fi
  bad_path="$(
    find "${venv_dir}" -xdev -type d ! -perm -0050 -print -quit
  )"
  if [[ -n "${bad_path}" ]]; then
    echo "Python venv 目录对运行组不可读/遍历：${bad_path}" >&2
    return 1
  fi
  bad_path="$(
    find "${venv_dir}" -xdev -type f ! -perm -0040 -print -quit
  )"
  if [[ -n "${bad_path}" ]]; then
    echo "Python venv 文件对运行组不可读：${bad_path}" >&2
    return 1
  fi
  bad_path="$(
    find "${venv_dir}" -xdev -type f -perm /0100 ! -perm -0010 \
      -print -quit
  )"
  if [[ -n "${bad_path}" ]]; then
    echo "Python venv 可执行文件对运行组不可执行：${bad_path}" >&2
    return 1
  fi
}

wkm_prepare_root_owned_runtime_venv() {
  local venv_dir="$1"
  local runtime_group="$2"
  local runtime_group_id special_path

  if [[ ! -d "${venv_dir}" || -L "${venv_dir}" ]]; then
    echo "Python venv 不是普通目录：${venv_dir}" >&2
    return 1
  fi
  if ! runtime_group_id="$(getent group "${runtime_group}" | cut -d: -f3)" || \
     [[ ! "${runtime_group_id}" =~ ^[0-9]+$ ]]; then
    echo "无法解析 Python venv 运行组：${runtime_group}" >&2
    return 1
  fi
  special_path="$(
    find "${venv_dir}" -xdev \
      ! -type d ! -type f ! -type l -print -quit
  )"
  if [[ -n "${special_path}" ]]; then
    echo "Python venv 含不允许的特殊文件：${special_path}" >&2
    return 1
  fi

  # Do not give the service account ownership or write access.  Root remains
  # the sole writer; the runtime group receives only the read/traverse/execute
  # bits required to import packages and launch the interpreter.
  find "${venv_dir}" -xdev \( -type d -o -type f \) \
    -exec chown 0:"${runtime_group_id}" -- {} + || return 1
  chmod -R u=rwX,g=rX,o= "${venv_dir}" || return 1
  wkm_assert_runtime_venv_permissions \
    "${venv_dir}" 0 "${runtime_group_id}"
}
