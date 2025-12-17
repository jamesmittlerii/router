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

# MIDI source (your working setup)
MIDI_SRC="system:midi_capture_1"
SFIZZ_MIDI_IN="${SFIZZ_NAME}:control"

# Audio ports
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

# If you want the script to always start from scratch:
RESET_FIRST=1

# ---------- Helpers ----------
log() { echo "[rhodes-chain] $*"; }

have_jack() { jack_lsp >/dev/null 2>&1; }

kill_chain() {
  log "Stopping any existing jalv instances for this chain..."
  pkill -f "jalv .* -n ${SFIZZ_NAME} ${SFIZZ_URI}"  || true
  pkill -f "jalv .* -n ${CHORUS_NAME} ${CHORUS_URI}" || true
  pkill -f "jalv .* -n ${REVERB_NAME} ${REVERB_URI}" || true
  pkill -f "jalv .* -n ${GAIN_NAME} ${GAIN_URI}"     || true
}

wait_port() {
  local port="$1"
  local tries=120  # ~12s
  for ((i=0; i<tries; i++)); do
    if jack_lsp 2>/dev/null | grep -Fxq "$port"; then return 0; fi
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
    # retry a few times to avoid race conditions
    for _ in 1 2 3 4 5; do
      if jack_connect "$src" "$dst" 2>/dev/null; then
        return 0
      fi
      sleep 0.1
    done
    echo "ERROR: failed to connect $src -> $dst" >&2
    exit 1
  fi
}

# FIFO control channel per instance
cmd_fifo() { echo "/tmp/jalv-${1}.cmd"; }
log_file() { echo "/tmp/jalv-${1}.log"; }

# Keep a FD open per instance so jalv doesn't block on FIFO open
declare -A CMD_FD

start_jalv_fifo() {
  local name="$1" uri="$2"

  local fifo; fifo="$(cmd_fifo "$name")"
  local logf; logf="$(log_file "$name")"

  rm -f "$fifo"
  mkfifo "$fifo"

  # Open the FIFO read/write so both ends are open (prevents startup deadlock)
  exec {fd}<>"$fifo"
  CMD_FD["$name"]=$fd

  log "Starting: $name"
  jalv -i -n "$name" "$uri" <&"$fd" >"$logf" 2>&1 &
}

send_cmd() {
  local name="$1" cmd="$2"
  local fifo; fifo="$(cmd_fifo "$name")"
  printf "%s\n" "$cmd" >"$fifo"
}

# ---------- Main ----------
if ! have_jack; then
  echo "ERROR: JACK is not running (jack_lsp failed)." >&2
  exit 1
fi

if [[ "$RESET_FIRST" -eq 1 ]]; then
  kill_chain
  # give JACK a moment to drop ports
  sleep 0.2
  rm -f /tmp/jalv-"${SFIZZ_NAME}".log /tmp/jalv-"${CHORUS_NAME}".log /tmp/jalv-"${REVERB_NAME}".log /tmp/jalv-"${GAIN_NAME}".log || true
fi

# Start plugins with FIFO control
start_jalv_fifo "$SFIZZ_NAME"  "$SFIZZ_URI"
start_jalv_fifo "$CHORUS_NAME" "$CHORUS_URI"
start_jalv_fifo "$REVERB_NAME" "$REVERB_URI"
start_jalv_fifo "$GAIN_NAME"   "$GAIN_URI"

# Wait for key ports
wait_port "$SFIZZ_MIDI_IN"
wait_port "$SFIZZ_L"
wait_port "$SFIZZ_R"
wait_port "$CHORUS_IN_L"
wait_port "$REVERB_IN_L"
wait_port "$GAIN_IN_L"
wait_port "$GAIN_OUT_L"
wait_port "$PLAY_L"
wait_port "$PLAY_R"

# Apply sfizz preset + set gain via FIFO commands
log "Applying sfizz preset: $SFIZZ_PRESET"
send_cmd "$SFIZZ_NAME" "preset $SFIZZ_PRESET"

log "Setting master gain to ${MASTER_GAIN_DB} dB"
send_cmd "$GAIN_NAME" "set Gain $MASTER_GAIN_DB"

# (Optional) dump controls to logs for verification
send_cmd "$SFIZZ_NAME" "controls"
send_cmd "$GAIN_NAME" "controls"

# Wire MIDI + audio
connect_once "$MIDI_SRC" "$SFIZZ_MIDI_IN"

connect_once "$SFIZZ_L"      "$CHORUS_IN_L"
connect_once "$SFIZZ_R"      "$CHORUS_IN_R"
connect_once "$CHORUS_OUT_L" "$REVERB_IN_L"
connect_once "$CHORUS_OUT_R" "$REVERB_IN_R"
connect_once "$REVERB_OUT_L" "$GAIN_IN_L"
connect_once "$REVERB_OUT_R" "$GAIN_IN_R"
connect_once "$GAIN_OUT_L"   "$PLAY_L"
connect_once "$GAIN_OUT_R"   "$PLAY_R"

log "Done."
log "Logs:"
log "  $(log_file "$SFIZZ_NAME")"
log "  $(log_file "$CHORUS_NAME")"
log "  $(log_file "$REVERB_NAME")"
log "  $(log_file "$GAIN_NAME")"
