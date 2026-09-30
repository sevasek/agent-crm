#!/bin/sh
# POSIX helpers for docker-entrypoint.sh.
#
# Production compose drops CAP_DAC_OVERRIDE. After the first successful boot
# /app/data is APP_UID:APP_GID mode 0700, and root cannot traverse it to redo
# chown -R. Only repair when the directory is traversable *and* ownership is
# wrong. If we cannot even stat/ls it, print a host-side chown command instead
# of crash-looping on "chown: Permission denied" (issue 70).
#
# Also runnable without Docker:
#   sh docker-entrypoint-lib.sh decide DIR UID GID
#   sh docker-entrypoint-lib.sh prepare DIR UID GID

# Owner uid of a path, empty on failure (including "cannot stat").
data_dir_stat_uid() {
  stat -c '%u' "$1" 2>/dev/null || true
}

data_dir_stat_gid() {
  stat -c '%g' "$1" 2>/dev/null || true
}

# True when this process can list and search the directory (needed for
# chown -R / find / chmod of children).
data_dir_can_traverse() {
  [ -d "$1" ] && [ -r "$1" ] && [ -x "$1" ]
}

# Prints skip | repair | error
decide_data_volume_action() {
  _dir="$1"
  _uid="$2"
  _gid="$3"

  _owner_uid=$(data_dir_stat_uid "$_dir")
  _owner_gid=$(data_dir_stat_gid "$_dir")

  if [ -n "$_owner_uid" ] && [ -n "$_owner_gid" ]; then
    if [ "$_owner_uid" = "$_uid" ] && [ "$_owner_gid" = "$_gid" ]; then
      echo skip
      return 0
    fi
    if data_dir_can_traverse "$_dir"; then
      echo repair
      return 0
    fi
    echo error
    return 0
  fi

  # Could not stat: missing (parent searchable) vs unreadable.
  _parent=$(dirname "$_dir")
  if [ -d "$_parent" ] && [ -x "$_parent" ] && [ ! -e "$_dir" ]; then
    echo repair
    return 0
  fi

  echo error
  return 0
}

print_data_volume_unreadable_error() {
  _dir="$1"
  _uid="$2"
  _gid="$3"
  echo "ERROR: cannot access ${_dir} to repair ownership." >&2
  echo "Root without CAP_DAC_OVERRIDE cannot traverse a 0700 directory owned by another user, so chown/chmod cannot succeed. The container will not recover by restarting." >&2
  echo "Fix the bind-mount on the host, then start the container:" >&2
  echo "  chown -R ${_uid}:${_gid} ./data" >&2
  echo "  chmod 700 ./data" >&2
  echo "Use your APP_UID and APP_GID if they are not ${_uid}:${_gid}. scripts/new-instance.sh applies this ownership before the first start." >&2
}

repair_data_volume() {
  _dir="$1"
  _uid="$2"
  _gid="$3"

  mkdir -p "$_dir"
  if ! chown -R "${_uid}:${_gid}" "$_dir"; then
    print_data_volume_unreadable_error "$_dir" "$_uid" "$_gid"
    return 1
  fi
  # Files first, then directories deepest-first: chmod 700 of a parent must
  # not lock this root-without-DAC_OVERRIDE process out of children.
  if command -v find >/dev/null 2>&1; then
    if ! find "$_dir" -type f -exec chmod 600 {} +; then
      print_data_volume_unreadable_error "$_dir" "$_uid" "$_gid"
      return 1
    fi
    if ! find "$_dir" -depth -type d -exec chmod 700 {} +; then
      print_data_volume_unreadable_error "$_dir" "$_uid" "$_gid"
      return 1
    fi
  else
    chmod 700 "$_dir"
    for _f in "$_dir"/*; do
      [ -e "$_f" ] || continue
      if [ -d "$_f" ]; then
        chmod 700 "$_f"
      else
        chmod 600 "$_f"
      fi
    done
  fi
}

prepare_data_volume_as_root() {
  _dir="$1"
  _uid="$2"
  _gid="$3"

  _action=$(decide_data_volume_action "$_dir" "$_uid" "$_gid")
  case "$_action" in
    skip)
      return 0
      ;;
    repair)
      repair_data_volume "$_dir" "$_uid" "$_gid"
      ;;
    error)
      print_data_volume_unreadable_error "$_dir" "$_uid" "$_gid"
      return 1
      ;;
    *)
      echo "ERROR: unexpected data-volume action '${_action}'" >&2
      return 1
      ;;
  esac
}

# CLI when this file is executed (tests); no-op when sourced by the entrypoint.
case "${0##*/}" in
  docker-entrypoint-lib.sh)
    set -eu
    _cmd="${1:-}"
    case "$_cmd" in
      decide)
        decide_data_volume_action "${2:?}" "${3:?}" "${4:?}"
        ;;
      prepare)
        prepare_data_volume_as_root "${2:?}" "${3:?}" "${4:?}"
        ;;
      repair)
        repair_data_volume "${2:?}" "${3:?}" "${4:?}"
        ;;
      *)
        echo "Usage: docker-entrypoint-lib.sh decide|prepare|repair DIR UID GID" >&2
        exit 2
        ;;
    esac
    ;;
esac
