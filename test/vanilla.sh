#!/usr/bin/env bash
set -euo pipefail

# ---- FIFOs ----
SFIZZ_FIFO="/tmp/sfizz_rhodes_control"
CHORUS_FIFO="/tmp/chorus_control"
REVERB_FIFO="/tmp/reverb_control"
GAIN_FIFO="/tmp/gain_control"

# ---- Ensure FIFO exists ----
ensure_fifo() {
  local f="$1"
  if [[ -e "$f" && ! -p "$f" ]]; then
    echo "ERROR: $f exists but is not a FIFO" >&2
    exit 1
  fi
  if [[ ! -p "$f" ]]; then
    echo "[jalv] creating FIFO $f"
    rm -f "$f"
    mkfifo "$f"
  fi
}

# ---- Start one jalv ----
start_jalv() {
  local name="$1"
  local uri="$2"
  local fifo="$3"
  local log="/tmp/jalv-${name}.log"

  # Skip if already running
  if pgrep -f "jalv .* -n ${name} ${uri}" >/dev/null 2>&1; then
    echo "[jalv] already running: $name"
    return
  fi

  ensure_fifo "$fifo"

  echo "[jalv] starting $name"
  /usr/bin/jalv -i -n "$name" "$uri" \
    < "$fifo" > "$log" 2>&1 &
}

# ---- Main ----
start_jalv sfizz_rhodes "http://sfztools.github.io/sfizz" "$SFIZZ_FIFO"
start_jalv chorus       "http://calf.sourceforge.net/plugins/MultiChorus" "$CHORUS_FIFO"
start_jalv reverb       "http://calf.sourceforge.net/plugins/Reverb" "$REVERB_FIFO"
start_jalv gain         "http://moddevices.com/plugins/mod-devel/Gain2x2" "$GAIN_FIFO"

echo "[jalv] done"
echo
echo "Send commands like:"
echo "  printf 'controls\n' > /tmp/gain_control"
echo "  printf 'preset urn:sfizz:presets:jrhodes\n' > /tmp/sfizz_rhodes_control"
echo
echo "Watch logs:"
echo "  tail -f /tmp/jalv-gain.log"
