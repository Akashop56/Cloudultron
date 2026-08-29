"""``cloudultron`` command-line entry point.

Subcommands are ordered by how dangerous they are, which is also the order you
should try them in on a new device::

    doctor     no device mutation at all; "can we see an adb and a device?"
    snapshot   one hierarchy dump; prints the digest and both hashes
    shell      guarded one-off device command (read-only unless --execute)
    run        the loop. dry-run by default, so `run` is also safe

The mode switch is deliberately asymmetric: ``--dry-run`` is the default and
``--execute`` must be typed. On a harness whose whole purpose is clicking
things, "forgot to pass the flag" should fail closed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from . import __version__, build_device
from .config import ExecutorConfig
from .errors import CloudultronError, GuardViolation
from .loop.engine import Executor, StepOutcome, StepRecord
from .loop.policy import ExplorePolicy, NullPolicy, ScriptedPolicy
from .safety import Guard, describe_verdict
from .ui.hashing import compare, content_hash, structure_hash
from .ui.render import render_digest, render_tree


# ---------------------------------------------------------------------------


#: Flags accepted either before or after the subcommand.
_GLOBAL_FLAGS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("--serial", {"help": "adb target; host:port for adb-over-WiFi (default from $CLOUDULTRON_ADB_SERIAL)"}),
    ("--adb", {"help": "path to the adb binary (default: adb on PATH)"}),
    ("--mock", {"action": "store_true", "help": "use the built-in fake device; no adb required"}),
    ("--timeout", {"type": float, "help": "per-command timeout in seconds"}),
    ("--json", {"action": "store_true", "help": "machine-readable output on stdout"}),
    ("-v", "--verbose", {"action": "store_true", "help": "include the full node tree"}),
    ("-q", "--quiet", {"action": "store_true", "help": "suppress per-step lines"}),
    ("--dry-run", {"dest": "dry_run", "action": "store_true", "default": None, "help": "plan mutations, dispatch none (the default)"}),
    ("--execute", {"dest": "dry_run", "action": "store_false", "help": "actually dispatch mutations"}),
)


def _add_global_flags(target: argparse.ArgumentParser, *, suppressing: bool) -> None:
    """Register the shared flags on a parser.

    ``suppressing=True`` sets ``default=argparse.SUPPRESS`` so the *subparser*
    copy never overwrites what the top-level parser already parsed. Without
    that, ``cloudultron --mock run`` would silently lose ``--mock``: subparsers
    re-default every attribute they declare, and "absent" would become False.
    """
    for spec in _GLOBAL_FLAGS:
        names, kwargs = spec[:-1], dict(spec[-1])
        if "default" in kwargs:
            if suppressing:
                kwargs["default"] = argparse.SUPPRESS
        elif suppressing:
            kwargs["default"] = argparse.SUPPRESS
        target.add_argument(*names, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cloudultron",
        description="Android UI automation executor with structural state-diffing and an enforced command guard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples\n"
            "  cloudultron doctor\n"
            "  cloudultron snapshot --save dump.xml\n"
            "  cloudultron run --mock --policy explore --steps 8\n"
            "  cloudultron run --mock --policy scripted --script demo.txt --execute\n"
            "  cloudultron shell 'dumpsys window | head -40'\n"
            "\n"
            "targets\n"
            "  --serial emulator-5554          local emulator (USB/emu transport)\n"
            f"  --serial 192.168.1.50:5555      adb over WiFi; `adb connect` is run for you\n"
            "  CLOUDULTRON_ADB_SERIAL=...       export it on Termux and stop typing it\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"cloudultron {__version__}")
    _add_global_flags(parser, suppressing=False)

    common = argparse.ArgumentParser(add_help=False)
    _add_global_flags(common, suppressing=True)

    # dest must not be "command": the `shell` subparser takes a positional
    # argument of that name, and argparse lets the positional win, silently
    # replacing the dispatch key with the user's command string.
    sub = parser.add_subparsers(dest="subcommand", required=True)

    run = sub.add_parser("run", parents=[common], help="run the observe->think->act loop")
    run.add_argument("--steps", type=int, help="max steps (default from config)")
    run.add_argument(
        "--policy",
        choices=("observe", "explore", "scripted"),
        default="observe",
        help="observe: never acts; explore: novelty search; scripted: replay --script",
    )
    run.add_argument("--script", help="file of scripted actions, one per line or JSON")
    run.add_argument("--settle", type=float, help="seconds to wait after each dispatched mutation")
    run.add_argument("--stagnation", type=int, help="steps with no structural change before the loop trips")
    run.add_argument("--record", nargs="?", const="runs", help="write trace.jsonl + dumps under this directory (default: ./runs)")
    run.add_argument("--until-package", help="stop once this package is focused (goal test without a policy)")

    snap = sub.add_parser("snapshot", parents=[common], help="dump and parse one screen")
    snap.add_argument("--save", help="also write the raw XML here")
    snap.add_argument("--tree", action="store_true", help="print the whole node tree, not just interactables")
    snap.add_argument("--compare-with", help="parse a saved XML file and diff it against the live screen")

    doc = sub.add_parser("doctor", parents=[common], help="check adb, the target, and the dump path")
    doc.add_argument("--deep", action="store_true", help="also try a real hierarchy dump")

    sh = sub.add_parser("shell", parents=[common], help="run one guarded device-side command")
    sh.add_argument("command", help="e.g. 'dumpsys window'")

    val = sub.add_parser("script", parents=[common], help="validate/normalise a script file without a device")
    val.add_argument("path", help="script file to check")
    val.add_argument("--emit", action="store_true", help="print canonical JSON")

    return parser


# ---------------------------------------------------------------------------


def _config_from_args(args: argparse.Namespace) -> ExecutorConfig:
    overrides: dict[str, Any] = {}
    if args.serial is not None:
        overrides["serial"] = args.serial
    if args.adb is not None:
        overrides["adb_path"] = args.adb
    if args.timeout is not None:
        overrides["command_timeout"] = args.timeout
    if args.dry_run is not None:
        overrides["dry_run"] = args.dry_run
    if getattr(args, "steps", None) is not None:
        overrides["max_steps"] = args.steps
    if getattr(args, "settle", None) is not None:
        overrides["settle_delay"] = args.settle
    if getattr(args, "stagnation", None) is not None:
        overrides["stagnation_limit"] = args.stagnation
    if getattr(args, "record", None):
        overrides["record_dir"] = args.record
    config = ExecutorConfig.from_env(overrides)
    if config.serial is None and not args.mock:
        config.serial = None  # let adb pick the sole device; do not assume :5555
    return config


def _make_device(args: argparse.Namespace, config: ExecutorConfig):
    return build_device(config, mock=args.mock)


def _emit(args: argparse.Namespace, payload: dict[str, Any], text_lines: list[str]) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, default=str, sort_keys=True))
    else:
        for line in text_lines:
            print(line)


def _step_line(record: StepRecord, args: argparse.Namespace) -> str:
    icon = {
        StepOutcome.OBSERVED.value: "·",
        StepOutcome.PLANNED.value: "◇",
        StepOutcome.EXECUTED.value: "◆",
        StepOutcome.BLOCKED.value: "✗",
        StepOutcome.REFUSED.value: "!",
        StepOutcome.FAILED.value: "✗",
    }.get(record.outcome, "?")
    bits = [f"step {record.step:>2} {icon} {record.outcome:<8}"]
    if record.nodes:
        bits.append(f"nodes={record.nodes}")
    bits.append(f"chg={record.change_level or '-'}({record.change})" if record.change else "chg=-")
    bits.append(f"h={record.structure_hash[:8]}")
    if record.decision:
        bits.append(f"-> {record.decision}")
    if record.target:
        bits.append(f"[{record.target}]")
    if record.guard:
        bits.append(f"guard:{record.guard}")
    if record.loop:
        bits.append(f"loop:{record.loop}")
    if record.detail:
        bits.append(record.detail[:90])
    return "  ".join(bits)


# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    lines: list[str] = []
    payload: dict[str, Any] = {"mode": "mock" if args.mock else "adb", "config": config.redacted()}

    lines.append(f"cloudultron {__version__}")
    lines.append(f"python      {sys.version.split()[0]}")
    lines.append(f"mode        {'DRY-RUN (mutations planned, not dispatched)' if config.dry_run else 'EXECUTE (mutations dispatched)'}")

    if args.mock:
        lines.append("adb         skipped (--mock)")
        payload["adb"] = "skipped"
        payload["device_ok"] = True
        lines.append("device      fake (5 screens: launcher, home, detail, confirm, settings)")
    else:
        try:
            _, transport = _make_device(args, config)
            info = transport.check()
            payload["adb"] = info
            lines.append(f"adb         {info.get('adb_version', '?')}")
            devices = info.get("devices") or []
            if info.get("error"):
                lines.append(f"error       {info['error']}")
            elif not devices:
                lines.append("devices     none attached")
                if config.is_remote_serial and config.serial:
                    lines.append(f"            for a network target run: adb connect {config.serial}")
            else:
                for row in devices:
                    parts = row if isinstance(row, list) else [str(row)]
                    marker = " <- selected" if config.serial and parts and parts[0] == config.serial else ""
                    state = parts[1] if len(parts) > 1 else "?"
                    lines.append(f"device      {parts[0]} [{state}]{marker}")
                    if state not in ("device",):
                        lines.append(f"            state {state!r}: unlock the screen / accept the RSA prompt")
            payload["device_ok"] = bool(devices) and all(
                (isinstance(r, list) and len(r) > 1 and r[1] == "device") or (isinstance(r, str) and "device" in r) for r in devices
            )
        except CloudultronError as exc:
            payload["error"] = str(exc)
            lines.append(f"error       {exc}")

    if args.deep and not args.mock:
        try:
            device, _ = _make_device(args, config)
            hierarchy = device.hierarchy(force=True)
            payload["dump_ok"] = True
            payload["nodes"] = hierarchy.screen.node_count
            lines.append(f"dump        ok ({hierarchy.screen.node_count} nodes via {hierarchy.method})")
            lines.append(f"focused     {device.current_focus() or 'unknown'}")
        except CloudultronError as exc:
            payload["dump_ok"] = False
            payload["dump_error"] = str(exc)
            lines.append(f"dump        FAILED: {exc}")
    return _report(args, payload, lines)


def cmd_snapshot(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    device, _ = _make_device(args, config)
    hierarchy = device.hierarchy(force=True)
    screen = hierarchy.screen
    focus = ""
    try:
        focus = device.current_focus()
    except CloudultronError:
        pass
    digest, indexed = render_digest(screen, focused_window=focus, max_count=config.max_interactables)

    lines = [digest, "", f"structure {structure_hash(screen)}   content {content_hash(screen)}", f"dump via {hierarchy.method} in {hierarchy.elapsed_ms}ms"]
    if hierarchy.report.noteworthy:
        lines.append(f"note: dump needed repair: sanitised={hierarchy.report.sanitised_chars} sliced={hierarchy.report.sliced_from_garbage} truncation={hierarchy.report.recovered_truncation}")
    payload = {
        "structure_hash": structure_hash(screen),
        "content_hash": content_hash(screen),
        "package": screen.package,
        "focused_window": focus,
        "nodes": screen.node_count,
        "window_size": list(screen.window_size),
        "interactables": len(indexed),
        "method": hierarchy.method,
        "digest": digest,
    }
    if args.tree:
        tree = render_tree(screen)
        lines.append("")
        lines.append(tree)
        payload["tree"] = tree
    if args.save:
        path = pathlib.Path(args.save)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(hierarchy.raw, encoding="utf-8")
        lines.append(f"saved {len(hierarchy.raw)} bytes -> {path}")
        payload["saved"] = str(path)
    if args.compare_with:
        other_raw = pathlib.Path(args.compare_with).read_text(encoding="utf-8")
        from .ui.parser import parse_hierarchy as _parse

        other, _ = _parse(other_raw)
        diff = compare(other, screen)
        lines.append("")
        lines.append(f"vs {args.compare_with}: {diff.summary()}")
        payload["diff"] = {"level": diff.level, "summary": diff.summary(), "added": list(diff.added), "removed": list(diff.removed)}
    return _report(args, payload, lines)


def cmd_run(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    device, _ = _make_device(args, config)

    if args.policy == "observe":
        policy: Any = NullPolicy()
    elif args.policy == "explore":
        policy = ExplorePolicy()
    else:
        if not args.script:
            print("error: --policy scripted needs --script FILE", file=sys.stderr)
            return 2
        policy = ScriptedPolicy.from_file(args.script)

    guard = Guard(dry_run=config.dry_run, deny_raw_shell=True)
    engine = Executor(device, policy, config=config, guard=guard, on_step=None if args.quiet else _printer(args))

    until = None
    if args.until_package:
        needle = args.until_package

        def until(record: StepRecord, _needle: str = needle) -> bool:  # noqa: ANN001
            return record.package == _needle or _needle in (record.focused_window or "")

    try:
        report = engine.run(max_steps=args.steps, until=until)
    except CloudultronError as exc:
        print(f"fatal: {exc}", file=sys.stderr)
        return 1

    lines: list[str] = []
    # The live printer already streamed each step; re-printing them here would
    # duplicate output. Only in --quiet mode (no printer) is a recap useful.
    if args.quiet:
        for record in engine.steps:
            lines.append(_step_line(record, args))
    lines.append("")
    lines.append(report.summary())
    for warning in report.warnings:
        lines.append(f"warning: {warning}")
    if report.trace_path:
        lines.append(f"trace: {report.trace_path}")
    if config.dry_run and report.planned:
        lines.append("")
        lines.append("dry-run: {} planned action(s) were NOT dispatched. Re-run with --execute.".format(report.planned))

    payload = {
        "report": _report_payload(report),
        "steps": [record.to_dict() for record in engine.steps],
        "policy": getattr(policy, "name", "?"),
    }
    code = 0 if report.status in {"ok", "done"} else 3
    _report(args, payload, lines, code=code)
    return code


def _report_payload(report) -> dict[str, Any]:
    from dataclasses import asdict

    data = asdict(report)
    data["terminal"] = report.terminal.value if report.terminal else None
    data["status"] = report.status
    return data


def _printer(args: argparse.Namespace):
    def emit(record: StepRecord) -> None:
        print(_step_line(record, args), file=sys.stderr)

    return emit


def cmd_shell(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    # A human typing `cloudultron shell` has explicitly asked for a command, so
    # the raw-shell path is open here (deny_raw_shell=False) while still being
    # classified, dry-run-gated, and destructively blocked.
    guard = Guard(dry_run=config.dry_run, deny_raw_shell=False)
    verdict = guard.check_shell(args.command)
    lines = [f"effect  {verdict.effect}", f"verdict {describe_verdict(verdict)}"]
    if not verdict.allowed:
        payload = {
            "allowed": False,
            "reason": verdict.reason,
            "effect": str(verdict.effect),
            "deferred": verdict.deferred,
            "command": args.command,
        }
        if args.json:
            _emit(args, payload, [])
        else:
            # Deferred (dry-run held it back) and blocked (the guard never allows
            # it) read differently on purpose: one needs a flag, the other needs
            # a different command.
            word = "deferred" if verdict.deferred else "blocked"
            print("\n".join(lines), file=sys.stderr)
            print(f"{word}: {verdict.reason}", file=sys.stderr)
        return 4
    device, transport = _make_device(args, config)
    result = transport.shell_raw(args.command, timeout=config.command_timeout)
    lines.append(f"exit    {result.returncode} in {result.duration_ms}ms")
    if result.text.strip():
        lines.append(result.text.rstrip())
    if result.error_text.strip():
        lines.append(f"stderr: {result.error_text.rstrip()}")
    _report(args, {"allowed": True, "effect": str(verdict.effect), "exit": result.returncode, "stdout": result.text, "stderr": result.error_text}, lines)
    return 0 if result.ok else 5


def cmd_script(args: argparse.Namespace) -> int:
    raw = pathlib.Path(args.path).read_text(encoding="utf-8")
    policy = ScriptedPolicy.from_text(raw)
    lines = [f"{len(policy.actions)} action(s) in {args.path}"]
    payload = {"count": len(policy.actions), "actions": [a.to_dict() for a in policy.actions]}
    for action in policy.actions:
        marker = "write" if action.is_mutation else "read"
        lines.append(f"  {action.describe():<44} {marker:<5} {action.rationale}")
    if args.emit:
        print(json.dumps(payload, indent=2))
        return 0
    return _report(args, payload, lines)


def _report(args: argparse.Namespace, payload: dict[str, Any], lines: list[str], code: int = 0) -> int:
    _emit(args, payload, lines)
    return code


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "doctor": cmd_doctor,
        "snapshot": cmd_snapshot,
        "run": cmd_run,
        "shell": cmd_shell,
        "script": cmd_script,
    }
    handler = handlers.get(args.subcommand)
    if handler is None:  # pragma: no cover - argparse enforces required subcommand
        parser.error(f"unknown command {args.subcommand!r}")
        return 2
    try:
        return handler(args)
    except GuardViolation as exc:
        print(f"guard: {exc}", file=sys.stderr)
        return 4
    except CloudultronError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
