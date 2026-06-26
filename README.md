# Router

Headless instrument router for a custom **mod-host** + **JACK** live rig. Pedalboard layouts are defined as MOD-style JSON configs, loaded at startup, and switched at runtime via **MIDI Program Change** — no MOD UI required.

The main use case: preload many **sfizz** (SFZ) instruments plus a shared LV2 effects chain, then select the active instrument instantly from a master keyboard (e.g. Studiologic SL88) without reloading samples.

## How it works

```
┌─────────────────────────────────────────────────────────────────────────┐
│  MIDI in (system:midi_capture_1)                                        │
│       │                                                                 │
│       ├──► sfizz / Fluida instances 10–22  (one active, rest bypassed) │
│       │         │                                                       │
│       │         └──► shared stereo FX bus ──► system:playback           │
│       │                                                                 │
│       └──► modrouter listens for Program Change                         │
│                 └──► mod-host bypass commands                           │
└─────────────────────────────────────────────────────────────────────────┘
```

1. **mod-host** runs as the LV2 plugin host (TCP control on `127.0.0.1:5555` by default).
2. **`modrouter`** reads a pedalboard JSON, loads every plugin via the mod-host text protocol, applies state/controls/bypass, and wires JACK connections.
3. A configurable JACK MIDI listener maps **Program Change N → plugin instance N** for known sampler instances.
4. Optional per-plugin top-level `gain` is applied to the shared `Gain2x2` output stage when that program is selected.
5. Switching is done with **bypass** — all instruments stay loaded in memory; only the selected one is un-bypassed.
6. On startup, the last active instrument is restored from disk and echoed back to the SL88 so keyboard and host stay in sync.

## Signal chain (`jsons/plus.json`)

`plus.json` is the full production pedalboard: many parallel instruments feeding one shared effects bus.

### Instruments (instance IDs 10–22)

All instances receive MIDI from `system:midi_capture_1`. Audio outputs are summed into the shared bus. Only one instrument is un-bypassed at a time (default: **16 = Rhodes**).

| ID | Symbol | Instrument |
|----|--------|------------|
| 10 | sfizz-k18 | K18 Upright Piano |
| 11 | sfizz-salamander | Salamander Grand Piano |
| 12 | sfizz-cp80 | Yamaha CP80 |
| 13 | sfizz-pianet | Pianet T |
| 14 | sfizz-wurlitzer | Wurlitzer |
| 15 | sfizz-ep200 | Wurlitzer EP200 |
| 16 | sfizz-rhodes | jRhodes3d (default active) |
| 17 | sf2-electricv-le | Electric V LE (SFZ) |
| 18 | sfizz-clav | Clavinet |
| 19 | sfizz-ivy-ambient | Ivy Piano in 162 — Ambient |
| 20 | sfizz-ivy-close | Ivy Piano in 162 — Close |
| 21 | sfizz-spanish-guitar | Spanish Classical Guitar |
| 22 | sfizz-button-accordion | Button Accordion |

Sampler plugins are mostly **sfizz** (`http://sfztools.github.io/sfizz`). SFZ file paths are set per instance via `state` (`sfzfile` patch key). `default.json` also includes a **Fluida** SF2 instance (ID 17) as an alternative clavinet source.

### Shared FX chain (instance IDs 35–44)

All instrument stereo outputs connect into **Calf StereoTools** (35), then through a stereo effects chain:

```
Instruments (10–22)
        │
        ▼
  StereoTools (35)          mode 5, always on — summing / stereo utility
        │
   ┌────┴────┐
   ▼         ▼
 wah L (50)  wah R (51)     Guitarix switchless wah, per-channel (bypassed)
   │         │
   └────┬────┘
        ▼
  Saturator (37)             Calf — bypassed
        ▼
  Para EQ (38)               LSP x16 stereo — bypassed
        ▼
  Compressor (39)            Calf — bypassed
        ▼
  Pulsator (40)              Calf — bypassed
        ▼
  MultiChorus (41)           Calf — on
        ▼
  Reverb (42)                Calf — bypassed
        ▼
  Gain2x2 (43)               MOD gain stage — on
        ▼
  Limiter (44)               LSP stereo — bypassed
        ▼
  system:playback_1/2
```

The wah stage splits left and right after StereoTools so each channel can be processed independently before rejoining at the saturator.

Most FX slots are present in the chain but **bypassed** in the JSON — they can be toggled live via `router-modhost-cmd` without reconfiguring the pedalboard.

### MIDI Program Change mapping

Program Change value **equals the mod-host instance ID**:

- PC **10** → K18 Upright
- PC **16** → Rhodes (matches SL88 patch P017 if 0-based PC)
- PC **22** → Button Accordion

Only instances registered as sampler plugins (sfizz and Fluida URIs) participate in switching. Program changes for unknown IDs are ignored.
If a selected plugin has a top-level `gain` value, that dB value is sent to the `Gain` parameter of the pedalboard's `Gain2x2` plugin, discovered by URI.

**Special:** PC **50** (`KILL_PC` in `modrouter`) triggers `systemctl poweroff` — a foot-switch or panic patch can shut the machine down.

## Pedalboard JSON format

Configs follow MOD pedalboard v2 layout (compatible enough for headless loading):

```json
{
  "version": 2,
  "name": "...",
  "plugins": {
    "16": {
      "uri": "http://sfztools.github.io/sfizz",
      "symbol": "sfizz-rhodes",
      "bypass": false,
      "gain": 0.0,
      "state": {
        "http://sfztools.github.io/sfizz:sfzfile": "/path/to/instrument.sfz"
      },
      "controls": {
        "volume": -10.0
      }
    }
  },
  "connections": [
    { "from": "system:midi_capture_1", "to": "16:control" },
    { "from": "16:out_left", "to": "35:in_l" }
  ]
}
```

- **Plugin IDs** are numeric strings used as mod-host instance IDs and as the Program Change mapping.
- **`bypass`** sets initial bypass state at load time.
- **`gain`** (optional, dB) sets shared output trim for that program selection.
- **`state`** → mod-host `patch_set` (LV2 patch properties, e.g. SFZ file path).
- **`controls`** → mod-host `param_set` (plugin parameters).
- **Connections** use shorthand port names (`16:out_left`); the loader expands these to `effect_16:out_left` for mod-host. `system:*` ports are passed through unchanged.

### Config variants (`jsons/`)

| File | Purpose |
|------|---------|
| `plus.json` | Full rig — all instruments + complete FX chain |
| `default.json` | Core keyboards + FX; includes Fluida SF2 clav option |
| `all.json` | Extended instrument set (subset of plus) |
| `rhodes.json`, `ep.json`, `clav.json`, `ivy.json` | Single-instrument or focused setups |
| `wah.json`, `wah2.json`, `rhodes_wah.json` | Wah / Rhodes experiments |
| `test.json` | Minimal test pedalboard |

## Scripts

### `modrouter` — main loader / router

**Requires:** running mod-host, JACK, and Python packages `jack` (python-jack-client), `mido`.

The repo is also installable as a Python package:

```bash
python -m pip install -e .
modrouter jsons/plus.json
```

The packaged implementation lives under `src/router_loader/`, with console
scripts for the main loader and helper tools:

| Command | Entry point |
|---------|-------------|
| `modrouter` | `router_loader.modrouter:main` |
| `router-calibrate-gain` | `router_loader.calibrate_gain:main` |
| `router-modhost-cmd` | `router_loader.modhost_cmd:main` |

#### Runtime MIDI configuration

Program Change input, mapped CC input, and outbound controller setup are independent:

- **Program Change input** selects the active plugin. On the Pi this normally comes from the SL88.
- **CC input** applies plugin `midi_cc` mappings. On the Pi this normally comes from the Launch Control XL3; on Windows it can come from the MiniLab 3.
- **Controller messages** cover outbound SL88 synchronization and LCXL setup/SysEx. They can be disabled for input-only controllers.

Command-line options override environment variables, which override the built-in Raspberry Pi defaults.

| Command-line option | Environment variable | Default | Description |
|---------------------|----------------------|---------|-------------|
| `--program-source PORT` | `PROGRAM_TARGET_PORT` | `system:midi_capture_1` | JACK source for Program Change messages |
| `--program-changes` / `--no-program-changes` | `LISTEN_PROGRAM_CHANGES` | enabled | Enable or completely disable Program Change listening |
| `--cc-source PORT` | `CC_TARGET_PORT` | `system:midi_capture_4` | JACK source for mapped CC messages |
| `--cc-channel N` | `CC_CHANNEL` | `2` | MIDI channel for mapped CC messages, 1–16 |
| `--send-controller-messages` / `--no-controller-messages` | `SEND_CONTROLLER_MESSAGES` | enabled | Enable or disable SL88 sync and LCXL setup messages |
| `--no-midi-connect` | — | disabled | Disable all automatic MIDI wiring and leave the loader idle |

Both source options accept either a full JACK port name or a case-insensitive JACK alias substring:

```bash
--program-source system:midi_capture_1
--program-source "@alias:SL88"

--cc-source system:midi_capture_4
--cc-source "@alias:Launch Control XL3"
```

Use `jack_lsp -A` to inspect aliases. Choose alias text that uniquely identifies the desired MIDI source port.

Explicit Raspberry Pi configuration:

```bash
modrouter \
  --program-changes \
  --program-source "@alias:SL88" \
  --cc-source "@alias:Launch Control XL3" \
  --cc-channel 2 \
  --send-controller-messages \
  jsons/plus.json
```

This is equivalent to the normal Pi defaults, except that aliases are used instead of fixed JACK port names.

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `MOD_HOST` | `127.0.0.1` | mod-host TCP address |
| `MOD_PORT` | `5555` | mod-host TCP port |
| `MOD_TIMEOUT` | `5.0` | Socket timeout (seconds) |
| `ROUTER_STATE` | `/var/lib/router/last_state.json` | Persisted last active instrument |
| `PROGRAM_TARGET_PORT` | `system:midi_capture_1` | JACK source for Program Change messages; accepts `@alias:text` |
| `LISTEN_PROGRAM_CHANGES` | `1` | Enable Program Change input; set to `0` to disable |
| `CC_TARGET_PORT` | `system:midi_capture_4` | JACK source for mapped CC messages; accepts `@alias:text` |
| `CC_CHANNEL` | `2` | MIDI channel for mapped CC messages (1–16) |
| `SEND_CONTROLLER_MESSAGES` | `1` | Send SL88 sync and LCXL setup; set to `0` to disable |

On exit (Ctrl+C / SIGTERM), the loader disconnects ports and removes loaded plugins from mod-host.

**SL88 sync:** After load, if an active piano is known, the loader sends a one-shot Program Change on **MIDI channel 2** (`COMMON_CHANNEL`) to `system:midi_playback_1` so the keyboard display matches the host.

### `router-modhost-cmd` — ad-hoc mod-host control

```bash
router-modhost-cmd list
router-modhost-cmd "bypass 42 0"      # enable reverb
router-modhost-cmd "param_set 43 Gain -3.0"
```

### Launch Control XL3

Plugins with an `lcxl` section in the pedalboard JSON get a custom-mode SysEx upload on program change (fader/encoder CC maps and labels). Pedalboards without any `lcxl` sections do not connect the LCXL output or send controller setup messages. See **[docs/lcxl3-sysex.md](docs/lcxl3-sysex.md)** for the reverse-engineered format, common wrong assumptions, and test commands.

```bash
python test/lcxl3_setup_plugin.py 26 --listen 150   # Caveman (SFZ pass-through)
python test/lcxl3_setup_plugin.py 28 --listen 150   # Connie (CC → LV2 params)
```

### `test/` — development utilities

Shell scripts (`jalv_chain*.sh`, `vanilla.sh`, `teardown.sh`) exercise individual LV2 chains with `jalv`. Python helpers send test Program Changes (`pc.py`, `pc2.py`), probe MIDI (`listen3.py`), or run earlier loader iterations (`load.py`).

## Typical startup

1. Start JACK.
2. Start mod-host (listening on port 5555).
3. Run the loader:

   ```bash
   modrouter jsons/plus.json
   ```

4. Play — use Program Change on the master keyboard to switch instruments. The loader debounces repeated PCs and saves the selection to `ROUTER_STATE`.

## Windows UCRT64 startup

Run this workflow from an **MSYS2 UCRT64** shell so JACK, mod-host, LV2 plugins, and Python all resolve from the same environment.

Use a locally patched JACK build. If you want it installed system-wide in the UCRT64 prefix, deploy the patch through the local `MINGW-packages/mingw-w64-jack2` package and install that package with `pacman`; otherwise make sure the patched `jackd.exe` and JACK DLLs are first on `PATH`.

Start JACK with the Windows WinMME MIDI backend and PortAudio audio backend:

```bash
jackd -S -X winmme -d portaudio -r 48000 -p 512
```

In another UCRT64 shell, add your user LV2 bundle directory to the LV2 search path, then start the local `mod-host.exe` without auto-connecting ports:

```bash
export LV2_PATH="$HOME/.lv2${LV2_PATH:+:$LV2_PATH}"
mod-host -n
```

In a third UCRT64 shell, set up Python from the router checkout. Use MSYS2's packaged `cffi` and `packaging`; pip currently tries to compile `cffi` on UCRT64/Python 3.14 and can fail. The virtualenv is created with `--system-site-packages` so those UCRT64 packages are visible inside `.venv`.

First-time setup:

```bash
cd /c/Users/chica/git/router
pacman -S --needed mingw-w64-ucrt-x86_64-python-cffi mingw-w64-ucrt-x86_64-python-packaging
python -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install --no-deps JACK-Client mido
```

If `.venv` already exists and was created without `--system-site-packages`, remove it and recreate it with the commands above.

For later runs, activate the venv from UCRT64/bash with:

```bash
source .venv/bin/activate
```

This UCRT64 venv uses `.venv/bin`, not `.venv/Scripts`.

Then load the Windows pedalboard:

```bash
modrouter --skip-state \
  --no-program-changes \
  --cc-source "@alias:Minilab3 MIDI" \
  --cc-channel 1 \
  --no-controller-messages \
  jsons/windows.json
```

This disables Program Change input, listens for MiniLab mapped CC messages on MIDI channel 1, and avoids sending SL88 sync or LCXL setup messages. Ordinary note connections remain defined in `windows.json`.

## Design notes

- **Preload vs switch:** All samplers are instantiated at boot so switching is bypass-only — fast and glitch-free compared to loading SFZs on demand.
- **Parallel MIDI:** Every instrument receives the same MIDI stream; bypass ensures only one produces audio (others are silent but still receive note-off etc. if not fully bypassed at the plugin level — verify sfizz bypass behavior for your use case).
- **Summing bus:** StereoTools acts as the mix point where all instrument outputs meet before FX.
- **Headless MOD JSON:** Layout metadata (`x`, `y`, `width`, `height`, `symbol`) is preserved for compatibility with MOD pedalboard exports but is not used by the loader.

## Dependencies

- [mod-host](https://github.com/moddevices/mod-host) — LV2 host with TCP control interface
- JACK audio/MIDI
- Python 3 with `jack` and `mido`
- LV2 plugins as referenced in your JSON (sfizz, Calf, LSP, Guitarix, MOD Gain2x2, Fluida, etc.)
- SFZ/SF2 sample libraries at the paths configured in your JSON
