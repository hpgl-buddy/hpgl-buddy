# hpgl-buddy

[![PyPI version](https://img.shields.io/pypi/v/hpgl-buddy.svg)](https://pypi.org/project/hpgl-buddy/)
[![Python versions](https://img.shields.io/pypi/pyversions/hpgl-buddy.svg)](https://pypi.org/project/hpgl-buddy/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/hpgl-buddy/hpgl-buddy/blob/master/LICENSE)

Carefree, observable plotting of HP-GL files on HP pen plotters over RS-232.

It does not just shove a file at the plotter: it validates the file, splits it to fit
the device buffer, feeds it so the buffer never overflows and an inked pen never stalls
mid-stroke, watches the device for faults the whole time, and logs every exchange so a
run can be understood and troubleshooted from the log alone.

**Supported devices:** 

* HP 7475A (RS-232). New devices are added as declarative profiles
(see [Extending](#extending)); HP-IB is planned.

---

## Install

```bash
pip install hpgl-buddy
```

Requires Python 3.13 and a USB-serial adapter (on macOS use the `/dev/cu.*` device).
`pyserial` is the only runtime dependency. To work on hpgl-buddy itself, see
[Development](#development).

---

## Quick start

```bash
# 1. Validate a file offline (no plotter needed)
hpgl-buddy check drawing.hpgl

# 2. Check the plotter is alive and interpret its status
hpgl-buddy status --port /dev/cu.usbserial-XXXX

# 3. Plot it (verbose shows every byte/ESC exchange)
hpgl-buddy -v plot drawing.hpgl --port /dev/cu.usbserial-XXXX

# 4. Generate and plot a built-in demo
hpgl-buddy demo --pens 6 --out demo.hpgl
hpgl-buddy plot demo.hpgl --port /dev/cu.usbserial-XXXX
```

Sample HP-GL files live in [`examples/`](https://github.com/hpgl-buddy/hpgl-buddy/tree/master/examples).

Serial defaults: **9600 8N1, no flow control** (configurable). Flow control is off by
default because XON/XOFF corrupted the exchange on the on-site adapter; enable it with
`--xonxoff` if your cabling needs it.

---

## Commands

| Command | What it does |
|---|---|
| `check FILE` | Offline HP-GL syntax check. No device. Exit non-zero on errors. |
| `status` | Ad-hoc healthcheck: identification, buffer, status byte, errors, limits - all interpreted. |
| `plot FILE` | Safe, buffer-aware plotting with progress + end-of-run report. |
| `monitor enable\|disable\|watch` | Enable/disable monitor mode (computer port) or stream the echoed bytes (terminal port). |
| `demo` | Generate demo HP-GL: `--scene card` (shapes/fills/labels/colours grid) or `--scene house` (a one-line drawing emitted as a single giant `PD` instruction - the >1024-byte oversized-instruction case, streamed in sub-blocks). |

Global: `-v/--verbose` (DEBUG, incl. raw ASCII+hex wire dumps), `--version`.

Serial options (on `status`/`plot`/`monitor`): `--port`, `--model` (default `hp7475a`),
`--baud`, `--framing` (e.g. `8N1`), `--timeout`, `--xonxoff`, `--rtscts`.

### plot options

- `--on-error abort|prompt|continue` - on a reported error: stop and park the pen
  (default), ask interactively, or auto-recover (`ESC.K` discard -> `IN` -> replay the
  tracked state preamble -> carry on).
- `--live-hpgl-verify off|chunk|pu` - optional on-device HP-GL error checking (see
  [Verification](#verification)). Default `off`.
- `--ignore-syntax-errors` - plot even if the offline check found errors.
- `--stats-json PATH` - write run statistics as JSON (`-` for stdout).

### monitor (two ports)

The 7475A enables monitor mode via an ESC sequence on the **computer** (data) port, then
echoes the bytes it receives out a separate **terminal** port - so it's two single-purpose
commands, one per port:

```bash
hpgl-buddy monitor enable  --port /dev/cu.COMPUTER   # turn monitor mode on (data port)
hpgl-buddy monitor watch   --port /dev/cu.TERMINAL   # stream the echo (terminal port)
hpgl-buddy monitor disable --port /dev/cu.COMPUTER   # turn it off when done
```

There are two monitor modes (`enable --mode received|parsed`): **`received`** (default)
echoes every byte as it arrives, including ESC device-control sequences; **`parsed`**
echoes only HP-GL as the plotter parses it from the buffer. `watch` prints every byte as
binary, hex, decimal, and ASCII/control-name.

---

## How plotting works

```
file -> parse -> offline syntax check -> plan into chunks -> stream to device -> report
```

1. **Parse** the bytes into instructions, each tagged with its source line and sequence
   index (so any error names the exact command).
2. **Syntax check** offline; `plot` refuses a file with errors unless `--ignore-syntax-errors`.
3. **Plan** into chunks of <=256 bytes, split only at instruction boundaries (never inside
   a command), each tagged whether it ends with the pen up.
4. **Stream**: a chunk is sent only when `ESC.B` reports enough free buffer space. This one
   gate both prevents overflow and keeps the buffer fed, so a long pen-down stroke spanning
   several chunks never underruns (an underrun would park an inked pen and blot). An
   instruction *larger than the buffer* (e.g. a single huge `PD` polyline) is streamed
   across several `ESC.B`-gated sub-blocks - never split as HP-GL, since the plotter
   reassembles the byte stream as it parses - so it plots like any other.
5. **Watch** (always on, after each chunk): `ESC.E` for I/O faults (overflow / framing /
   data loss) and `ESC.O` for environmental faults (paper lever or pinch wheels raised ->
   abort; VIEW pressed -> warn). Both are immediate and never stall the pen.
6. **Confirm**: a final `OS;OE;OI;` tailgate waits for the pen to physically finish and
   reports the end status / any HP-GL error.
7. **Report** instructions/chunks/bytes sent, elapsed time, recovered errors, and warnings
   (and the same as JSON via `--stats-json`).

### Verification

HP-GL/syntax errors are a property of the *file* (already validated offline), so on-device
HP-GL error checking is **optional** and off by default. When enabled it never stalls the
pen - it uses a *one-deep* tailgate: the `OS;OE;OI;` query is prefixed to the chunk after a
pen-up, so its verdict reports the *previous* chunk while the current one is already drawing.

- `chunk` - check at pen-up chunk boundaries.
- `pu` - break a chunk at every pen-up so each completed stroke is checked (more, smaller
  chunks).

Either way you learn a chunk is clean within ~1 chunk, and a reported error names the span
of chunks it could belong to with all candidate instructions.

### Limits to know

- **No pen sensing on the 7475A**: a missing or fallen pen plots dry and is *not* detectable
  by any status query. Pre-load the pens your file uses. (`plot` warns about this.)
- **Throughput is bound by the baud rate**: a huge pen-down instruction with very short
  segments can outrun a 9600-baud link no matter how we feed it (the pen draws faster than
  bytes arrive). hpgl-buddy always feeds as fast as the buffer allows; raise the baud if a
  dense drawing dwells.

---

## Architecture

Thoroughly separated layers, each replaceable on its own:

```
hpgl/       parse HP-GL bytes into a Program; offline syntax check
devices/    declarative TOML profiles + abstract Device base (registry)
interface/  Transport abstraction + pyserial RS-232 implementation
status/     ESC + HP-GL command builders, response parsers, status interpretation, monitor
execution/  planner (Program -> chunks) + flow control + executor + progress/report
demo/       demo HP-GL generators
```

The full design rationale, with HP manual citations, lives in
[`DESIGN.md`](https://github.com/hpgl-buddy/hpgl-buddy/blob/master/DESIGN.md).

### Extending

Add a simple device by dropping a `<model>.toml` into `devices/profiles/` (buffer size,
pen count, serial defaults, capabilities, `pen_sensing`). A device with unusual behavior
can subclass `Device` and register it. The interface layer is transport-agnostic, so HP-IB
can be added as another `Transport` without touching the rest.

---

## Development

```bash
pip install -e . -r requirements-dev.txt   # editable install + pinned dev tooling
tox                                         # run the test suite (Python 3.13)
tox -e build                                # build the sdist + wheel
```

Dependencies follow a loose-*rough* -> pinned pattern, frozen in clean environments by the
`dependencies_update` GitHub Actions workflow:

- **runtime:** `requirements-rough.txt` (hand-maintained, loose; kept in sync with
  `[project.dependencies]`) -> **`requirements.txt`** (fully pinned).
- **dev / CI tooling:** `requirements-dev-rough.txt` (loose: pytest, tox, build, twine) ->
  **`requirements-dev.txt`** (fully pinned), needed only in CI and dev environments.

A `Dockerfile` provides a reproducible build/test image (not for serial I/O - hardware is
not reachable from a container).

Conventions: extensive logging (no `print`), ASCII-only output, descriptive names, and
errors that state what happened, where, and why.
