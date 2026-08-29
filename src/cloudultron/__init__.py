"""ULTRON-family Android UI automation harness.

Public surface, in the order you will probably need it::

    from cloudultron import ExecutorConfig, build_device, Executor
    from cloudultron import Action, Guard, Effect

The three layers are independent by design:

* :mod:`cloudultron.adb` -- how a command reaches a device.
* :mod:`cloudultron.ui` -- how a screen becomes comparable data.
* :mod:`cloudultron.loop` -- decide one action per observation, with tripwires.

Any of them is useful alone: the parser works on a saved dump with no device,
and :class:`cloudultron.loop.engine.Executor` works against a fake one.
"""

from __future__ import annotations

from .config import DEFAULT_REMOTE_SERIAL, ExecutorConfig
from .errors import (
    AdbCommandFailed,
    CloudultronError,
    DeviceError,
    DeviceTimeout,
    GuardViolation,
    HierarchyUnavailable,
    PolicyError,
    TransportError,
    TransportUnavailable,
)
from .loop.actions import Action, Dispatcher, Op
from .loop.engine import Executor, RunReport, StepOutcome, StepRecord, wait_for_stable
from .loop.policy import ExplorePolicy, NullPolicy, Observation, Policy, ScriptedPolicy
from .safety import (
    EXPLORE_PROFILE,
    OPERATOR_PROFILE,
    PROFILES,
    TEST_LAB_PROFILE,
    Effect,
    Guard,
    GuardProfile,
    Verdict,
    guard_from_config,
    resolve_profile,
)
from .ui.hashing import Diff, LoopDetector, compare, content_hash, structure_hash
from .ui.model import Rect, Screen, UiNode
from .ui.parser import parse_hierarchy
from .ui.render import render_digest, render_tree

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # config
    "ExecutorConfig",
    "DEFAULT_REMOTE_SERIAL",
    # errors
    "CloudultronError",
    "TransportError",
    "TransportUnavailable",
    "AdbCommandFailed",
    "DeviceTimeout",
    "DeviceError",
    "HierarchyUnavailable",
    "GuardViolation",
    "PolicyError",
    # safety
    "Guard",
    "GuardProfile",
    "Effect",
    "Verdict",
    "PROFILES",
    "EXPLORE_PROFILE",
    "TEST_LAB_PROFILE",
    "OPERATOR_PROFILE",
    "resolve_profile",
    "guard_from_config",
    # ui
    "Screen",
    "UiNode",
    "Rect",
    "parse_hierarchy",
    "structure_hash",
    "content_hash",
    "compare",
    "Diff",
    "LoopDetector",
    "render_digest",
    "render_tree",
    # loop
    "Executor",
    "RunReport",
    "StepRecord",
    "StepOutcome",
    "wait_for_stable",
    "Action",
    "Op",
    "Dispatcher",
    "Policy",
    "Observation",
    "NullPolicy",
    "ScriptedPolicy",
    "ExplorePolicy",
    # factory
    "build_device",
    "build_transport",
]


def build_transport(config: ExecutorConfig):
    """Real :class:`~cloudultron.adb.transport.AdbTransport` from a config."""
    from .adb.transport import AdbTransport

    return AdbTransport(
        serial=config.serial,
        adb_path=config.adb_path,
        timeout=config.command_timeout,
        retries=config.transport_retries,
        backoff=config.retry_backoff,
    )


def build_device(config: ExecutorConfig | None = None, *, mock: bool = False):
    """Convenience constructor for ``(device, transport)``.

    ``mock=True`` builds the fake, so a notebook or CI run needs no adb at all::

        device, _ = build_device(ExecutorConfig(dry_run=True), mock=True)
    """
    config = config or ExecutorConfig()
    if mock:
        from .testing.fake import FakeTransport

        transport = FakeTransport()
    else:
        transport = build_transport(config)
    from .adb.device import AndroidDevice

    return AndroidDevice(transport, timeout=config.command_timeout), transport
