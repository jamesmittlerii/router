#!/usr/bin/env bash
set -euo pipefail

# ---------- Config ----------
SFIZZ_URI="http://sfztools.github.io/sfizz"
SFIZZ_NAME="sfizz_rhodes"
SFIZZ_PRESET="urn:sfizz:presets:jrhodes"

CHORUS_URI="http://calf.sourceforge.net/plugins/MultiChorus"
CHORUS_NAME="chorus"

REVERB_URI="http://calf.sourceforge.net/plugins/Reverb"
REVERB_NAME="reverb"

GAIN_URI="http://moddevices.com/plugins/mod-devel/Gain2x2"
GAIN_NAME="gain"
MASTER_GAIN_DB="-3.0"

# MIDI source
MIDI_SRC="system:midi_capture_1"
SFIZZ_MIDI_IN="${SFIZZ_NAME}:control"

# Ports
SFIZZ_L="${SFIZZ_NAME}:out_left"
SFIZZ_R="${SFIZZ_NAME}:out_right"

CHORUS_IN_L="${CHORUS_NAME}:in_l"
CHORUS_IN_R="${CHORUS_NAME}:in_r"
CHORUS_OUT_L="${CHORUS_NAME}:out_l"
CHORUS_OUT_R="${CHORUS_NAME}:out_r"

REVERB_IN_L="${REVERB_NAME}:in_l"
REVERB_IN_R="${REVERB_NAME}:in_r"
REVERB_OUT_L="${REVERB_NAME}:out_l"
REVERB_OUT_R="${REVERB_NAME}:out_r"

GAIN_IN_L="${GAIN_NAME}:In1"
GAIN_IN_R="${GAIN_NAME}:In2"
GAIN_OUT_L="${GAIN_NAME}:Out1"
GAIN_OUT_R="${GAIN_NAME}:Out2"

PLAY_L="system:playback_1"
PLAY_R="system:playback_2"

# Where we keep pids for cleanup
PIDDIR="/tmp/rhodes-chain"
mkdir -p "$PIDDIR"

log() { echo "[rhodes-chain] $*"; }

have_jack() { jack_lsp >/dev/null 2>&1; }

is_running() {
  local name="$1" uri="$2"
  pgrep -f "jalv .* -n ${name} ${uri}" >/dev/null 2>&1
}

wait_port() {
  local port="$1"
  local tries=120  # ~12s
  for ((i=0; i<tries; i++)); do
    if jack_lsp 2>/dev/null | grep -Fxq "$port"; then
      return 0
    fi
    sleep 0.1
  done
  echo "ERROR: Timed out waiting for JACK port: $port" >&2
  exit 1
}

connect_once() {
  local src="$1" dst="$2"
  if jack_lsp -c 2>/dev/null | awk -v s="$src" -v d="$dst" '
    $0==s {insrc=1; next}
    insrc && $0 ~ /^ *-> / { if (index($0,d)>0) found=1 }
    insrc && $0 !~ /^ *-> / && $0!=s { insrc=0 }
    END { exit(found?0:1) }
  '; then
    log "Already connected: $src -> $dst"
  else
    log "Connecting: $src -> $dst"
    jack_connect "$src" "$dst"
  fi
}

fifo_path() {
  local name="$1"
  echo "/tmp/${name}_control"
}

pidfile_jalv() {
  local name="$1"
  echo "${PIDDIR}/${name}.jalv.pid"
}

pidfile_writer() {
  local name="$1"
  echo "${PIDDIR}/${name}.writer.pid"
}

# Create FIFO + start a perma-writer that keeps it open
ensure_fifo_and_writer() {
  local name="$1"
  local fifo
  fifo="$(fifo_path "$name")"

  if [[ ! -p "$fifo" ]]; then
    rm -f "$fifo"
    log "Creating FIFO: $fifo"
    mkfifo "$fifo"
  fi
}

start_jalv_with_fifo() {
  local name="$1" uri="$2"
  local fifo
  fifo="$(fifo_path "$name")"

  if is_running "$name" "$uri"; then
    log "Already running: $name"
    return 0
  fi

  ensure_fifo_and_writer "$name"

  log "Starting jalv: $name"
  # stdin from fifo; stdout/stderr to log
  jalv -n "$name" "$uri" <"$fifo" >/tmp/jalv-"$name".log 2>&1 &
  echo jalv -n "$name" "$uri"
  #jalv -n "$name" "$uri" <"$fifo" 
  echo $! >"$(pidfile_jalv "$name")"
  log "launched (maybe)"
}

send_cmd() {
  local name="$1" cmd="$2"
  local fifo
  fifo="$(fifo_path "$name")"
  log "CMD -> $name: $cmd"
  printf "%s\n" "$cmd" >"$fifo"
}

stop_chain() {
  log "Stopping chain..."

  for name in "$SFIZZ_NAME" "$CHORUS_NAME" "$REVERB_NAME" "$GAIN_NAME"; do
    # Stop jalv
    local jpidfile wpidfile
    jpidfile="$(pidfile_jalv "$name")"
    if [[ -f "$jpidfile" ]]; then
      local pid
      pid="$(cat "$jpidfile" || true)"
      if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
        log "Killing jalv $name (pid $pid)"
        kill "$pid" 2>/dev/null || true
      fi
      rm -f "$jpidfile"
    fi

    # Stop writer
    wpidfile="$(pidfile_writer "$name")"
    if [[ -f "$wpidfile" ]]; then
      local wpid
      wpid="$(cat "$wpidfile" || true)"
      if [[ -n "${wpid:-}" ]] && kill -0 "$wpid" 2>/dev/null; then
        log "Killing fifo-writer $name (pid $wpid)"
        kill "$wpid" 2>/dev/null || true
      fi
      rm -f "$wpidfile"
    fi

    # Remove fifo
    rm -f "$(fifo_path "$name")" || true
  done

  log "Stopped."
}

# ---------- CLI ----------
case "${1:-}" in
  --stop)
    stop_chain
    exit 0
    ;;
esac

# ---------- Main ----------
if ! have_jack; then
  echo "ERROR: JACK is not running (jack_lsp failed)." >&2
  exit 1
fi

# Start plugins (stdin controlled via FIFO)
start_jalv_with_fifo "$SFIZZ_NAME"  "$SFIZZ_URI"
start_jalv_with_fifo "$CHORUS_NAME" "$CHORUS_URI"
start_jalv_with_fifo "$REVERB_NAME" "$REVERB_URI"
start_jalv_with_fifo "$GAIN_NAME"   "$GAIN_URI"

# Wait for ports to exist
wait_port "$SFIZZ_MIDI_IN"
wait_port "$SFIZZ_L"
wait_port "$SFIZZ_R"
wait_port "$CHORUS_IN_L"
wait_port "$REVERB_IN_L"
wait_port "$GAIN_IN_L"

# Apply settings (via FIFO commands)
send_cmd "$SFIZZ_NAME" "preset $SFIZZ_PRESET"
send_cmd "$GAIN_NAME"  "set Gain $MASTER_GAIN_DB"

# Wire MIDI
connect_once "$MIDI_SRC" "$SFIZZ_MIDI_IN"

# Wire audio chain
connect_once "$SFIZZ_L"      "$CHORUS_IN_L"
connect_once "$SFIZZ_R"      "$CHORUS_IN_R"
connect_once "$CHORUS_OUT_L" "$REVERB_IN_L"
connect_once "$CHORUS_OUT_R" "$REVERB_IN_R"
connect_once "$REVERB_OUT_L" "$GAIN_IN_L"
connect_once "$REVERB_OUT_R" "$GAIN_IN_R"
connect_once "$GAIN_OUT_L"   "$PLAY_L"
connect_once "$GAIN_OUT_R"   "$PLAY_R"

log "Done."
log "To send commands later:  echo 'set Gain -6.0' > $(fifo_path "$GAIN_NAME")"
log "To stop everything:      $0 --stop"
