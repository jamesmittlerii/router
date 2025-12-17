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
# sfizz LV2 atom/midi input
SFIZZ_MIDI_IN="${SFIZZ_NAME}:control"

# Audio ports (as seen in your jack_lsp output)
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

# ---------- Helpers ----------
log() { echo "[rhodes-chain] $*"; }

have_jack() {
  jack_lsp >/dev/null 2>&1
}

is_running() {
  # match: jalv -n <name> <uri>
  pgrep -f "jalv .* -n ${1} ${2}" >/dev/null 2>&1
}

start_jalv() {
  local name="$1" uri="$2"
  if is_running "$name" "$uri"; then
    log "Already running: $name"
    return 0
  fi
  log "Starting: $name"
  # -i is crucial on your system
  jalv -i -n "$name" "$uri" >/tmp/jalv-"$name".log 2>&1 &
}

wait_port() {
  local port="$1"
  local tries=80  # ~8s total
  local i
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

# Run a jalv control command against a named instance.
# Note: jalv -c can be a little picky; we feed it a tiny script.
jalv_ctl() {
  local name="$1" uri="$2" cmd="$3"
  printf "%s\nquit\n" "$cmd" | jalv -c -n "$name" "$uri" >/dev/null 2>&1 || true
}

# ---------- Main ----------
if ! have_jack; then
  echo "ERROR: JACK is not running (jack_lsp failed)." >&2
  exit 1
fi

# Start plugins
start_jalv "$SFIZZ_NAME"  "$SFIZZ_URI"
start_jalv "$CHORUS_NAME" "$CHORUS_URI"
start_jalv "$REVERB_NAME" "$REVERB_URI"
start_jalv "$GAIN_NAME"   "$GAIN_URI"

# Wait for ports to exist
wait_port "$SFIZZ_L"
wait_port "$SFIZZ_R"
wait_port "$SFIZZ_MIDI_IN"
wait_port "$CHORUS_IN_L"
wait_port "$REVERB_IN_L"
wait_port "$GAIN_IN_L"

# Load sfizz preset (this is how you load the SFZ path via LV2 state)
log "Applying sfizz preset: $SFIZZ_PRESET"
jalv_ctl "$SFIZZ_NAME" "$SFIZZ_URI" "preset $SFIZZ_PRESET"

# Set master gain to -3.0 dB (symbol is usually Gain; if not, adjust)
log "Setting master gain to ${MASTER_GAIN_DB} dB"
jalv_ctl "$GAIN_NAME" "$GAIN_URI" "set Gain $MASTER_GAIN_DB"

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

log "Done. Logs: /tmp/jalv-sfizz_rhodes.log /tmp/jalv-chorus.log /tmp/jalv-reverb.log /tmp/jalv-gain.log"
