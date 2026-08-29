#!/bin/sh

runtime_dir=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
state=$(cat "$runtime_dir/voxtype/state" 2>/dev/null) || exit 0
health=$(cut -f1 "$runtime_dir/voxtype/mic-health" 2>/dev/null)

if [ "$health" = "failed" ]; then
  printf '⚠ MIC'
  exit 0
fi

case "$state" in
  recording)
    if [ "$health" = "checking" ]; then
      printf '🎤 CHECK'
    else
      printf '🎤 REC'
    fi
    ;;
  transcribing)
    printf '⏳ STT'
    ;;
esac
