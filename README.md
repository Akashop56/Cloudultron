# Cloudultron

An Android UI automation **executor**: an observe → think → act loop with
structural state-diffing and a command guard that is enforced in code rather than
requested in a prompt.

Python 3.9+, **zero runtime dependencies** — the whole thing is `xml.etree`,
`hashlib`, `subprocess` and `dataclasses`, because it is meant to install on
Termux as happily as on a workstation.

```bash
PYTHONPATH=src python3 -m cloudultron doctor --mock    # no device needed
PYTHONPATH=src python3 -m cloudultron run --mock --policy explore
python3 run_tests.py                                    # 234 tests, stdlib only
```

---

## Why it is shaped this way

Four decisions carry most of the design, and each exists because the naive
version of it fails:

**1. Policies emit typed actions, not shell strings.**
The decision-maker returns `Action.tap(index=3)`. Only the dispatcher turns that
into `input tap 540 965`. So `rm -rf /sdcard` is not *forbidden* — it is
unrepresentable, which is a stronger property and needs less filtering. Raw
shell exists as one escape hatch (`Action.raw_shell`); whether a policy may use it
is one of the things a profile decides (see below), and every command that goes
through it is classified by argv token rather than by regex over a string.

**2. Every screen gets two hashes, not one.**
A screen is never byte-identical between reads: a clock ticks, a spinner adds a
node, a badge changes. Hash everything and every step looks like a transition, so
your loop never detects that it is stuck. Hash only geometry and you miss a login
screen that swapped in "wrong password" without moving a pixel.

| | ignores | answers | used for |
|---|---|---|---|
| `structure_hash` | text, content-desc | *is this a different screen?* | anti-loop trip, "did my tap do anything" |
| `content_hash` | nothing user-visible | *did anything the user can see change?* | stability waits, effect check |

**3. "The screen changed" is not the anti-loop signal.**
A ping-pong between two screens produces a legitimate change on *every step* —
each individual action worked — so change-detection declares it healthy forever.
The loop keeps three separate verdicts because they have different causes:

- `stagnation` — structure identical for N steps. The action had no effect.
- `oscillation` — the structure hash cycles with period 2..N, and a cycle needs at
  least two distinct screens: four identical hashes are stagnation. The policy
  needs a "do not come back here" rule; a frozen screen does not.
- `livelock` — the same `(screen, action)` **pair** recurs even while the screen
  changes. This one is a bug in *us*, and it is checked independently of dispatch,
  so it still fires in dry-run. `Action.key()` deliberately excludes the
  rationale, because the failure mode of an LLM policy is rewording the same
  bad idea every step while the check looks for an identical string.

**4. The guard is a dial, not a switch, and the dial has a floor.**
Whether a device is safe to point this at is a fact about the *device*, not about
this repository — so it cannot live in the code as a constant, and it cannot live
in the policy either, because the policy is the thing being checked. It is a
profile the operator selects, and it only ever relaxes what the strict default
permits. What no profile removes is classification and logging: a run that
executed `rm -rf /sdcard` while reporting nothing is a run you cannot read
afterwards. Deleting the check is what makes a copied file dangerous; arming it
off, in writing, on a device you own, is not.

---

## Layout

```
src/cloudultron/
  config.py     ExecutorConfig; env-var overrides (CLOUDULTRON_*, ANDROID_SERIAL)
  safety.py     Effect tiers (read < write < destructive) + Guard. The enforcement point.
  errors.py     raise on hard failure, return on policy outcome
  adb/
    transport.py  argv construction, quoting, exec-out, transient retries
    device.py     the verbs: hierarchy(), tap, swipe, text, keyevent, am/pm, focus
  ui/
    model.py      Rect, UiNode, Screen — incl. interactables() pruning
    parser.py     dump bytes -> tree; survives garbage, control bytes, truncation
    hashing.py    the two hashes, compare() -> Diff, LoopDetector
    render.py     compact index-addressable digest for a decision-maker
  loop/
    actions.py    the action vocabulary + Dispatcher (the only writer of state)
    policy.py     Policy protocol + Null / Scripted / Explore
    engine.py     the loop, tripwires, budgets, trace
  testing/fake.py a fake that answers adb, so the stack runs with no device
  cli.py          doctor | snapshot | run | shell | script
```

Import direction is one way and the test suite enforces it: `ui` depends on
nothing but `errors`, `adb` sits on `ui`, `loop` sits on `ui`+`safety` and **never**
on `adb` — that is what keeps the executor testable against the fake.
See `tests/test_hygiene.py`.

---

## The loop, one step

```
observe   uiautomator dump -> parse -> structure_hash, content_hash, current_focus
think     diff vs previous; LoopDetector.check(); budget check      <- before decide
decide    policy.decide(Observation) -> Action                       (expensive)
act       Guard.check_*  ->  deferred? record the plan : dispatch     (cheap, decisive)
          the *profile* decides what is refused here; it never decides what the
          command was -- classification runs first and unconditionally, so the
          trace says what a stricter profile would have stopped
settle    sleep only when something was actually put on the wire
          device.invalidate()  <- without this, a post-tap read can be served a
                                  pre-tap dump and every action looks like a no-op
```

Checks run *before* the policy call on purpose: a step that is going to trip is
not worth a model round-trip.

---

## CLI

| | mutates the device? |
|---|---|
| `doctor [--deep]` | no |
| `snapshot [--save f] [--tree] [--compare-with f]` | no |
| `run [--policy observe\|explore\|scripted] [--steps N]` | autonomous policy: only with `--execute`. script: by default |
| `shell 'dumpsys window'` | read-only unless `--execute` |
| `script FILE [--emit]` | no |

The default mode follows the **provenance of the actions**, not a global setting:

* `--policy explore` / `observe` — the actions came from a model or a wanderer that
  nobody reviewed, so dry-run stays on and `--execute` must be typed.
* `--policy scripted --script nav.txt` — you opened that file, so a replay
  dispatches by default; pass `--dry-run` to plan instead. Requiring `--execute`
  on every replay just teaches people to type it without reading.
* `--i-am-the-operator` — implies `--execute`, and says so on stderr first. So does
  naming the profile (`--guard-profile operator`, or the environment), because
  those are one decision written three ways; only an explicit `--dry-run` overrides
  it. `--guard-profile test-lab` does *not* imply execution: that profile changes
  what is blocked, not whether you meant to touch the device.

Every run prints the armed ruleset to stderr unless `-q`, so "which of these did
I set three flags ago" never needs memory.

```bash
# real device, local emulator
cloudultron run --serial emulator-5554 --policy explore --steps 20 --verbose

# adb over WiFi (host:port triggers `adb connect` for you)
cloudultron --serial 192.168.1.50:5555 snapshot

# record a replayable trace, then drive it in from CI
cloudultron run --execute --policy scripted --script nav.txt --record runs/ --json
```

Exit codes: `0` clean or step-budget reached, `3` a tripwire fired, `4` the guard
blocked/deferred, `1` device or transport error, `2` usage, `5` a shell command that
ran and failed, `130` interrupted.

---

## Guard profiles

The guard is one object with three settings. Pick the setting, not the code.

```bash
cloudultron run --guard-profile explore      # the default, and the strictest
cloudultron run --guard-profile test-lab     # a device you are paid to break
cloudultron run --i-am-the-operator          # nothing blocked; everything logged
```

| | what is refused | patterns enforced | a policy may compose shell |
|---|---|---|---|
| `explore` | every destructive verb (`rm`, `reboot`, `chmod`, `dd`, `mkfs`, …) | all of them | no |
| `test-lab` | the device-wrecking list only | always-on + `rm -rf` | yes, gated by content |
| `operator` | nothing | none (still classified) | yes |

`test-lab` exists for a farm device where `reboot`, `pm clear`, `am force-stop`,
`chmod`/`chown`, `settings put` and `pm uninstall` are Tuesday. It opens the raw
channel *because* a profile whose permitted verbs are unreachable from a run is
decoration; what stays shut is the content — `mkfs`, `fdisk`, `wipefs`, `fastboot`,
redirects into `/dev`, `curl … | sh`, `rm -rf` of a wide path, command
substitution. Those are refused in every profile except `operator`.

Two rules hold across all three:

1. **Classification is never optional.** Splitting on `&&`, `||`, `;` and `|`
   before tokenising means `ls && rm -rf /sdcard` is judged as the second segment,
   not the first. Operator mode turns off *gating*, not *reading*.
2. **Profiles only relax.** No named profile may block something the strict
   default allowed, so choosing one can never break a command that already worked.
   A test pins this.

`--allow VERB` releases one verb from the blocklist for a single invocation:

```bash
cloudultron run --guard-profile test-lab --allow rm --execute --policy explore
```

`--allow` widens a profile, it cannot install one: `--allow mkfs` is a usage error
outside operator mode, because a flag that reads like an exception should not be
the way you wipe a partition table.

### Clicking things the explorer would normally avoid

`--allow-label 'Buy now'` releases one label from the explorer's danger filter for
this suite. The filter is a heuristic for unsupervised wandering, not a security
boundary, so authorising a purchase or account-deletion flow you *meant* to test is
a suite-scope decision:

```bash
cloudultron run --policy explore --allow-label 'Buy now' --allow-label 'Delete account'
cloudultron run --policy explore --danger-label 'transfer'      # add bank-app words
cloudultron run --policy explore --no-danger-filter             # click anything addressable
```

Matching is substring on the label and extends rather than replaces, so adding
`transfer` does not quietly drop `delete`. `--no-danger-filter` is the blunt
version; prefer naming the labels.

### What operator mode guarantees you afterwards

Nothing is blocked, and that is the point, but the run is still legible: each
waived objection is written into the step (`operator override: <reason>`), counted
in the summary (`guard=operator(overrides=3)`), and repeated in `--json` under
`guard_log`. An unguarded run that cannot be read afterwards is not an operator
mode, it is just noise.

Environment equivalents for CI: `CLOUDULTRON_GUARD_PROFILE=test-lab`,
`CLOUDULTRON_OPERATOR=1`, `CLOUDULTRON_ALLOW_VERBS=chmod,rm`,
`CLOUDULTRON_ALLOW_LABELS=Buy now,Delete account`.

## Termux

```bash
pkg install python android-tools
adb connect <emulator-host>:5555 && adb devices        # or a USB OTG device
git clone <this repo> && cd Cloudultron
PYTHONPATH=src python3 -m cloudultron doctor --deep
```

`adb` must be reachable from the shell you run in; on non-rooted devices
`uiautomator dump` and `input` work without a pairing prompt, but `am start`
to another user's app and `dumpsys` of some services will not. Everything here
degrades to "cannot observe" as a clean `HierarchyUnavailable` rather than a
half-read tree.

---

## Extending

A new profile is data, not code: a name, a verb blocklist, and a subset of the
`PATTERNS` keys, registered in `PROFILES`. Nothing else in the codebase changes,
which is the test that the seam is in the right place — if adding a ruleset needs
an `if` in the engine, the engine knows too much.

A policy is two methods:

```python
from cloudultron import Action, Executor, ExecutorConfig, build_device, Guard

class MyPolicy:
    name = "mine"

    def decide(self, obs) -> Action:
        # obs.digest is a compact, index-addressable screen listing;
        # obs.diff_summary says what changed since last step;
        # obs.history is the last few (action, outcome) pairs.
        if "[focused]" in obs.digest:
            return Action.text("hello")
        return Action.tap(index=2, rationale="Continue button")

policy, cfg = MyPolicy(), ExecutorConfig(dry_run=True)
device, _ = build_device(cfg)
Executor(device, policy, config=cfg).run()          # guard is built from cfg
```

An LLM-backed policy is just `decide()` returning `json.loads(model(observation.to_prompt_dict()))`
mapped onto `Action` factories. `to_prompt_dict()` exists so the payload sent to a
model is bounded and does not include raw XML.

`guard=` is optional: pass `guard_from_config(cfg)` or nothing, and the executor
builds the guard the config describes. The guard does not need to know about a new
policy — it only needs to know whether
an action *touches*. That is the payoff of the typed-action boundary.

## Status

Working: transport, parsing, hashing, diffing, all three tripwires, the guard and
its three profiles, provenance-based dry-run gating, trace recording, the fake
device, the CLI, 234 tests.

Not built yet, in rough order of usefulness: screenshot/OCR fallback for
`FLAG_SECURE` and canvas views; `--record` writing replayable scripts as well as
traces; `ADBKeyboard` for non-ASCII input (`input text` cannot encode it, so we
refuse instead of typing mojibake); a real `LLMPolicy`; multi-device fan-out.
