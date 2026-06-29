# hpgl-buddy - Design Decisions (Task 1)

Living design document. Records the decisions made for the basic implementation so
we can circle back and revise. Anything marked **OPEN** still needs a decision or
hardware confirmation.

Authoritative reference for all device behavior is the HP **Interfacing and
Programming Manual** (`hp/FFONS49JUMXQZJH`, 230 pp) and the **HP 7475A Short Form**
(`hp/HP_7475A_Short_Form_Feb_89`). Page numbers below cite the Interfacing manual.

---

## 1. Decisions locked (2026-06-18)

| Topic            | Decision                                                              |
|------------------|-----------------------------------------------------------------------|
| Tool name        | `hpgl-buddy` (kept)                                                    |
| Flow control     | `ESC.B` software polling as primary, XON/XOFF enabled as safety net   |
| Device profiles  | Declarative TOML data files + an abstract `Device` base class         |
| Default baud     | 9600 (configurable; matches on-site 7475A DIP setting)                |
| Python           | 3.11+ (floor set by stdlib tomllib; tested on 3.11-3.13)               |
| Interface        | RS-232 only (HP-IB deferred)                                          |
| Output           | ASCII only, no emoji. Logging only, no `print`.                       |
| Delivery         | Wheel (PyPI upload deferred)                                          |

---

## 2. Guiding principles

1. **HP-GL file != execution scenario.** A file is just a list of instructions. A
   separate situational-awareness layer decides whether/how it is safe to send each
   piece, confirms it did not error, and tracks progress. Parsing and execution are
   different modules.
2. **Strict component isolation.** Each layer (HP-GL, device, interface, execution,
   status) depends only on abstractions of its neighbors, so a new device, interface,
   or protocol can be added without touching the others.
3. **Observability over black-box behavior.** Every ESC exchange and chunk boundary is
   logged. At DEBUG we log raw bytes as ASCII + hex so the wire traffic can be
   reconstructed from the log alone.
4. **Prevent, do not pretend to heal.** Once ink is on paper it cannot be undone. Fault
   tolerance means: never overflow the buffer, never let the buffer underrun while the
   pen is down, and on a reported device error abort cleanly with a full state dump.
   See section 7.

---

## 3. The pen-down safety rule (core constraint)

The 7475A buffer is ~1024 bytes. Two distinct failure modes:

- **Overflow** - host sends faster than the plotter drains. Causes lost bytes / garbled
  plot. Prevented by flow control (section 6).
- **Underrun while Pen Down (PD)** - the buffer empties mid-stroke, so an *inked* pen
  sits stationary on the paper and blots. This is the failure the task calls out.

Therefore chunking is not a blind byte-count split. Rules:

- **Never split mid-instruction.** A chunk always ends on an HP-GL instruction
  terminator (`;` or a label terminator), never inside one.
- **Keep pen-down runs fed.** A contiguous pen-down stroke sequence is treated as a
  unit; we prefer to have its bytes buffered ahead so the pen never waits.
- **ESC polling is safe anytime; pen-up checkpoints are for HP-GL queries.** `ESC.`
  device-control instructions (`ESC.B`, `ESC.E`) are processed immediately by the I/O
  processor and are *not* buffered (manual p.174), so they never stall the pen and are
  used for live monitoring during a stroke. The *buffered* HP-GL queries (`OS`, `OE`)
  execute in sequence and only answer after preceding graphics drain, so those are
  issued at pen-up sync points.

The real pen-down rule is therefore simply: **keep the buffer fed so it never underruns
mid-stroke.** Monitoring it does not cost a pause.

---

## 4. Architecture / pipeline

```
HP-GL bytes
   |  hpgl.parser
   v
Program  (ordered list of Instruction objects)        <- pure data, no device
   |  hpgl.syntax_check  (offline validation)
   v
execution.planner                                      <- splits into Chunks at
   |                                                       pen-up / instruction
   v                                                       boundaries (section 3)
Chunk queue + ProgressState
   |  execution.executor  (drives flow_control + status)
   v
interface.Transport  (serial_rs232)  <-> HP 7475A
```

Status/ESC exchanges (`status.escape`, `status.status_codes`) are used by the executor
for situational awareness and by the standalone ad-hoc healthcheck command.

---

## 5. Package layout (src layout, wheel-friendly)

```
src/hpgl_buddy/
  __init__.py            Public API surface: re-exports the supported library names
                         (get_device, parse_hpgl, plot_program, ...). See INTEGRATION.md.
  cli.py                 CLI entry (subcommands; argparse, stdlib only - no runtime deps
                         beyond pyserial). See section 11.
  logging_setup.py       Central logging config; ascii+hex helpers for DEBUG.
  errors.py              Exception hierarchy (section 10).

  hpgl/
    instruction.py       Instruction dataclass (mnemonic, params, raw_bytes, pen_state).
    tokens.py            Mnemonic table + parameter arity/type metadata.
    parser.py            bytes -> Program.
    syntax_check.py      Structural validation, no device needed.

  devices/
    base.py              Abstract Device + Capabilities (buffer size, limits, quirks).
    registry.py          Discovers and loads TOML profiles by model id.
    profiles/
      hp7475a.toml       Declarative 7475A profile.

  interface/
    base.py              Abstract Transport (open/close/write/read/flush, timeouts).
    serial_rs232.py      pyserial implementation.

  execution/
    planner.py           Program -> Chunk list (pen-up boundary aware).
    flow_control.py      ESC.B polling + XON/XOFF policy.
    executor.py          Sends chunks, confirms no error, tracks progress.
    run.py               plot_program(): orchestration entry (chunk sizing + planning +
                         flow/executor wiring) shared by the CLI and library callers.
    progress.py          ProgressState / queue + run statistics for the report.

  status/
    escape.py            ESC command builders + response parsers (ESC.B, ESC.@, ...).
    status_codes.py      OS status byte + OE error code interpretation.
    adhoc.py             Healthcheck (ESC.E/L/B/O, OI/OS/OE/OA/OH) + ready_to_plot.

  demo/
    generator.py         Builds demo HP-GL for a requested pen count (section 12).
```

---

## 6. Interface + flow control

- **Transport abstraction.** `interface/base.py` defines the contract; everything above
  it is interface-agnostic, so HP-IB can be added later as another Transport.
- **Serial defaults:** 9600 baud, 8 data bits, no parity, 1 stop bit (8N1) -
  configurable. macOS host -> expect a USB-serial adapter device path
  (`/dev/tty.usbserial-*`).
- **Primary handshake - software checking via `ESC.B`** (manual p.159, p.168). `ESC.B`
  returns a decimal 0-255 = bytes of buffer space currently free. The executor polls it
  and only releases the next chunk when `free_bytes >= chunk_size` (with a configurable
  safety margin). This is the most portable and most *loggable* method, matching the
  observability goal.
- **Safety net - XON/XOFF** (manual p.162): available but **OFF by default**.
  Bench finding (2026-06-18): with pyserial XON/XOFF enabled on the on-site USB-serial
  adapter (0557:2008), status responses were swallowed/garbled (missing replies and
  high-bit byte corruption), while plain 9600 8N1 with no flow control exchanged cleanly
  (confirmed via miniterm and a clean OE/OH round-trip). So ESC.B polling is the sole
  primary mechanism; XON/XOFF is opt-in via `--xonxoff` and RTS/CTS via `--rtscts`.
- **Two independent error channels** (both watched during a plot):
  - `ESC.E` (immediate, manual p.169) - RS-232 / I/O errors: `0` none, `10` overlapping
    output request, `11`/`12` bad device-control byte, `13` param out of range, `14` too
    many params, `15` framing/parity/overrun, `16` input buffer overflow (data lost).
    Polled live; clears the front-panel ERROR light when read.
  - `OE` (buffered) - HP-GL instruction errors (unknown mnemonic, bad params, etc.).
    Read at pen-up checkpoints; attributable to the just-sent chunk, not stalling.
- `ESC.K` aborts graphics and discards the buffer; `ESC.J` aborts a partial device
  -control instruction; `ESC.L` reports buffer size. Used for clean abort / re-init.
- Relevant tuning escapes documented in the manual and to be wired through config:
  `ESC.I`/`ESC.H` (block size), `ESC.M` (turnaround delay, trigger/terminator chars),
  `ESC.N` (intercharacter delay, Xoff trigger). RS-232 instruction summary on p.208.

**Resolved on hardware:** `ESC.B` is used to gate sends (room before writing) and for
progress; completion/error detection is the tailgate `OS;OE;OI;` sync (section 7), not
an `ESC.B` drain threshold.

---

## 7. Execution model + fault tolerance

- `planner` turns the Program into a queue of Chunks honoring section 3 rules.
- **Command supply is gated only by buffer space.** Every write goes out in sub-blocks of
  at most `send_block_bytes` (<= the buffer), each gated by `wait_for_space(block)` which
  polls `ESC.B` until `free_bytes >= block + reserve`, then writes. Nothing about pen state
  - just "is there room?". This single gate keeps the pen fed (a long pen-down run streams
  as the buffer drains, so it never underruns) and never stalls the pen. `send_block_bytes`
  is sized to hold a whole chunk **plus** a prefixed verify tailgate (`chunk_budget + 64`),
  so a tailgate-prefixed chunk is always one block - see the verify note below for why the
  poll between sub-blocks must never fall inside a prefixed payload.
- **Never fill to the exact `ESC.B` boundary - keep a reserve.** Filling the buffer to the
  reported free count overflowed the 7475A on hardware (`ESC.B` said 252 free, a 252-byte
  write reported `ESC.E=16` buffer overflow; the same size at 256 free survived on 4 bytes
  of slack). The manual warns to "leave room for the overshoot" and the plotter's own XON
  threshold is 128 bytes (p.162), so `wait_for_space` keeps `--buffer-reserve` bytes free
  (default 128): it waits for `free >= block + reserve`, so used never exceeds
  `capacity - reserve`. This caps the *top* of the buffer only; the pen is fed from the
  bottom (never empty), so the reserve costs ~12% of look-ahead and no throughput that
  matters.
- **The buffer wait is stall-timed, not wall-clock-timed.** A dense or slow plot is
  pen-speed-bound: the buffer can sit near-full and drain a byte at a time for minutes
  (observed on hardware), so an absolute timeout false-aborts a healthy plot. Instead
  `wait_for_space` (and the end-of-run drain) abort only when `ESC.B` shows **no change**
  for `--buffer-stall-timeout` seconds - the clock resets on every change, so a
  slow-but-drawing plot is never flagged. Both waits share one primitive
  (`FlowController._poll_buffer`) that owns the poll loop, an INFO progress heartbeat,
  the stall clock, and `cancel`. When the buffer does go flat, `ESC.O` classifies it: a
  **VIEW** pause (16/24) is the operator deliberately suspending graphics, so it is *not*
  a stall (keep waiting); a **raised paper lever** (32/40) is named in the abort;
  **processing** (0/8) is genuinely ambiguous - the 7475A has no "pen moving" bit
  (manual p.181), so a long single stroke holding the buffer flat past the timeout still
  aborts. That residual case is a hardware limit, mitigated by the configurable timeout.
- **Oversized instructions (> buffer) are streamed, not split.** An instruction larger
  than a whole chunk is emitted as one `oversized` chunk; the sub-block sender above feeds
  its raw bytes in `ESC.B`-gated pieces. The plotter parses the byte stream incrementally
  and reassembles partial numbers across sub-block boundaries, so the instruction is never
  split as HP-GL - that is how a single huge `PD` polyline (common in vector exports)
  plots. In a live verify mode, whenever a tailgate-prefixed payload would not fit one send
  block (`len(tailgate) + chunk > send_block_bytes` - any oversized chunk, and any chunk too
  near the budget once the prefix is added) the pending verdict is read *standalone before*
  the chunk instead of prefixed. Otherwise `_send_raw` would split the payload and poll
  `ESC.B` between sub-blocks - and that poll collides with the prefix tailgate's buffered
  reply, the `ESC.B` read swallowing the `OS` token and desyncing the verdict (an observed
  field hang). The single-block sizing above keeps normal prefixed chunks off this path.
- **Always-on environmental watch (the run-time faults).** After each chunk the executor
  reads two immediate device-control queries - no pen stall:
  - `ESC.E` - RS-232 I/O errors (overflow / framing / data-loss). Handled by the error
    policy; these are the *dangerous* ones (lost bytes -> garbled plot).
  - `ESC.O` - extended status (manual p.181): paper lever / pinch wheels raised (32/40)
    aborts the run (plot physically compromised); VIEW pressed (16/24) is a warning.
  These cover the random, environmental faults, which (per the priority split) matter
  more than syntax: HP-GL errors are a property of the file, environmental ones are not.
- **Optional live HP-GL verification (`--live-hpgl-verify off|chunk|pu`).** HP-GL/syntax
  errors (`OE`) are validated offline already, so on-device `OE` checking is off by
  default. When enabled it uses a **one-deep tailgate**: after a pen-up chunk we remember
  it as `pending` and *prefix* `OS;OE;OI;` to the **next** chunk. That tailgate executes
  right after the previous (pen-up) chunk finishes - while the next chunk is already
  buffered and drawing - so its verdict (the previous chunk's `OE`) comes back without
  stalling the pen. We thus know chunk N-1 is clean before committing N+1; exposure is
  exactly the one chunk currently drawing. `chunk` mode checkpoints at pen-up chunk
  boundaries; `pu` mode makes the planner break a chunk at every `PU` (more, smaller
  chunks) to checkpoint each stroke. A trailing tailgate collects the last verdict and
  confirms completion in all modes.
- **Tailgate read robustness** (validated 2026-06-18): the three CR-delimited replies are
  accumulated against one generous deadline (default 90 s), not three independent
  timeouts - a slow first reply (slow carousel change) must never shift the others into
  the wrong slot (the pen-not-parked regression). The `OI` -> `7475A` tag is the
  completion sentinel and the resync anchor; an `OE` value outside the documented 0-8
  range is treated as a response desync (warn, do not abort).
- **Pen presence is not detectable on the 7475A.** No pen sensor: a missing/fallen pen
  plots dry with no error (manual p.119), and `SP` only errors on an out-of-range
  parameter, not an empty stall (p.43). Captured as `pen_sensing = false` in the profile;
  `plot` warns to pre-load the pens the file uses. Bigger HP plotters can sense pens, so
  it is a per-profile capability.
- **Error policy (`--on-error`), configurable:**
  - `abort` (default, safest): stop feeding, raise a domain exception, log the failing
    chunk, recent ESC traffic, and `ESC.O`/position. Park the pen up (`PU`) so no blot.
  - `prompt`: on a caught HP-GL error, acknowledge it, present the offending command(s)
    with provenance, and let the operator choose continue / skip / abort interactively.
  - `continue`: auto-recover and carry on (the user's "ignore fixable errors" case).
- **Recovery sequence (`prompt`/`continue`):** caught error -> log raw command, its
  sequence index in the file, and source line number -> `ESC.K` (discard buffer, stop
  the runaway) -> `IN` (reinitialize state) -> **replay the tracked state preamble** ->
  resume from the next safe instruction.
- **Provenance is mandatory.** The parser records, per `Instruction`, its file sequence
  index and source line number so any error names the exact command. Because `OE` is
  buffered, attribution is at chunk granularity (pen-up boundary): we log every command
  in the offending chunk as a candidate.
- **State preamble tracking.** `IN` resets pen / scaling (`SC`,`IP`) / rotation (`RO`) /
  pen select (`SP`) / position (`PA`,`PU`). The executor keeps a running snapshot of
  these state-setting instructions so recovery can re-establish context; without replay,
  geometry after `IN` would be misplaced. (Task-1 scope: replay the tracked subset; log
  loudly if a command we cannot model preceded the failure.)
- **Progress + report:** ProgressState yields instructions sent / remaining, chunk
  count, elapsed time, ESC exchange log, recovered errors, warnings, and a `cancelled`
  flag - emitted as the end-of-run report (README "Report" section). It is mutated in
  place (poll it), and `run`/`plot_program` also accept an optional `progress_callback`
  pushed after each chunk and at the terminal state - an observer whose exceptions are
  swallowed so it can never disrupt the run.
- **Cooperative cancellation.** `Executor.run` / `plot_program` accept an optional
  `threading.Event`; when set from another thread (e.g. a GUI Stop button) the run halts
  at the next chunk boundary or during the drain wait, issues `ESC.K` + `PU` (discard
  buffer, park pen), sets `ProgressState.cancelled`, and returns the partial progress
  rather than raising - cancellation is a normal outcome. The event is read only by the
  running thread, so the transport stays single-owner. `cancel=None` is the unchanged
  default and keeps CLI behavior identical.

**OPEN:** exact contents of the replayed state preamble (which mnemonics to track);
start with `SC`/`IP`/`RO`/`SP`/`IW`/last pen position, expand as real files demand.

---

## 8. HP-GL handling + syntax check

- **Parser** tokenizes into `Instruction` objects (mnemonic, raw params, raw bytes,
  derived pen state) and tags each as pen-up / pen-down / neutral for the planner.
- **Syntax check scope (basic, per task):** recognized mnemonic; parameter count and
  numeric type plausibility; balanced label delimiters (`LB ... <ETX>`); terminator
  presence. It does **not** geometrically simulate the plot. Runs fully offline
  (`check` subcommand) so files can be validated without a plotter.

**OPEN:** how strict to be on unknown mnemonics - reject vs. warn-and-pass-through.
Lean warn-and-pass so vendor extensions are not blocked.

---

## 9. Device profiles (TOML + base class)

`Device` base class holds *behavior*; the TOML file holds *facts* about a model. A
contributor adds a simple device by dropping a `.toml` in `profiles/`; unusual behavior
can still subclass `Device`.

Example `profiles/hp7475a.toml` (fields illustrative, to be confirmed from manual):

```toml
model = "7475A"
vendor = "HP"
buffer_bytes = 1024
interfaces = ["rs232"]
pen_count = 6
[limits]
hard_clip = "device-reported"   # query via status escapes, not hard-coded
[serial_defaults]
baud = 9600
framing = "8N1"
[capabilities]
buffer_query = "ESC.B"
status = "OS"
error = "OE"
monitor_mode = "ESC.@"
```

---

## 10. Errors + logging

- **Logging:** stdlib `logging`, no `print` anywhere. Levels: INFO = user-facing
  progress; DEBUG = raw wire bytes as **ASCII + hex** plus every ESC exchange; WARNING /
  ERROR per section 7. Module name in every record so failures are traceable to a layer.
- **Exception hierarchy** (`errors.py`): `HpglBuddyError` base; `HpglSyntaxError`,
  `TransportError`, `DeviceError` (carries `OE` code + meaning), `BufferPolicyError`,
  `ProtocolError`. Every raised error states: what happened, which module, during which
  activity, and the cause. Unhandled exceptions log a full stacktrace.
- **Naming:** full descriptive identifiers (e.g. `available_buffer_bytes`, not `bs`).

---

## 11. CLI surface

```
hpgl-buddy status                 Ad-hoc healthcheck: identify, OS, OE, ESC.B; prints
                                  interpreted values (section follows status_codes).
hpgl-buddy check  FILE            Offline syntax check, no device.
hpgl-buddy plot   FILE [--port ... --baud 9600 ...]   Safe plot with full progress log.
hpgl-buddy monitor (enable|disable|watch)   enable/disable monitor mode on the computer
                                  port (--mode received|parsed), or watch the echo on the
                                  terminal port. ESC.@ bits per manual p.168.
hpgl-buddy demo   [--scene card|house] [--pens N] [--out FILE]   Demo generator (section 12).
# raw command execution: deferred (task says "not sure we need it right away").
```

CLI uses stdlib `argparse` subcommands - no extra runtime dependency beyond `pyserial`.

**OPEN:** confirm argparse (zero deps) vs. typer (nicer help, adds a dep). Defaulting to
argparse for a hardware tool that should stay lean.

---

## 12. Demo generator

Generates demo HP-GL for 1, 2, and up to 6 pens (user may have fewer installed; `--pens`
caps usage). Per task:

- **Shapes:** circle, square, rectangle, triangle, notches. Each regular shape <= ~4x4 cm.
- **Fill:** several fill styles (`FT`) across shapes.
- **Labels:** a plain label, and a label-between-lines style.
- **Colours:** a multi-colour series with pen switches mid-plot to show registration
  accuracy across pen changes.
- **Scaling:** 7475A uses ~40 plotter units/mm; 4 cm ~= 1600 units. Generator emits
  conservative coordinates well inside hard-clip limits.

**OPEN:** label-between-lines is likely centered/justified label placement (`CP`/`LO`);
confirm intended visual from the manual's labeling section before finalizing.

---

## 13. Alternatives evaluated

- **Chiplotle3** (PyPI) - full pen-plotter framework with serial comms and HP-GL
  handshake handling. Good reference for handshake/state logic, but heavyweight and
  older-style for our "lean, well-logged, contributor-friendly" goal. Use as a reference,
  not a dependency.
- **python-hpgl** (alexforencich) - an HP-GL *parser/generator* only; no device comms or
  buffer/flow control, which is the hard and safety-critical part here.

Conclusion: neither covers the safe-feeding + situational-awareness core we need, so we
build it, optionally borrowing parser ideas. (These summaries to be verified hands-on
before relying on them.)

---

## 14. Resolved (Option A defaults, 2026-06-18)

- Unknown mnemonic: **warn and pass through** (don't block vendor extensions). (§8)
- CLI: **argparse**, stdlib only, `pyserial` the sole runtime dep. (§11)
- Abort: **park pen up (`PU`)** to avoid a blot. (§7)
- Error policy modes added: `abort` (default) / `prompt` / `continue`. (§7)

## 15. Hardware available for testing

- 2x USB-serial adapters + a Y cable on the plotter -> can drive plotting on one port
  while reading **monitor-mode** echo (`ESC.@`) on the second. Transport layer should
  therefore allow an optional secondary read-only monitor port. Real `plot` and
  `status`/`monitor` smoke tests are possible on hardware.

## 16. Implementation status

- Milestone 1 (skeleton + profile + `check`/`status`): done.
- Milestone 2 (execution layer + demo + monitor): done.
  - `execution/`: planner (size-capped chunks tagged at pen-up boundaries),
    flow_control (ESC.B wait loop + ESC.E/OE reads), executor (feed loop, live
    ESC.E check, pen-up OE check, abort/prompt/continue policy with ESC.K + IN +
    state-preamble replay), progress/report.
  - `demo/`: shapes (circle/square/rectangle/triangle/notches), fills (FT
    variants), labels (plain + between-lines), multi-colour pen-cycling series;
    `--pens` clamped 1-6. Output passes the offline syntax check.
  - `monitor`: `monitor watch` streams the device echo and logs every byte as
    binary + hex + decimal + ASCII/control-name (one row per symbol).
- Milestone 3 (library-integration prep): in progress.
  - `execution/run.py` `plot_program()` centralizes the plot orchestration (chunk
    sizing, planning, flow/executor wiring), shared by the CLI and external callers
    (issue #9); the top-level package re-exports the supported API; cooperative
    cancellation via a `threading.Event`. See TASK-2 and INTEGRATION.md.

## 17. Still open

- ESC.B safety-margin bytes (§6): **resolved** - a 128-byte reserve (`--buffer-reserve`)
  prevents the at-boundary overflow seen on hardware; tune per device if needed.
- State-preamble replay is currently optimistic (folds in all sent state
  instructions); confirm the tracked mnemonic set and roll-back semantics
  against real files (§7).
- Validate demo registration and "between-lines" placement on paper.

## Appendix A. Verified command reference (from `hp/FFONS49JUMXQZJH`)

Immediate (device-control, not buffered - safe to poll mid-plot):
- `ESC.B` -> free buffer bytes, 0-255. (p.168)
- `ESC.E` -> RS-232 I/O error 0 / 10-16; clears ERROR light. (p.169)
- `ESC.O` -> extended status word. (p.180-181)
- `ESC.L` -> total buffer size in bytes. (p.174)
- `ESC.J` abort device-control instruction; `ESC.K` abort graphics + discard buffer. (p.174)
- `ESC.@` set config / monitor mode (bit2: mode0=parsed, mode1=received). (p.168)
- `ESC.I`/`ESC.H` block size; `ESC.M`/`ESC.N` delays, trigger/terminator chars. (p.159-162)

Buffered (HP-GL, execute in sequence):
- `OS` output status, `OE` output (HP-GL) error, `OA`/`OC` position, `OI` identify,
  `OH`/`OP` limits, `OF` factors. (read exact bit meanings from manual when coding)
