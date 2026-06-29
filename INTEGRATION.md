# Integrating hpgl-buddy into your software

This guide is for embedding hpgl-buddy as a **library** (e.g. a PyQt GUI), not the
CLI. It covers the public API, the current constraints you must design around, and
copy-paste Python examples for every supported operation.

> Status: the library covers the full planned GUI scope (device list, status, check,
> demo, plot - with cancellation and live progress). The
> [Known conditions](#known-conditions--caveats) below are deliberate design
> constraints, not missing features. This document tracks what is true *now*.

## Public API at a glance

Everything is re-exported from the top-level `hpgl_buddy` package - import from
there rather than the deeper module paths:

```python
from hpgl_buddy import (
    available_models, get_device,        # device profiles
    parse_hpgl, check_program,           # parse + offline check
    SerialTransport,                     # RS-232 byte pipe
    run_healthcheck, HealthReport,       # status retrieval
    generate_demo, generate_scene,       # demo HP-GL bytes
    plot_program, ProgressState,         # plotting + progress
    ErrorPolicy, VerifyMode,             # plotting options
    HpglBuddyError, TransportError, DeviceError,  # errors
)
```

Everything you need for the planned GUI (device list, status, check, demo, plot)
is in that import. Monitor modes are intentionally out of scope.

## Known conditions & caveats

Design the UI around these - they are intentional constraints of a local,
serial-port, single-device tool.

1. **Serial ports are not enumerated for you.** The library only *consumes* a port
   string. Discover ports in the UI, per platform, with pyserial:
   `serial.tools.list_ports.comports()`. Then pass the chosen device path to
   `SerialTransport`. (This is deliberate - port discovery is platform-specific.)
2. **Stop a plot with a `threading.Event`, not by closing the port.** Pass a
   `cancel=threading.Event()` to `plot_program`; set it from the UI thread to stop
   at the next chunk boundary (or during the final drain). The buffer is discarded
   (ESC.K), the pen parked (PU), and the call returns with `progress.cancelled`
   set - cancellation is a normal outcome, not an exception. Only the worker reads
   the event, so the transport stays single-owner. Do **not** close the transport
   from another thread to force a stop - that raises mid-write and leaves the pen
   down and the buffer in an unknown state. (Cancellation latency is at most one
   in-flight chunk, ~256 bytes.)
3. **All calls block.** Run anything that touches the port (status, plot) on a
   worker thread; never on the UI thread.
4. **Progress: poll or push.** Pass your own `ProgressState` into `plot_program` and
   either poll it from the UI thread (e.g. a `QTimer`) - the same instance is mutated
   in place - or pass a `progress_callback` to be called after each chunk and at the
   end. The callback runs on the **worker thread**, so marshal to the UI (e.g. emit a
   Qt signal); it is an observer only, and any exception it raises is logged and
   swallowed rather than disrupting the plot.
5. **One operation per port at a time.** A `SerialTransport` is single-owner; do not
   issue concurrent operations on the same open port from multiple threads.
6. **You must validate before plotting.** `plot_program` plots the program as-is.
   Run `check_program` yourself and decide whether to proceed (mirror the CLI's
   "refuse on errors unless overridden" if you want that behavior).
7. **pyserial is required at connect time.** It is imported lazily when a port opens;
   opening without it installed raises `TransportError`. Add `pyserial` to your app's
   dependencies.
8. **The library never prints - it logs.** All output goes through the stdlib
   `logging` module under the `hpgl_buddy.*` logger namespace (wire bytes at DEBUG).
   Attach your own handler to surface it in the UI; configure logging yourself.
9. **No pen sensing on the 7475A.** A missing/unloaded pen plots dry and is
   undetectable. The condition is logged when you plot - surface it to the operator.

## Examples

### 1. List supported devices and resolve one

```python
from hpgl_buddy import available_models, get_device

models = available_models()          # e.g. ["hp7475a"] - populate a dropdown
device = get_device("hp7475a")       # raises HpglBuddyError on unknown id
print(device.model, device.buffer_bytes, device.pen_count,
      device.profile.pen_sensing, device.profile.serial_defaults.baud)
```

### 2. Enumerate serial ports (UI side, your responsibility)

```python
from serial.tools import list_ports

ports = [(p.device, p.description) for p in list_ports.comports()]
# e.g. [("/dev/tty.usbserial-1420", "FT232R USB UART"), ...]
```

### 3. Open a connection

```python
from hpgl_buddy import SerialTransport

# Take defaults from the device profile, or let the user override baud/framing.
transport = SerialTransport(
    port="/dev/tty.usbserial-1420",
    baud=device.profile.serial_defaults.baud,        # 9600 for the 7475A
    framing=device.profile.serial_defaults.framing,  # "8N1"
    read_timeout_seconds=2.0,
)
# Use as a context manager, or call transport.open() / transport.close() yourself.
```

### 4. Retrieve device status (healthcheck)

```python
from hpgl_buddy import run_healthcheck

with transport:
    report = run_healthcheck(transport, timeout_seconds=2.0)

# Structured fields - bind straight to widgets (any may be None on no-response):
report.identification        # "7475A"
report.buffer_free_bytes     # int
report.status_byte.active_flags if report.status_byte else []
report.io_error_number       # 0 == OK
report.hpgl_error_number     # 0 == OK
report.extended_status       # ESC.O: .buffer_empty / .view_pressed / .paper_lever_raised
report.ready_to_plot         # bool: no error pending and paper lever down (VIEW is transient)
report.notes                 # list[str] of per-query problems
# report.render() -> ready-made multi-line ASCII summary, if you just want text.
# A GUI can gate its "Plot" button on report.ready_to_plot and surface
# report.extended_status.paper_lever_raised as "load paper / lower the lever".
```

### 5. Offline syntax check

```python
from hpgl_buddy import parse_hpgl, check_program

data = open("drawing.hpgl", "rb").read()
program = parse_hpgl(data, source_name="drawing.hpgl")
findings = check_program(program)
errors = [f for f in findings if f.severity == "error"]
for f in findings:
    print(f.severity, str(f))      # show in a results panel
# Decide whether to plot based on `errors`.
```

### 6. Generate a demo

```python
from hpgl_buddy import generate_demo, generate_scene

card = generate_demo(pen_count=3)  # shapes/fills/labels grid -> bytes
house = generate_scene()           # one large oversized-instruction drawing -> bytes
open("demo.hpgl", "wb").write(card)
```

### 7. Plot on a worker thread with live progress

The core pattern: validate, open the port, run `plot_program` on a thread, poll the
shared `ProgressState` from the UI.

```python
import threading
from hpgl_buddy import (
    get_device, parse_hpgl, check_program, SerialTransport,
    plot_program, ProgressState, ErrorPolicy, VerifyMode,
    HpglBuddyError, DeviceError, TransportError,
)

def run_plot(port, path, on_done):
    device = get_device("hp7475a")
    program = parse_hpgl(open(path, "rb").read(), source_name=path)
    if any(f.severity == "error" for f in check_program(program)):
        on_done(error="syntax errors; aborting"); return

    progress = ProgressState()          # <-- poll THIS from the UI thread
    cancel = threading.Event()          # <-- a Stop button calls cancel.set()
    result = {"progress": progress, "cancel": cancel}

    def worker():
        try:
            with SerialTransport(port, baud=9600, framing="8N1") as transport:
                plot_program(
                    transport, program, device,
                    verify_mode=VerifyMode.OFF,      # OFF | CHUNK | PU
                    error_policy=ErrorPolicy.ABORT,  # ABORT | PROMPT | CONTINUE
                    progress=progress,
                    cancel=cancel,
                )
            on_done(error=None, progress=progress)   # progress.cancelled tells stopped vs done
        except (DeviceError, TransportError, HpglBuddyError) as exc:
            on_done(error=str(exc), progress=progress)

    threading.Thread(target=worker, daemon=True).start()
    return result

# Stop button handler (UI thread):  result["cancel"].set()
#
# In the UI thread, on a timer, read the shared instance:
#   pct = 100 * progress.chunks_sent / max(1, progress.chunks_total)
#   label = f"{progress.instructions_sent}/{progress.instructions_total} instr, " \
#           f"{progress.bytes_sent} bytes, {progress.elapsed_seconds:.0f}s"
# When the run ends, progress.cancelled is True if it was stopped early, and
# progress.to_dict() gives a JSON-ready run report.
#
# Push alternative to the timer: pass progress_callback=lambda p: emit_signal(p) to
# plot_program. It fires after each chunk and at the end, on the worker thread.
```

PyQt mapping: run `worker` in a `QThread` (or `QThreadPool`), drive the progress
bar from a `QTimer` reading `progress`, wire a Stop button to `cancel.set()`, and
emit a signal from `on_done`. Keep the `SerialTransport` and `plot_program` call
entirely on the worker thread.

### 8. Interactive error handling (optional)

With `ErrorPolicy.PROMPT`, supply a `prompt_handler` to ask the operator per device
error instead of aborting. It is called **on the worker thread**, so marshal to the
UI and block for the answer.

```python
from hpgl_buddy import DECISION_CONTINUE, DECISION_ABORT

def prompt_handler(chunk, error_number, channel, meaning) -> str:
    # channel is "I/O" or "HP-GL"; return one of the DECISION_* strings.
    answer = ask_user_modal(f"{channel} error {error_number} ({meaning}) "
                            f"at chunk #{chunk.index}. Recover and continue?")
    return DECISION_CONTINUE if answer else DECISION_ABORT

plot_program(transport, program, device,
             error_policy=ErrorPolicy.PROMPT, prompt_handler=prompt_handler,
             progress=progress)
```

### 9. Capture library logs into the UI

```python
import logging

class QtLogHandler(logging.Handler):
    def emit(self, record):
        append_to_log_panel(self.format(record))   # marshal to the UI thread

handler = QtLogHandler()
handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
logging.getLogger("hpgl_buddy").addHandler(handler)
logging.getLogger("hpgl_buddy").setLevel(logging.DEBUG)  # DEBUG includes wire bytes
```

## Error types to catch

All inherit from `HpglBuddyError`:

- `TransportError` - port open/read/write/timeout failures (and missing pyserial).
- `DeviceError` - the plotter reported a fault; carries `.error_code` and
  `.error_meaning`. Under `ErrorPolicy.ABORT` the pen is parked before it is raised.
- `HpglSyntaxError`, `ProtocolError`, `BufferPolicyError` - less common; catching the
  `HpglBuddyError` base covers them.
