"""Exception hierarchy for the Cloudultron executor.

The rule used here: a *hard failure* raises, a *policy outcome* is returned.

"adb is not installed" raises ``TransportUnavailable``. "The screen has not
changed for four steps" does not raise -- it is reported as a
``TerminalReason.LOOP`` in the run report, because stopping is the correct
behaviour, not an error.
"""

from __future__ import annotations


class CloudultronError(Exception):
    """Base class for every error raised by this package."""


# --- transport / adb -------------------------------------------------------


class TransportError(CloudultronError):
    """Something went wrong talking to the adb transport itself."""


class TransportUnavailable(TransportError):
    """The ``adb`` binary (or the requested device) is not reachable."""


class AdbCommandFailed(TransportError):
    """An adb invocation returned a non-zero exit status."""

    def __init__(self, argv: list[str], returncode: int, stderr: str = "") -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        shown = " ".join(argv)
        detail = f": {stderr.strip()[:400]}" if stderr.strip() else ""
        super().__init__(f"command failed (exit {returncode}) {shown}{detail}")


class DeviceTimeout(TransportError):
    """An adb invocation exceeded its deadline."""


# --- device-side failures --------------------------------------------------


class DeviceError(CloudultronError):
    """The device refused or failed to perform an operation."""


class HierarchyUnavailable(DeviceError):
    """``uiautomator dump`` produced nothing usable.

    Common real causes: secure window (FLAG_SECURE), a SurfaceView-only
    screen, uiautomator being wedged (fix: ``am force-stop com.android.shell``
    or reboot), or the dump racing an in-flight animation.
    """

    def __init__(self, message: str, raw_output: str = "") -> None:
        self.raw_output = raw_output
        super().__init__(message)


# --- guard / safety --------------------------------------------------------


class GuardViolation(CloudultronError):
    """The safety layer refused a command. Always fatal to that action.

    Deliberately not auto-retried: a blocked command stays blocked no matter
    how many times it is attempted, so retrying only wastes a step.
    """


# --- policy ----------------------------------------------------------------


class PolicyError(CloudultronError):
    """The decision-maker misbehaved (unknown index, malformed action)."""
