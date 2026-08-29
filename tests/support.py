"""Shared helpers for the suite.

The path bootstrap lives in ``run_tests.py`` (unittest) and ``conftest.py``
(pytest), so this module can assume ``cloudultron`` is importable and stay
concerned only with fixtures.
"""

from __future__ import annotations

from cloudultron.config import ExecutorConfig
from cloudultron.testing.fake import Element, FakeScreen, FakeTransport, build_demo_device


def fake_device(**device_kwargs):
    """``(AndroidDevice, FakeTransport)`` over the demo screen graph.

    Note the device is the *real* class: only the transport is fake, so these
    tests exercise the dump/read/parse path rather than a mock of it.
    """
    from cloudultron.adb.device import AndroidDevice

    transport = FakeTransport()
    return AndroidDevice(transport, **device_kwargs), transport


def config(**overrides) -> ExecutorConfig:
    """Config tuned for tests: no sleeps, no retries, no real device."""
    base = dict(
        dry_run=True,
        settle_delay=0.0,
        command_timeout=5.0,
        transport_retries=0,
        stability_timeout=0.3,
        max_steps=8,
        stagnation_limit=3,
        oscillation_max_period=3,
        policy_livelock_limit=3,
    )
    base.update(overrides)
    return ExecutorConfig(**base)


def one_screen_transport(screen: FakeScreen) -> FakeTransport:
    """Transport pinned to a single screen -- for parser/digest tests where
    navigation would only add noise."""
    return FakeTransport(screens={screen.name: screen}, start=screen.name)


def simple_screen(name: str = "form", **kwargs) -> FakeScreen:
    """A login-ish screen whose layout is stable but whose text can vary."""
    return FakeScreen(
        name=name,
        package="com.foo",
        activity="LoginActivity",
        elements=[
            Element(
                cls="android.widget.TextView",
                resource_id="com.foo:id/title",
                text=kwargs.get("title", "Sign in"),
                bounds=(60, 120, 500, 200),
                package="com.foo",
            ),
            Element(
                cls="android.widget.Button",
                resource_id="com.foo:id/submit",
                text=kwargs.get("button", "Log in"),
                bounds=(60, 700, 1020, 830),
                clickable=True,
                package="com.foo",
                taps_to=kwargs.get("taps_to"),
            ),
        ],
    )


SAMPLE_DUMP = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy index="0" class="hierarchy" rotation="0" window-size="1080x1920">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.foo" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][1080,1920]">
    <node index="0" text="Sign in" resource-id="com.foo:id/title" class="android.widget.TextView" package="com.foo" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[60,120][500,200]" />
    <node index="1" text="" resource-id="com.foo:id/user" class="android.widget.EditText" package="com.foo" content-desc="Username" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="true" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[60,300][1020,420]" />
    <node index="2" text="" resource-id="com.foo:id/pass" class="android.widget.EditText" package="com.foo" content-desc="Password" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="true" selected="false" bounds="[60,450][1020,570]" />
    <node index="3" text="Log in" resource-id="com.foo:id/submit" class="android.widget.Button" package="com.foo" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[60,700][1020,830]" />
    <node index="4" text="Forgot password?" resource-id="" class="android.widget.TextView" package="com.foo" content-desc="" checkable="false" checked="false" clickable="true" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[380,880][700,940]" />
    <node NAF="true" text="" resource-id="com.foo:id/dead" class="android.widget.ImageButton" package="com.foo" content-desc="" checkable="false" checked="false" clickable="true" enabled="false" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,1800][120,1920]" />
  </node>
</hierarchy>"""

#: The same tree with one title change and identical geometry.
SAMPLE_DUMP_RETEXTED = SAMPLE_DUMP.replace('text="Sign in"', 'text="Welcome back"')

#: The same tree with a moved button: geometry changes, element identity does not.
SAMPLE_DUMP_MOVED = SAMPLE_DUMP.replace('bounds="[60,700][1020,830]"', 'bounds="[60,900][1020,1030]"')

#: The same tree with an extra node appended (a banner appeared).
SAMPLE_DUMP_WITH_BANNER = SAMPLE_DUMP.replace(
    '  </node>\n</hierarchy>',
    '    <node index="9" text="No connection" resource-id="com.foo:id/banner" '
    'class="android.widget.TextView" package="com.foo" content-desc="" checkable="false" '
    'checked="false" clickable="false" enabled="true" focusable="false" focused="false" '
    'scrollable="false" long-clickable="false" password="false" selected="false" '
    'bounds="[0,220][1080,280]" />\n  </node>\n</hierarchy>',
)


__all__ = [
    "fake_device",
    "config",
    "one_screen_transport",
    "simple_screen",
    "build_demo_device",
    "SAMPLE_DUMP",
    "SAMPLE_DUMP_RETEXTED",
    "SAMPLE_DUMP_MOVED",
    "SAMPLE_DUMP_WITH_BANNER",
]
