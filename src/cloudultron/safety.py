"""The guard: classify every command, gate according to a profile.

Design note
-----------
Two separations do the work here, and conflating them is what made the original
version too blunt to use.

**Classification is not blocking.** Every command is always parsed, split at
pipeline boundaries, and assigned an :class:`Effect`. That is what puts
``destructive: recursive force delete`` in the trace. In ``operator`` mode the
classification still happens -- only the gate opens. A run that is unguarded by
explicit choice should still be self-describing afterwards, which is the only
real difference between "the operator armed this" and "nobody looked".

**Profiles are data, not flags in the middle of the loop.** What is permitted is
one :class:`GuardProfile` object resolved by name, so widening the harness for a
device farm is a named, reviewable choice rather than edits scattered through the
dispatcher.

Splitting on control operators *before* tokenising is load-bearing and stays in
every profile: it is what stops ``ls && rm -rf /`` reading as a harmless ``ls``.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field, replace
from enum import IntEnum

from .errors import GuardViolation


class Effect(IntEnum):
    """How much damage a command can do. Higher is more dangerous.

    Ordered so the gate can compare with ``>=`` instead of enumerating pairs.
    """

    READ = 0  #: pure observation: dump, cat, getprop, dumpsys
    WRITE = 1  #: mutates device state: input, am start, pm install
    DESTRUCTIVE = 2  #: not recoverable by pressing back: rm, format, dd

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.name.lower()


#: Verbs that always classify as destructive. Note this is the *reporting* set
#: and is deliberately the same for every profile -- a lab run should still know
#: it just rebooted. Gating uses :attr:`GuardProfile.blocked_verbs`.
DESTRUCTIVE_TOKENS = frozenset(
    {
        "rm", "rmdir", "shred", "mkfs", "fdisk", "sfdisk", "wipefs", "format",
        "dd", "truncate", "unlink", "chattr", "chown", "chmod", "setenforce",
        "iptables", "ip6tables", "nft", "recovery", "fastboot",
        "reboot", "reboot-bootloader", "shutdown",
    }
)

#: Device-side verbs treated as observation. Anything unlisted and not a known
#: writer is treated as WRITE, because "unknown" belongs closer to "mutating".
READ_TOKENS = frozenset(
    {
        "dumpsys", "cat", "ls", "stat", "getprop", "settings", "wm",
        "ps", "pidof", "id", "uname", "date", "which", "find", "du",
        "head", "tail", "grep", "zgrep", "wc", "sort", "uniq", "cut",
        "uiautomator", "ime", "content", "pm", "top", "getevent", "screencap",
    }
)

WRITE_TOKENS = frozenset(
    {
        "input", "am", "svc", "appops", "cmd", "setprop", "monkey",
        "screencap", "ime", "content", "settings", "wm", "mkdir", "touch",
        "cp", "mv", "kill", "pkill", "nohup", "start", "stop", "reboot",
        "uiautomator",
    }
)

#: Named pattern registry, so a profile can pick a subset instead of the loop
#: growing another ``if profile == ...`` branch.
PATTERNS: dict[str, tuple[re.Pattern[str], str]] = {
    "block_device_redirect": (re.compile(r">\s*/dev/(sd|block|mtd|loop)", re.I), "redirect into a block device"),
    "pipe_to_shell": (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sh|bash|zsh)\b", re.I), "pipe remote download into a shell"),
    "fork_bomb": (re.compile(r":\s*\(\s*\)\s*\{[^}]*\|\s*[^}]*&", re.I), "fork bomb"),
    "recursive_force_delete": (re.compile(r"\brm\b\s+-[a-z]*r[a-z]*f|\brm\b\s+-[a-z]*f[a-z]*r", re.I), "recursive force delete"),
    "system_write": (re.compile(r"(^|\s)/system(\s|/)", re.I), "write to /system"),
    "raw_device_write": (re.compile(r"\bdd\b[^|]*\bof=/dev/", re.I), "raw write to a device node"),
    "package_uninstall": (re.compile(r"\bpm\b.*\buninstall\b", re.I), "package uninstall"),
    "settings_mutation": (re.compile(r"\bsettings\b.*\bput\b", re.I), "settings mutation"),
    "device_download": (re.compile(r"\b(wget|curl)\b.*-O\b", re.I), "download a file to the device"),
    "network_relay": (re.compile(r"\bnc\b|\bncat\b|\bsocat\b", re.I), "network relay"),
    # No "wipe app data" pattern on purpose. `pm clear <pkg>` is how every Android
    # test suite resets state between runs, it is already classified WRITE, and
    # gating it separately would be a *new* restriction rather than a configured
    # one. Profiles below relax things; none of them tightens `pm clear`.
}

#: Patterns every profile enforces, including a lab. These are the ones with no
#: legitimate automation use: nothing in a test pipeline needs a raw write to a
#: block device or a download piped straight into a shell.
ALWAYS_ON_PATTERNS = ("block_device_redirect", "pipe_to_shell", "fork_bomb", "raw_device_write", "network_relay")

#: Structural shell constructs this module refuses to reason about. Auditing
#: ``$(...)`` correctly is a research project; refusing it is a few lines.
_UNPARSEABLE = ("`", "$(", "<(", ">(", "/dev/")

#: Verbs a device farm legitimately needs and ``explore`` does not.
LAB_ROUTINE_VERBS = frozenset({"reboot", "reboot-bootloader", "shutdown", "chmod", "chown", "truncate", "unlink"})

#: Verbs no profile permits without the operator flag: these destroy the device
#: rather than the run.
NEVER_PERMITTED_IN_PROFILE = frozenset({"mkfs", "fdisk", "sfdisk", "wipefs", "fastboot", "recovery", "setenforce"})


@dataclass(frozen=True)
class GuardProfile:
    """One coherent answer to "what may this run do to the device?".

    Attributes
    ----------
    blocked_verbs:
        Destructive verbs this profile refuses. ``explore`` blocks the whole
        reported set; ``test_lab`` releases the routine ones (see
        :data:`LAB_ROUTINE_VERBS`) and keeps the device-destroying ones.
    patterns:
        Pattern names enforced, from :data:`PATTERNS`.
    allow_policy_raw_shell:
        Whether a *policy* may emit :meth:`Action.raw_shell`. Independent of
        dry-run: this is about who is allowed to compose a command string at all.
    """

    name: str
    blocked_verbs: frozenset[str]
    patterns: tuple[str, ...]
    allow_policy_raw_shell: bool = False
    #: Printed verbatim by the CLI so an operator can see which ruleset armed.
    note: str = ""

    @property
    def enforced(self) -> tuple[tuple[re.Pattern[str], str], ...]:
        """The compiled patterns this profile applies, in declaration order.

        An accessor rather than ``PATTERNS[key]`` at each call site because a
        profile's whole job is naming a *subset*: the lab enforces fewer patterns
        than the default, and asking the profile what it enforces keeps the
        selection in one place.
        """
        return tuple(PATTERNS[key] for key in self.patterns)

    @property
    def tolerated_destructive(self) -> frozenset[str]:
        """Destructive-classified verbs this profile permits.

        Derived rather than listed so ``blocked_verbs`` stays the single knob:
        ``test-lab`` releases reboot/chmod by removing them from the blocklist,
        and this set follows automatically.
        """
        return DESTRUCTIVE_TOKENS - self.blocked_verbs


#: Exploratory/autonomous default: nothing destructive reaches the device.
EXPLORE_PROFILE = GuardProfile(
    name="explore",
    blocked_verbs=DESTRUCTIVE_TOKENS,
    patterns=tuple(PATTERNS),
    allow_policy_raw_shell=False,
    note="strict defaults for autonomous (model-driven) policies",
)

#: CI / device-farm profile: routine lifecycle ops allowed, device-wrecking ones
#: still refused.
TEST_LAB_PROFILE = GuardProfile(
    name="test-lab",
    blocked_verbs=(DESTRUCTIVE_TOKENS - LAB_ROUTINE_VERBS),
    patterns=ALWAYS_ON_PATTERNS + ("recursive_force_delete",),
    # Raw shell is open here on purpose. With it closed, every verb this profile
    # goes out of its way to permit (reboot, pm clear, chmod, uninstall) would be
    # unreachable from a run, because a policy could only ever emit typed taps --
    # a profile whose allowances cannot be exercised is decoration. What keeps
    # the profile honest is the content gate, which still refuses rm -rf, the
    # device-wrecking list, redirects into /dev, and pipe-to-shell.
    allow_policy_raw_shell=True,
    note="routine device-lifecycle ops permitted (reboot, chmod, pm clear, uninstall, settings put); wrecking list still refused",
)

#: Operator-armed profile: classification and logging stay on, gating is off.
OPERATOR_PROFILE = GuardProfile(
    name="operator",
    blocked_verbs=frozenset(),
    patterns=(),
    allow_policy_raw_shell=True,
    note="NO GATING: every action the policy emits is dispatched. Explicit operator arming.",
)

#: The ungated profile's name. ``config`` keeps a literal copy because it may
#: not import this module (it must stay a leaf); a test asserts they agree.
OPERATOR_PROFILE_NAME = OPERATOR_PROFILE.name

PROFILES: dict[str, GuardProfile] = {
    EXPLORE_PROFILE.name: EXPLORE_PROFILE,
    TEST_LAB_PROFILE.name: TEST_LAB_PROFILE,
    OPERATOR_PROFILE.name: OPERATOR_PROFILE,
}


#: The ruleset operator mode measures its waivers against. Same object as
#: EXPLORE_PROFILE by name, and a test pins that so the reference cannot drift to
#: something weaker than the default gate.
REFERENCE_PROFILE = EXPLORE_PROFILE


def resolve_profile(name: str) -> GuardProfile:
    """Look up a profile by name, with a helpful error for typos."""
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown guard profile {name!r}; choose from {', '.join(sorted(PROFILES))}"
        ) from None


@dataclass(frozen=True)
class Verdict:
    """The guard's decision about one command.

    ``deferred`` and ``overridden`` distinguish the three ways a command does
    not run -- which matters to whoever reads a trace an hour later:

    ``deferred``
        dry-run held it back; ``--execute`` is all that separates now from later.
    ``blocked``
        the profile refuses it in any mode.
    ``overridden``
        the profile would have refused it and the operator's arming let it
        through. Recorded, not hidden, so the trace still says what happened.
    """

    allowed: bool
    effect: Effect
    reason: str = ""
    argv: tuple[str, ...] = ()
    deferred: bool = False
    overridden: bool = False
    #: The profile's original objection, kept even when ``allowed``.
    objection: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class Guard:
    """Decides whether a command may reach the device, and when.

    Parameters
    ----------
    profile:
        The ruleset. Defaults to :data:`EXPLORE_PROFILE`.
    dry_run:
        When True, :attr:`Effect.WRITE` and above is refused, so the loop can
        observe the screen while planning rather than performing.
    deny_raw_shell:
        ``None`` means "follow the profile"; an explicit bool overrides it, which
        is how the human ``shell`` subcommand opens the escape hatch without
        changing what a policy may do.
    permitted:
        Extra verbs to release from the blocklist (``--allow chmod``). Widening is
        additive and auditable; it is not a way to install a new profile.
    """

    profile: GuardProfile = EXPLORE_PROFILE
    dry_run: bool = True
    deny_raw_shell: bool | None = None
    permitted: frozenset[str] = field(default_factory=frozenset)
    operator_mode: bool = False
    #: Every refusal or override, for the run summary. A guard that quietly lets
    #: things through is indistinguishable from one that was never asked.
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # `profile=OPERATOR_PROFILE` and `operator_mode=True` are the same intent
        # written two ways, so the flag is derived here rather than trusted to
        # every caller to set both. A Guard built with only the profile must not
        # silently run as a half-override -- blocked by dry-run, waived by
        # nothing, and no error to explain it.
        if self.profile.name == OPERATOR_PROFILE_NAME:
            self.operator_mode = True
        unknown = self.permitted & NEVER_PERMITTED_IN_PROFILE
        if unknown and not self.operator_mode:
            # --allow can widen a profile; it cannot install a new one. Wiping a
            # filesystem is left to the operator flag, which also logs loudly.
            raise GuardViolation(
                f"--allow cannot release {', '.join(sorted(unknown))} outside operator mode; "
                "those are device-destroying, not task-destructive"
            )

    # ------------------------------------------------------------- config

    @property
    def raw_shell_denied(self) -> bool:
        if self.deny_raw_shell is not None:
            return self.deny_raw_shell and not self.operator_mode
        return not self.profile.allow_policy_raw_shell and not self.operator_mode

    def widened(self, verbs: list[str] | tuple[str, ...]) -> "Guard":
        """Return a copy of this guard with extra verbs released."""
        return replace(self, permitted=frozenset(self.permitted) | frozenset(verbs))

    def describe(self) -> str:
        """One-line summary of the active ruleset, for the CLI banner."""
        bits = [f"profile={self.profile.name}", f"dry_run={str(self.dry_run).lower()}"]
        if self.permitted:
            # "released" rather than "allow+=", because the banner is read by
            # someone deciding whether this run is safe to walk away from.
            bits.append("released: " + ", ".join(sorted(self.permitted)))
        if self.operator_mode:
            bits.append("OPERATOR MODE: no gating")
        if self.profile.note:
            bits.append(f"({self.profile.note})")
        return " ".join(bits)

    # --------------------------------------------------------- entrypoints

    def check_typed(self, effect: Effect, description: str) -> Verdict:
        """Vet an action the dispatcher already knows how to build.

        Typed actions can only ever be READ or WRITE, so the interesting logic
        is the dry-run gate -- and in operator mode, nothing at all.
        """
        if self.dry_run and effect >= Effect.WRITE:
            reason = f"dry-run: {description} was not dispatched"
            self.log.append(reason)
            return Verdict(False, effect, reason, (), deferred=True)
        return Verdict(True, effect, "", ())

    def check_shell(self, command: str) -> Verdict:
        """The *policy* entry point: may this caller compose a shell string at all?

        Kept separate from :meth:`check_command` because the two questions have
        different owners. Who is allowed to build a command string is a property of
        the caller (a policy may not, a human at a keyboard may); whether a given
        command is permitted is a property of the command. Folding them together
        made every content question answer "refused", which reads like a policy
        failure and is actually just an unopened door.
        """
        if self.raw_shell_denied:
            reason = "raw shell is disabled for policies"
            self.log.append(reason)
            return Verdict(False, Effect.WRITE, reason, (), objection=reason)
        return self.check_command(command)

    def check_command(self, command: str) -> Verdict:
        """Vet a device-side shell string on its content alone.

        Classify first, then object, then gate. Keeping those steps apart is what
        lets operator mode report what it waived: the effect and the objection are
        both computed even though only one of them decides the outcome.
        """
        verdict = self._classify_only(command)

        if self.operator_mode:
            # Judge against the strict reference ruleset, not an empty one. An
            # operator profile whose blocklist is empty has nothing to waive and
            # would report a clean run after `rm -rf /sdcard` -- which makes
            # "unguarded" synonymous with "unauditable", the one property a
            # no-blocklist run must not lose. The dry-run axis is separate and
            # still applies, so `--i-am-the-operator --dry-run` plans loudly.
            if self.dry_run and verdict.effect >= Effect.WRITE:
                reason = "dry-run: write not dispatched"
                self.log.append(reason)
                return Verdict(False, verdict.effect, reason, verdict.argv, deferred=True, objection=reason)
            objection = self._objection(command, verdict.effect, profile=REFERENCE_PROFILE)
            if objection is not None:
                reason, _, waivable = objection
                if not waivable:
                    self.log.append(reason)
                    return Verdict(False, verdict.effect, reason, verdict.argv, objection=reason)
                self.log.append(f"operator override: {reason}")
                return Verdict(True, verdict.effect, "", verdict.argv, overridden=True, objection=reason)
            return verdict

        objection = self._objection(command, verdict.effect)
        if objection is None:
            return verdict
        reason, deferred, _ = objection
        self.log.append(reason)
        return Verdict(False, verdict.effect, reason, verdict.argv, deferred=deferred, objection=reason)

    def require_command(self, command: str) -> Verdict:
        """Like :meth:`check_command` but raises on refusal."""
        verdict = self.check_command(command)
        if not verdict.allowed:
            raise GuardViolation(f"guard blocked command ({verdict.reason}): {command}")
        return verdict

    def require_shell(self, command: str) -> Verdict:
        """Like :meth:`check_shell` but raises on refusal."""
        verdict = self.check_shell(command)
        if not verdict.allowed:
            raise GuardViolation(f"guard blocked command ({verdict.reason}): {command}")
        return verdict

    # ------------------------------------------------------------ internals

    def _objection(
        self, command: str, effect: Effect, profile: GuardProfile | None = None
    ) -> tuple[str, bool, bool] | None:
        """Return ``(reason, deferred, waivable)`` for the content of ``command``.

        ``deferred`` marks the refusal as mode-driven (dry-run) rather than
        rule-driven. ``waivable`` separates the two kinds of objection: a profile
        objecting to a *capability* is the operator's call to overrule, whereas a
        command that cannot be parsed or is empty is a malformed request, and
        waiving that would just send nothing to the device while claiming it ran.
        """
        text = (command or "").strip()
        if not text:
            return "empty command", False, False

        for token in _UNPARSEABLE:
            if token in text:
                return f"unauditable shell construct {token!r}", False, True
        for pattern, why in (profile or self.profile).enforced:
            if pattern.search(text):
                return f"destructive pattern: {why}", False, True
        try:
            segments = self._split_segments(text)
        except ValueError as exc:
            return f"cannot tokenise command: {exc}", False, False
        active = profile or self.profile
        tolerated = active.tolerated_destructive | frozenset(self.permitted)
        for segment in segments:
            if not segment:
                continue
            verb = self._lead_verb(segment)
            # The effect comes from _classify_segment, which sees through a
            # read-only-looking verb: `find /sdcard -delete` classifies as
            # DESTRUCTIVE even though "find" itself is a read. Gating on the verb
            # name alone would let that through.
            if self._classify_segment(segment) is Effect.DESTRUCTIVE and verb not in tolerated:
                return f"destructive operation in profile {active.name!r}: {verb or segment[0]}", False, True
        if profile is None and self.dry_run and effect >= Effect.WRITE:
            return "dry-run: write not dispatched", True, True
        return None

    def _classify_only(self, command: str) -> Verdict:
        """Parse and assign an effect, ignoring whether it is permitted."""
        text = (command or "").strip()
        if not text:
            return Verdict(True, Effect.READ, "", ())
        try:
            segments = self._split_segments(text)
        except ValueError as exc:
            return Verdict(True, Effect.DESTRUCTIVE, f"unparseable ({exc})", ())

        effect = Effect.READ
        for segment in segments:
            if not segment:
                continue
            segment_effect = self._classify_segment(segment)
            if segment_effect > effect:
                effect = segment_effect
        return Verdict(True, effect, "", tuple(segments[0]) if segments else ())

    @staticmethod
    def _split_segments(text: str) -> list[list[str]]:
        """Split on shell control operators, then tokenise each piece.

        Splitting *before* tokenising is what stops ``ls && rm -rf /`` from being
        classified as a single harmless ``ls`` command. Enforced in every profile
        including operator mode, because classification quality is what the trace
        is made of.
        """
        pieces = re.split(r"&&|\|\||;|\|", text)
        return [shlex.split(piece) for piece in pieces if piece.strip()]

    @staticmethod
    def _lead_verb(segment: list[str]) -> str:
        """The executable name for a segment, normalised for lookup.

        ``/system/bin/rm`` and ``mkfs.ext4`` both have to land on a set member:
        the first by taking the basename, the second by dropping the filesystem
        suffix. Without the suffix rule, ``mkfs.ext4 /dev/block/mmcblk0p1`` --
        the single most destructive command anyone could type here -- classifies
        as an unknown binary, i.e. merely a write, in every profile.
        """
        for token in segment:
            if re.fullmatch(r"\w+=\S*", token):  # FOO=bar at the head of a line
                continue
            name = token.rsplit("/", 1)[-1].lower()
            return name
        return ""

    @classmethod
    def _matches(cls, verb: str, names: frozenset[str]) -> bool:
        """Membership test that also accepts the ``family.variant`` spelling."""
        if not verb:
            return False
        if verb in names:
            return True
        # mkfs.ext4 -> mkfs, losetup.a -> losetup. Only the first dot, so a real
        # script name like `run.py` still has to match on its own basename.
        head = verb.split(".", 1)[0]
        return head != verb and head in names

    @classmethod
    def _classify_segment(cls, segment: list[str]) -> Effect:
        """Classify one pipeline segment by its leading non-env-assignment token."""
        verb = cls._lead_verb(segment)
        if not verb:
            return Effect.WRITE  # nothing recognisable: assume mutating

        if cls._matches(verb, DESTRUCTIVE_TOKENS):
            return Effect.DESTRUCTIVE
        if verb == "pm":
            # `pm list packages` is a read; `pm install`/`clear` are not.
            return Effect.READ if len(segment) > 1 and segment[1].startswith(("list", "path", "dump", "resolve")) else Effect.WRITE
        if verb == "settings":
            # `settings get`/`show` are reads; `put`/`delete` are not. Same
            # subcommand-aware logic as pm -- the verb alone would misfile every
            # read-only settings query as a write.
            return Effect.READ if len(segment) > 1 and segment[1] in {"get", "show"} else Effect.WRITE
        if verb == "ime":
            return Effect.READ if len(segment) > 1 and segment[1] == "list" else Effect.WRITE
        if verb in ("am", "cmd", "svc", "input"):
            return Effect.WRITE
        if verb == "find":
            # `find ... -delete` and `-exec rm` never appear as their own segment,
            # so check the segment text explicitly.
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
    if verdict.allowed and verdict.overridden:
        return f"override [{verdict.effect}] {verdict.objection}"
    if verdict.allowed:
        return f"allow [{verdict.effect}]"
    tag = "defer" if verdict.deferred else "block"
    return f"{tag} [{verdict.effect}] {verdict.reason}"


def guard_from_config(config: object) -> Guard:
    """Build the guard an :class:`~cloudultron.config.ExecutorConfig` describes.

    Takes the config structurally rather than importing it: ``safety`` must stay
    a leaf of the import graph, and the layering test enforces that. Duck-typing
    here buys library users the same profile behaviour the CLI gets, so
    ``Executor(device, policy, config=cfg)`` cannot silently run the strict
    defaults when the config asked for a lab profile.
    """
    name = getattr(config, "guard_profile", None) or EXPLORE_PROFILE.name
    profile = resolve_profile(name)
    # Selecting the profile and setting the flag mean the same thing; keeping two
    # spellings in sync by hand is how you end up with half an override.
    operator_mode = bool(getattr(config, "operator_mode", False)) or profile.name == OPERATOR_PROFILE_NAME
    return Guard(
        profile=profile,
        dry_run=bool(getattr(config, "dry_run", True)),
        permitted=frozenset(getattr(config, "allow_verbs", ()) or ()),
        operator_mode=operator_mode,
    )
