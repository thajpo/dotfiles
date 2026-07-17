#!/bin/sh

runtime_dir=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
state=$(cat "$runtime_dir/voxtype/state" 2>/dev/null) || exit 0

case "$state" in
  recording)
    printf '🎤 REC'
    ;;
  transcribing)
    printf '⏳ STT'
    ;;
esac
