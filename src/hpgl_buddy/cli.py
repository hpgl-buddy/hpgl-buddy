"""Command-line interface for hpgl-buddy.

Subcommands:
    check    - offline HP-GL syntax check (no device).
    status   - ad-hoc plotter healthcheck over RS-232.
    monitor  - switch the plotter's monitor mode on or off.
    plot     - safe, buffer-aware plotting of a file (in progress).
    demo     - generate demo HP-GL for a pen count (in progress).

The CLI never uses print; all output goes through logging so a run can be
fully reconstructed from the log.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .devices import get_device
from .errors import HpglBuddyError
from .execution import ErrorPolicy, Executor, ProgressState, VerifyMode, plan_chunks
from .execution.flow_control import FlowController
from .execution.planner import DEFAULT_MAX_CHUNK_BYTES
from .demo import generate_demo, generate_scene
from .hpgl import check_program, parse_hpgl
from .interface import SerialTransport
from .logging_setup import configure_logging
from .status import escape, run_healthcheck, watch

logger = logging.getLogger("hpgl_buddy.cli")

EXIT_OK = 0
EXIT_FINDINGS = 1  # syntax errors found / device reported a problem
EXIT_USAGE = 2  # bad usage or not-yet-implemented
EXIT_FAILURE = 3  # transport or other domain failure


# --- transport construction ------------------------------------------------

def _build_transport(args: argparse.Namespace, port: str | None = None) -> SerialTransport:
    """Build a SerialTransport, taking unset values from the device profile.

    ``port`` overrides ``args.port`` so a single command can address more than
    one physical connector (e.g. the monitor's computer vs. terminal ports).
    """
    device = get_device(args.model)
    target_port = port if port is not None else args.port
    baud = args.baud if args.baud is not None else device.profile.serial_defaults.baud
    framing = args.framing or device.profile.serial_defaults.framing
    logger.info(
        "Target: %s on %s @ %d %s", device.describe(), target_port, baud, framing
    )
    return SerialTransport(
        port=target_port,
        baud=baud,
        framing=framing,
        read_timeout_seconds=args.timeout,
        software_flow_control=args.xonxoff,
        hardware_flow_control=args.rtscts,
    )


# --- subcommand handlers ---------------------------------------------------

def _handle_check(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HpglBuddyError(f"cannot read HP-GL file '{path}': {exc}") from exc

    program = parse_hpgl(data, source_name=str(path))
    findings = check_program(program)

    error_count = 0
    for finding in findings:
        if finding.severity == "error":
            error_count += 1
            logger.error("%s", finding)
        else:
            logger.warning("%s", finding)

    if error_count:
        logger.error(
            "Syntax check FAILED: %d error(s) in %d instruction(s)",
            error_count,
            len(program),
        )
        return EXIT_FINDINGS

    logger.info(
        "Syntax check PASSED: %d instruction(s), %d warning(s)",
        len(program),
        len(findings),
    )
    return EXIT_OK


def _handle_status(args: argparse.Namespace) -> int:
    transport = _build_transport(args)
    with transport:
        report = run_healthcheck(transport, timeout_seconds=args.timeout)

    # Emit the rendered report through logging, one line at a time.
    for line in report.render().splitlines():
        logger.info("%s", line)

    if report.notes:
        return EXIT_FINDINGS
    if report.io_error_number not in (None, 0):
        return EXIT_FINDINGS
    if report.hpgl_error_number not in (None, 0):
        return EXIT_FINDINGS
    return EXIT_OK


def _handle_monitor(args: argparse.Namespace) -> int:
    # On the 7475A, monitor mode is enabled by an ESC sequence sent to the
    # COMPUTER (data) port; the plotter then echoes received bytes out a
    # separate TERMINAL port. So 'on'/'off' address the computer port, and
    # 'watch' reads the terminal port.
    if args.state == "watch":
        if args.enable:
            if not args.command_port:
                raise HpglBuddyError(
                    "--enable needs --command-port (the COMPUTER port) to send the "
                    "monitor-on sequence; --port is the TERMINAL port we read from"
                )
            logger.info("Enabling monitor mode (mode %d) on computer port %s", args.mode, args.command_port)
            with _build_transport(args, port=args.command_port) as computer_port:
                computer_port.write(escape.monitor_mode(True, display_received=(args.mode == 1)))
        logger.info("Reading echo on terminal port %s", args.port)
        with _build_transport(args) as terminal_port:
            watch(terminal_port, duration_seconds=args.seconds)
        return EXIT_OK

    # 'on' / 'off' toggle monitor mode on the computer (data) port.
    enabled = args.state == "on"
    command = escape.monitor_mode(enabled, display_received=(args.mode == 1))
    with _build_transport(args) as computer_port:
        computer_port.write(command)
    logger.info(
        "Monitor mode %s requested on %s (mode %d). Echo appears on the terminal port; "
        "watch it with: hpgl-buddy monitor watch --port <terminal-port>",
        "ON" if enabled else "OFF",
        args.port,
        args.mode,
    )
    return EXIT_OK


def _stdin_prompt(chunk, error_number, channel, meaning) -> str:
    """Interactive prompt for the 'prompt' error policy."""
    from .execution.executor import DECISION_ABORT, DECISION_CONTINUE

    sys.stderr.write(
        f"\n{channel} error {error_number} ({meaning}) in chunk #{chunk.index}.\n"
        f"Recover (discard buffer, reinitialize, replay state) and continue? [y/N]: "
    )
    sys.stderr.flush()
    answer = sys.stdin.readline().strip().lower()
    return DECISION_CONTINUE if answer in ("y", "yes") else DECISION_ABORT


def _handle_plot(args: argparse.Namespace) -> int:
    path = Path(args.file)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise HpglBuddyError(f"cannot read HP-GL file '{path}': {exc}") from exc

    program = parse_hpgl(data, source_name=str(path))
    findings = check_program(program)
    error_count = sum(1 for finding in findings if finding.severity == "error")
    for finding in findings:
        log = logger.error if finding.severity == "error" else logger.warning
        log("%s", finding)
    if error_count and not args.ignore_syntax_errors:
        logger.error(
            "Refusing to plot: %d syntax error(s). Re-run with --ignore-syntax-errors "
            "to send anyway.",
            error_count,
        )
        return EXIT_FINDINGS

    device = get_device(args.model)
    if not device.profile.pen_sensing:
        logger.info(
            "Note: %s has no pen sensing - load the pens this file uses before plotting "
            "(a missing pen plots dry and is not detectable).",
            device.model,
        )
    verify_mode = VerifyMode(args.live_hpgl_verify)
    chunk_budget = min(DEFAULT_MAX_CHUNK_BYTES, max(64, device.buffer_bytes - 128))
    chunks = plan_chunks(
        program,
        max_chunk_bytes=chunk_budget,
        break_on_pen_up=(verify_mode is VerifyMode.PU),
    )

    transport = _build_transport(args)
    policy = ErrorPolicy(args.on_error)
    progress = ProgressState()
    with transport:
        flow_controller = FlowController(
            transport, buffer_size_bytes=device.buffer_bytes, query_timeout_seconds=args.timeout
        )
        executor = Executor(
            transport,
            flow_controller,
            error_policy=policy,
            prompt_handler=_stdin_prompt if policy is ErrorPolicy.PROMPT else None,
            verify_mode=verify_mode,
        )
        executor.run(chunks, progress)

    for line in progress.render().splitlines():
        logger.info("%s", line)

    if args.stats_json:
        document = json.dumps(progress.to_dict(), indent=2)
        if args.stats_json == "-":
            sys.stdout.write(document + "\n")
            sys.stdout.flush()
        else:
            Path(args.stats_json).write_text(document + "\n", encoding="utf-8")
            logger.info("Wrote run statistics to %s", args.stats_json)
    return EXIT_OK


def _handle_demo(args: argparse.Namespace) -> int:
    if args.scene == "house":
        program_bytes = generate_scene()
    else:
        program_bytes = generate_demo(pen_count=args.pens)
    if args.out:
        Path(args.out).write_bytes(program_bytes)
        logger.info("Wrote %d bytes of demo HP-GL to %s", len(program_bytes), args.out)
    else:
        sys.stdout.buffer.write(program_bytes)
        sys.stdout.buffer.flush()
    return EXIT_OK


# --- argument parser -------------------------------------------------------

def _add_serial_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--port", required=True, help="serial device path, e.g. /dev/tty.usbserial-XXXX")
    subparser.add_argument("--model", default="hp7475a", help="device profile id (default: hp7475a)")
    subparser.add_argument("--baud", type=int, default=None, help="baud rate (default: profile value)")
    subparser.add_argument("--framing", default=None, help="serial framing, e.g. 8N1 (default: profile value)")
    subparser.add_argument("--timeout", type=float, default=2.0, help="read timeout in seconds (default: 2.0)")
    subparser.add_argument("--xonxoff", action="store_true", help="enable XON/XOFF software flow control (off by default)")
    subparser.add_argument("--rtscts", action="store_true", help="enable RTS/CTS hardware flow control")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hpgl-buddy",
        description="Carefree, observable plotting of HP-GL files on HP pen plotters.",
    )
    parser.add_argument("--version", action="version", version=f"hpgl-buddy {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable DEBUG logging incl. raw wire bytes")

    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="offline HP-GL syntax check")
    check_parser.add_argument("file", help="path to an HP-GL file")
    check_parser.set_defaults(handler=_handle_check)

    status_parser = subparsers.add_parser("status", help="ad-hoc plotter healthcheck")
    _add_serial_arguments(status_parser)
    status_parser.set_defaults(handler=_handle_status)

    monitor_parser = subparsers.add_parser(
        "monitor",
        help="switch monitor mode (computer port) or watch the echo (terminal port)",
    )
    monitor_parser.add_argument("state", choices=["on", "off", "watch"], help="on/off toggle monitor mode on the computer port; watch streams each byte echoed on the terminal port")
    monitor_parser.add_argument("--mode", type=int, choices=[0, 1], default=1, help="0=bytes as parsed, 1=bytes as received (default: 1)")
    monitor_parser.add_argument("--seconds", type=float, default=None, help="watch duration in seconds (default: until interrupted)")
    monitor_parser.add_argument("--enable", action="store_true", help="for watch: enable monitor mode on --command-port before reading")
    monitor_parser.add_argument("--command-port", default=None, help="for watch --enable: the COMPUTER (data) port to send the monitor-on sequence to")
    _add_serial_arguments(monitor_parser)
    monitor_parser.set_defaults(handler=_handle_monitor)

    plot_parser = subparsers.add_parser("plot", help="safe, buffer-aware plotting of an HP-GL file")
    plot_parser.add_argument("file", help="path to an HP-GL file")
    _add_serial_arguments(plot_parser)
    plot_parser.add_argument("--on-error", choices=["abort", "prompt", "continue"], default="abort", help="behavior on a reported error (default: abort)")
    plot_parser.add_argument("--ignore-syntax-errors", action="store_true", help="plot even if the offline syntax check finds errors")
    plot_parser.add_argument("--stats-json", default=None, metavar="PATH", help="write run statistics as JSON to PATH ('-' for stdout)")
    plot_parser.add_argument("--live-hpgl-verify", choices=["off", "chunk", "pu"], default="off", help="live on-device HP-GL (OE) verification: off (env checks only, default); chunk (one-deep tailgate per pen-up chunk); pu (checkpoint at every pen-up). Environmental ESC.E/ESC.O checks are always on.")
    plot_parser.set_defaults(handler=_handle_plot)

    demo_parser = subparsers.add_parser("demo", help="generate demo HP-GL")
    demo_parser.add_argument("--scene", choices=["card", "house"], default="card", help="'card' = shapes/fills/labels/colours grid; 'house' = one continuous pen-down line drawing")
    demo_parser.add_argument("--pens", type=int, default=1, help="number of pens to exercise for the card scene (1-6)")
    demo_parser.add_argument("--out", default=None, help="output file (default: stdout)")
    demo_parser.set_defaults(handler=_handle_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose)

    try:
        return args.handler(args)
    except HpglBuddyError as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        return EXIT_FAILURE
    except Exception:  # unhandled: emit a full stacktrace per the design
        logger.exception("Unhandled error during '%s'", getattr(args, "command", "?"))
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
