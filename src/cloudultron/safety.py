"""The guard: where "don't do destructive things" stops being a promise.

Design note
-----------
The executor does **not** accept a raw shell string from the decision-maker as
its normal operating path. Policies emit typed :class:`Action` objects
(:meth:`Action.tap`, :meth:`Action.keyevent`, ...) and only the dispatcher
below turns those into argv lists. That single inversion is what makes the
safety layer real:

* a policy cannot express ``rm -rf /sdcard`` as a ``TapAction``, so the
  enormous majority of mistakes are unrepresentable rather than filtered;
* there is no string parsing to fool with quotes, backticks, ``$()``, newlines,
  or a ``;`` hiding inside a filename.

Two escape hatches remain for humans (``cloudultron shell``) and for a future
raw-shell-capable policy. Both go through :class:`Guard`, which classifies by
*argv token* rather than by regex over a shell string, and which has three
tiers. ``DESTRUCTIVE`` is blocked unconditionally: :class:`ExecutorConfig` has
``allow_destructive`` so you can see it in the config, but nothing in this
module honours it. Making a footgun expensive to arm is intentional.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import IntEnum

from .errors import GuardViolation


class Effect(IntEnum):
    """How much damage a command can do. Higher is more dangerous.

    Ordered so the guard can compare with ``>=`` instead of enumerating pairs.
    """

    READ = 0        #: pure observation: dump, cat, getprop, dumpsys
    WRITE = 1       #: mutates device state: input, am start, pm install
    DESTRUCTIVE = 2  #: not recoverable by pressing back: rm, format, dd

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.name.lower()


#: Device-side verbs we treat as observation. Anything not listed here and not
#: in :data:`WRITE_TOKENS` is treated as WRITE, because "unknown" should be
#: closer to "mutating" than to "harmless".
READ_TOKENS = frozenset(
    {
        "dumpsys", "cat", "ls", "stat", "getprop", "settings", "wm",
        "ps", "pidof", "id", "uname", "date", "which", "find", "du",
        "head", "tail", "grep", "zgrep", "wc", "sort", "uniq", "cut",
        "uiautomator", "ime", "content", "pm", "top", "getevent", "screencap",
    }
)

#: Verbs that clearly mutate. ``pm`` appears in both sets; see _classify_shell.
WRITE_TOKENS = frozenset(
    {
        "input", "am", "svc", "appops", "cmd", "setprop", "monkey",
        "screencap", "ime", "content", "settings", "wm", "mkdir", "touch",
        "cp", "mv", "kill", "pkill", "nohup", "start", "stop", "reboot",
        "uiautomator",
    }
)

#: Unrecoverable or clearly hostile. Matched on the *first token* of a segment.
DESTRUCTIVE_TOKENS = frozenset(
    {
        "rm", "rmdir", "shred", "mkfs", "fdisk", "sfdisk", "wipefs", "format",
        "dd", "truncate", "unlink", "chattr", "chown", "chmod", "setenforce",
        "iptables", "ip6tables", "nft", "recovery", "fastboot",
        # Ending the run's device out from under it is not something a policy
        # should be able to decide, however benign it looks on an emulator.
        "reboot", "reboot-bootloader", "shutdown",
    }
)

#: Patterns that are dangerous regardless of which binary they attach to:
#: redirection into a block device, a pipe-to-shell, or a fork bomb.
SUSPICIOUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rx, re.IGNORECASE), why)
    for rx, why in (
        (r">\s*/dev/(sd|block|mtd|loop)", "redirect into a block device"),
        (r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh)\b", "pipe remote download into a shell"),
        (r":\s*\(\s*\)\s*\{[^}]*\|\s*[^}]*&", "fork bomb"),
        (r"\brm\b\s+-[a-z]*r[a-z]*f|\brm\b\s+-[a-z]*f[a-z]*r", "recursive force delete"),
        (r"(^|\s)/system(\s|/)", "write to /system"),
        (r"\bdd\b[^|]*\bof=/dev/", "raw write to a device node"),
        (r"\bpm\b.*\buninstall\b", "package uninstall"),
        (r"\bsettings\b.*\bput\b", "settings mutation"),
        (r"\b(wget|curl)\b.*-O\b", "download a file to the device"),
        (r"\bnc\b|\bncat\b|\bsocat\b", "network relay"),
    )
)

#: Structural shell metacharacters we refuse to reason about at all. Trying to
#: audit `$(...)` correctly is a research project; refusing is a few lines.
_UNPARSEABLE = ("`", "$(", "<(", ">(", "/dev/")


@dataclass(frozen=True)
class Verdict:
    """The guard's decision about one command.

    ``deferred`` separates the two ways a command can be refused, which a
    reader of a trace needs: "blocked" means the guard would refuse it in any
    mode, "deferred" means dry-run held it back and ``--execute`` is all that
    stands between now and later.
    """

    allowed: bool
    effect: Effect
    reason: str = ""
    argv: tuple[str, ...] = ()
    deferred: bool = False

    def __bool__(self) -> bool:
        return self.allowed


class Guard:
    """Decides whether a command may reach the device, and when.

    Parameters
    ----------
    dry_run:
        When True, :attr:`Effect.WRITE` and above is refused, so the loop can
        still observe the screen while nothing is dispatched.
    deny_raw_shell:
        When True (the default), the raw-shell escape hatch is closed entirely
        for policies. Human CLIs pass False for their own explicit subcommand.
    """

    def __init__(self, *, dry_run: bool = True, deny_raw_shell: bool = True) -> None:
        self.dry_run = dry_run
        self.deny_raw_shell = deny_raw_shell

    # ------------------------------------------------------------ entrypoints

    def check_typed(self, effect: Effect, description: str) -> Verdict:
        """Vet an action the dispatcher already knows how to build.

        Typed actions can only ever be READ or WRITE, so the interesting logic
        is just the dry-run gate.
        """
        if self.dry_run and effect >= Effect.WRITE:
            return Verdict(False, effect, f"dry-run: {description} was not dispatched", (), deferred=True)
        return Verdict(True, effect, "", ())

    def check_shell(self, command: str) -> Verdict:
        """Vet a raw device-side shell string.

        Returns a :class:`Verdict` rather than raising so callers can report
        "what were you going to do" in dry-run mode, but :meth:`require_shell`
        is the usual entrypoint.
        """
        if self.deny_raw_shell:
            return Verdict(False, Effect.WRITE, "raw shell is disabled for policies", ())

        text = (command or "").strip()
        if not text:
            return Verdict(False, Effect.WRITE, "empty command", ())

        for token in _UNPARSEABLE:
            if token in text:
                return Verdict(False, Effect.DESTRUCTIVE, f"unauditable shell construct {token!r}", ())

        for pattern, why in SUSPICIOUS_PATTERNS:
            if pattern.search(text):
                return Verdict(False, Effect.DESTRUCTIVE, f"destructive pattern: {why}", ())

        # shlex with posix=True so quotes are consumed rather than literalised.
        try:
            segments = self._split_segments(text)
        except ValueError as exc:
            return Verdict(False, Effect.DESTRUCTIVE, f"cannot tokenise command: {exc}", ())

        effect = Effect.READ
        for segment in segments:
            if not segment:
                continue
            segment_effect = self._classify_segment(segment)
            if segment_effect > effect:
                effect = segment_effect

        if effect is Effect.DESTRUCTIVE:
            return Verdict(False, effect, "destructive verb in command", tuple(segments[0]))
        if self.dry_run and effect >= Effect.WRITE:
            return Verdict(False, effect, "dry-run: write not dispatched", tuple(segments[0]), deferred=True)
        return Verdict(True, effect, "", tuple(segments[0]))

    def require_shell(self, command: str) -> Verdict:
        """Like :meth:`check_shell` but raises on refusal."""
        verdict = self.check_shell(command)
        if not verdict.allowed:
            raise GuardViolation(f"guard blocked command ({verdict.reason}): {command}")
        return verdict

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _split_segments(text: str) -> list[list[str]]:
        """Split on shell control operators, then tokenise each piece.

        Splitting *before* tokenising is what stops ``ls && rm -rf /`` from
        being classified as a single harmless ``ls`` command.
        """
        pieces = re.split(r"&&|\|\||;|\|", text)
        return [shlex.split(piece) for piece in pieces if piece.strip()]

    @staticmethod
    def _classify_segment(segment: list[str]) -> Effect:
        """Classify one pipeline segment by its leading (non-env-assignment) token."""
        verb = ""
        for token in segment:
            if re.fullmatch(r"\w+=\S*", token):  # FOO=bar at the head of a line
                continue
            verb = token.rsplit("/", 1)[-1].lower()  # /system/bin/rm -> rm
            break

        if not verb:
            return Effect.WRITE  # nothing recognisable: assume mutating

        if verb in DESTRUCTIVE_TOKENS:
            return Effect.DESTRUCTIVE
        if verb == "pm":
            # `pm list packages` is a read; `pm install`/`clear` are not.
            return Effect.READ if len(segment) > 1 and segment[1].startswith(("list", "path", "dump", "resolve")) else Effect.WRITE
        if verb == "settings":
            # `settings get`/`show` are reads; `put`/`delete` are not. Same
            # subcommand-aware logic as pm -- the verb alone would misfile
            # every read-only settings query as a write.
            return Effect.READ if len(segment) > 1 and segment[1] in {"get", "show"} else Effect.WRITE
        if verb == "ime":
            return Effect.READ if len(segment) > 1 and segment[1] == "list" else Effect.WRITE
        if verb in ("am", "cmd", "svc"):
            return Effect.WRITE
        if verb == "input":
            return Effect.WRITE
        if verb == "find":
            # `find ... -delete` and `-exec rm` never reach the token list we
            # classify, so check the segment text explicitly.
            joined = " ".join(segment)
            if "-delete" in joined or "-exec" in joined:
                return Effect.DESTRUCTIVE
            return Effect.READ
        if verb in WRITE_TOKENS:
            return Effect.WRITE
        if verb in READ_TOKENS:
            return Effect.READ
        return Effect.WRITE  # unknown binary -> treat as a write


def describe_verdict(verdict: Verdict) -> str:
    """One-line human rendering, used by the CLI and the trace log."""
    tag = "allow" if verdict.allowed else ("defer" if verdict.deferred else "block")
    effect = str(verdict.effect)
    if verdict.reason:
        return f"{tag} [{effect}] {verdict.reason}"
    return f"{tag} [{effect}]"
