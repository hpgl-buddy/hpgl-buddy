"""Command-line interface for hpgl-buddy.

Subcommands:
    check    - offline HP-GL syntax check (no device).
    status   - ad-hoc plotter healthcheck over RS-232.
    monitor  - enable/disable monitor mode (computer port) or watch the echo (terminal port).
    plot     - safe, buffer-aware plotting of an HP-GL file.
    demo     - generate demo HP-GL (card grid or house line-drawing).

Output goes through logging only (never print), so a run can be fully
reconstructed from the log.
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
from .execution import ErrorPolicy, ProgressState, VerifyMode, plot_program
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

def _build_transport(args: argparse.Namespace) -> SerialTransport:
    """Build a SerialTransport for ``args.port``, taking unset values from the
    device profile."""
    device = get_device(args.model)
    baud = args.baud if args.baud is not None else device.profile.serial_defaults.baud
    framing = args.framing or device.profile.serial_defaults.framing
    logger.info(
        "Target: %s on %s @ %d %s", device.describe(), args.port, baud, framing
    )
    return SerialTransport(
        port=args.port,
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
    # COMPUTER (data) port; the plotter then echoes received bytes out a separate
    # TERMINAL port. The three actions are kept on their own ports:
    #   enable/disable --port <computer>   watch --port <terminal>
    if args.state == "watch":
        logger.info("Watching the monitor echo on %s", args.port)
        with _build_transport(args) as terminal_port:
            watch(terminal_port, duration_seconds=args.seconds)
        return EXIT_OK

    # enable / disable monitor mode on the computer (data) port.
    enabled = args.state == "enable"
    command = escape.monitor_mode(enabled, display_received=(args.mode == "received"))
    with _build_transport(args) as computer_port:
        computer_port.write(command)
    logger.info(
        "Monitor mode %s on %s. Watch the echo on the terminal port with: "
        "hpgl-buddy monitor watch --port <terminal-port>",
        "ENABLED" if enabled else "DISABLED",
        args.port,
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
    verify_mode = VerifyMode(args.live_hpgl_verify)
    policy = ErrorPolicy(args.on_error)

    # The plot orchestration (chunk sizing, planning, flow control + executor
    # wiring) lives in execution.plot_program so the CLI and external callers
    # share one tested path. The CLI owns file I/O, the syntax gate above, the
    # transport lifecycle, and the run report below.
    transport = _build_transport(args)
    progress = ProgressState()
    with transport:
        plot_program(
            transport,
            program,
            device,
            verify_mode=verify_mode,
            error_policy=policy,
            prompt_handler=_stdin_prompt if policy is ErrorPolicy.PROMPT else None,
            query_timeout_seconds=args.timeout,
            progress=progress,
        )

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
    parser.add_argument("-V", "--version", action="version", version=f"hpgl-buddy {__version__}")
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
        help="enable/disable monitor mode (computer port) or watch the echo (terminal port)",
    )
    monitor_parser.add_argument("state", choices=["enable", "disable", "watch"], help="enable/disable monitor mode on the computer (data) port, or watch the byte echo on the terminal port")
    monitor_parser.add_argument("--mode", choices=["received", "parsed"], default="received", help="for enable: which of the two monitor modes - 'received' = bytes echoed as received (ESC.@ mode 1), 'parsed' = as parsed from the buffer (ESC.@ mode 0). Default: received.")
    monitor_parser.add_argument("--seconds", type=float, default=None, help="for watch: duration in seconds (default: until interrupted)")
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
    demo_parser.add_argument("--scene", choices=["card", "house"], default="card", help="'card' = shapes/fills/labels/colours grid; 'house' = one giant >1024-byte PD instruction (oversized-instruction case, streamed in sub-blocks)")
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
