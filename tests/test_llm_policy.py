"""Tests for the model-backed policy.

No test here touches the network: every case drives :class:`LLMPolicy` through
its ``poster`` seam with canned provider replies. That is the whole point of the
seam -- prompt construction, reply parsing, retry accounting and the fallback
policy are all observable without an API key, and the HTTP function that *does*
need a socket is the one piece kept small enough to read at a glance.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from tests.support import config, fake_device

from cloudultron.errors import PolicyError
from cloudultron.loop.actions import Action, Op
from cloudultron.loop.engine import Executor, TerminalReason
from cloudultron.loop.llm import (
    ANTILOOP_RULES,
    DEFAULT_MODEL,
    KEY_ENV,
    MODEL_ENV,
    RESPONSE_CONTRACT,
    SYSTEM_PROMPT,
    LLMPolicy,
    LoopMemory,
    extract_json,
)
from cloudultron.loop.policy import HistoryEntry, Observation
from cloudultron.testing.fake import FakeTransport
from cloudultron.ui.hashing import content_hash, structure_hash
from cloudultron.ui.render import render_digest

from cloudultron.ui.parser import parse_hierarchy

from tests.support import SAMPLE_DUMP


def observation(**overrides):
    """A realistic mid-run observation with four addressable elements."""
    screen = parse_hierarchy(SAMPLE_DUMP)[0]
    # Same call the engine makes, so a test cannot pass on a digest format the
    # engine would never have produced.
    digest, indexed = render_digest(screen, focused_window="com.foo/com.foo.LoginActivity")
    base = dict(
        step=3,
        screen=screen,
        digest=digest,
        diff_summary="back button appeared",
        diff_level="structure",
        focused_window="com.foo/com.foo.LoginActivity",
        history=(),
        steps_remaining=7,
        hints=(),
        structure_hash=structure_hash(screen),
        content_hash=content_hash(screen),
        extra={"indexed_count": len(indexed)},
    )
    base.update(overrides)
    return Observation(**base)


def reply(obj) -> str:
    """A provider body carrying ``obj`` as the assistant's message content."""
    content = obj if isinstance(obj, str) else json.dumps(obj)
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]})


class StubProvider:
    """A stand-in for the HTTP call: scripted ``(status, body)`` pairs."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.slept: list[float] = []

    def __call__(self, url, headers, body, timeout):
        index = len(self.requests)
        self.requests.append(
            {"url": url, "headers": dict(headers), "body": json.loads(body.decode()), "timeout": timeout}
        )
        if index < len(self.responses):
            return self.responses[index]
        return self.responses[-1]

    @property
    def sent(self):
        return len(self.requests)

    @property
    def bodies(self):
        return [r["body"] for r in self.requests]

    def user_prompt(self, index=0) -> dict:
        return json.loads(self.requests[index]["body"]["messages"][1]["content"])


def policy_with(*responses, **kwargs) -> tuple[LLMPolicy, StubProvider]:
    provider = StubProvider(responses)
    kwargs.setdefault("model", DEFAULT_MODEL)
    kwargs.setdefault("base_url", "https://openrouter.ai/api/v1")
    kwargs.setdefault("api_key", "sk-or-secret-abcdefgh")
    kwargs.setdefault("sleeper", lambda seconds: provider.slept.append(seconds))
    kwargs["poster"] = provider
    return LLMPolicy(**kwargs), provider


class EnvironmentTests(unittest.TestCase):
    """Which env vars are read, and what happens when they are absent."""

    def setUp(self):
        self._saved = {k: __import__("os").environ.get(k) for k in (KEY_ENV, MODEL_ENV, "OPENROUTER_BASE_URL")}
        for key in self._saved:
            __import__("os").environ.pop(key, None)

    def tearDown(self):
        import os

        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_default_model_is_a_free_tier_id_and_not_the_auto_router(self):
        # The whole reason the default exists: `openrouter/auto` picks a model by
        # price/quality, so a fresh checkout would both not know what it is
        # talking to and start paying for it.
        self.assertEqual(DEFAULT_MODEL, "meta-llama/llama-3.1-70b-instruct:free")
        self.assertNotEqual(DEFAULT_MODEL, "openrouter/auto")
        self.assertTrue(DEFAULT_MODEL.endswith(":free"))

    def test_missing_key_is_a_construction_error_naming_the_variable(self):
        # Raised before any device work or HTTP, so a misconfigured run fails in a
        # millisecond with an instruction instead of after three retries.
        with self.assertRaises(PolicyError) as ctx:
            LLMPolicy.from_env()
        message = str(ctx.exception)
        self.assertIn(KEY_ENV, message)
        self.assertNotIn("sk-or", message)

    def test_key_and_model_come_from_the_environment(self):
        import os

        os.environ[KEY_ENV] = "sk-or-from-env"
        os.environ[MODEL_ENV] = "some/lab/model-2:free"
        policy = LLMPolicy.from_env()
        self.assertEqual(policy.api_key, "sk-or-from-env")
        self.assertEqual(policy.model, "some/lab/model-2:free")

    def test_an_empty_model_variable_falls_back_to_the_default(self):
        # Compose passes ${LLM_MODEL-} which yields an empty string, not an unset
        # variable, in every container that has no .env -- the default has to
        # survive that.
        import os

        os.environ[KEY_ENV] = "sk-or-x"
        os.environ[MODEL_ENV] = ""
        self.assertEqual(LLMPolicy.from_env().model, DEFAULT_MODEL)

    def test_explicit_arguments_override_the_environment(self):
        import os

        os.environ[KEY_ENV] = "sk-or-env"
        os.environ[MODEL_ENV] = "env/model:free"
        self.assertEqual(LLMPolicy.from_env(model="cli/model:free").model, "cli/model:free")

    def test_base_url_override_changes_the_endpoint(self):
        import os

        os.environ[KEY_ENV] = "sk-or-env"
        os.environ["OPENROUTER_BASE_URL"] = "http://127.0.0.1:8000/v1/"
        self.assertEqual(LLMPolicy.from_env().base_url, "http://127.0.0.1:8000/v1")

    def test_the_key_never_appears_in_repr_describe_or_prompt(self):
        policy, provider = policy_with((200, reply({"action": "done"})))
        secret = policy.api_key
        self.assertNotIn(secret, repr(policy))
        self.assertNotIn(secret, policy.describe())
        self.assertNotIn(secret, json.dumps(policy.build_prompt(observation())))
        # ...but it must be on the wire, as a bearer token.
        policy.decide(observation())
        self.assertEqual(provider.requests[0]["headers"]["Authorization"], f"Bearer {secret}")

    def test_describe_reports_a_paid_model_without_refusing_it(self):
        # Choosing a billed model is the operator's call; hiding it is not.
        free, _ = policy_with((200, reply({"action": "done"})))
        paid = replace(free, model="anthropic/claude-opus-4.1")
        self.assertIn("free tier", free.describe())
        self.assertIn("billed", paid.describe())


class PromptTests(unittest.TestCase):
    """What the model is told, and whether it is told the truth."""

    def test_prompt_carries_the_observation_and_the_goal(self):
        obs = observation()
        policy, provider = policy_with((200, reply({"action": "done"})), goal="log in as test user")
        prompt = policy.build_prompt(obs)
        sent = json.loads(prompt["messages"][1]["content"])
        for key in obs.to_prompt_dict():
            self.assertIn(key, sent, f"to_prompt_dict() field {key!r} must reach the model")
        self.assertEqual(sent["goal"], "log in as test user")
        self.assertEqual(sent["focused_window"], "com.foo/com.foo.LoginActivity")

    def test_every_antiloop_rule_and_the_output_contract_are_in_the_system_prompt(self):
        for rule in ANTILOOP_RULES + RESPONSE_CONTRACT:
            self.assertIn(rule, SYSTEM_PROMPT)
        self.assertIn("structure hash", SYSTEM_PROMPT)
        self.assertIn("nothing else", SYSTEM_PROMPT)

    def test_hints_from_the_engine_are_forwarded_verbatim(self):
        # The engine's tripwires already produce sentences written for whoever is
        # deciding; paraphrasing them into the prompt would let the two drift.
        obs = observation(hints=("oscillation detected between two screens",))
        policy, _ = policy_with((200, reply({"action": "done"})))
        self.assertIn("oscillation detected between two screens", policy.build_prompt(obs)["messages"][1]["content"])

    def test_memory_counts_only_actions_that_reached_the_device(self):
        obs = observation(structure_hash="a" * 40)
        dead = HistoryEntry(step=1, action="tap_index(index=1)", outcome="executed chg=none", structure_hash="a" * 40)
        blocked = HistoryEntry(step=2, action="tap_index(index=2)", outcome="blocked: raw shell disabled", structure_hash="a" * 40)
        planned = HistoryEntry(step=3, action="tap_index(index=3)", outcome="planned (dry-run)", structure_hash="a" * 40)
        memory = LoopMemory.from_observation(replace(obs, history=(dead, blocked, planned)))
        self.assertEqual(sorted(memory.spent_for_this_screen), ["tap_index(index=1)"])
        self.assertIn("a" * 12, memory.visited[0])
        # A blocked step is a backfire; a dry-run "planned" step is not. Counting
        # the latter would make every preview run look like a fight with the guard.
        self.assertEqual(memory.backfires, 1, memory.to_dict())
        refused = replace(obs, history=(HistoryEntry(step=4, action="x", outcome="refused: index out of range", structure_hash="a" * 40),))
        self.assertEqual(LoopMemory.from_observation(refused).backfires, 1)

    def test_a_screen_that_absorbed_two_different_actions_is_reported_dead(self):
        hash_ = "b" * 40
        entries = (
            HistoryEntry(step=1, action="tap_index(index=1)", outcome="executed chg=none", structure_hash=hash_),
            HistoryEntry(step=2, action="tap_index(index=2)", outcome="executed chg=none", structure_hash=hash_),
        )
        memory = LoopMemory.from_observation(observation(structure_hash=hash_, history=entries))
        self.assertIn(hash_[:12], memory.dead_screens)

    def test_a_rejected_reply_is_explained_in_the_next_prompt(self):
        policy, provider = policy_with(
            (200, reply({"action": "teleport"})),
            (200, reply({"action": "back"})),
        )
        with self.assertRaises(PolicyError):
            policy.decide(observation())
        policy.decide(observation())
        self.assertIn("unknown action", provider.user_prompt(1)["rejected_last_reply"])
        self.assertNotIn("rejected_last_reply", provider.user_prompt(0), "the first turn has nothing to atone for")

    def test_temperature_is_zero_and_the_reply_budget_is_bounded(self):
        # A decider that is also a storyteller needs both a leash and a fixed
        # temperature, or the run is not reproducible from the trace.
        policy, provider = policy_with((200, reply({"action": "back"})), max_tokens=128)
        policy.decide(observation())
        body = provider.requests[0]["body"]
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["max_tokens"], 128)
        self.assertEqual(body["model"], DEFAULT_MODEL)


class ReplyParsingTests(unittest.TestCase):
    """Tolerating the ways a model decorates a JSON object."""

    def test_plain_object(self):
        self.assertEqual(extract_json('{"action":"back"}'), {"action": "back"})

    def test_fenced_block(self):
        # Needs no dedicated code path -- covered here so that a future "simplify"
        # of the scanner cannot break fences without failing something.
        self.assertEqual(extract_json('```json\n{"action": "back"}\n```'), {"action": "back"})
        self.assertEqual(extract_json('```\n{"action": "back"}\n```'), {"action": "back"})

    def test_prose_before_and_after(self):
        text = 'Sure! Here is the next step:\n{"action": "tap_index", "index": 2}\nLet me know if that works.'
        self.assertEqual(extract_json(text)["index"], 2)

    def test_braces_inside_strings_do_not_move_the_depth_counter(self):
        # Balanced braces would survive a scanner that forgot about string state;
        # an unbalanced brace inside a quoted rationale would not. This is the
        # version that fails if the quote tracking is ever removed.
        parsed = extract_json('{"rationale": "tap the { brace-shaped button", "action": "back"}')
        self.assertEqual(parsed["action"], "back")
        parsed = extract_json('{"rationale": "tap {here}", "action": "back"}')
        self.assertEqual(parsed["action"], "back")

    def test_an_escaped_quote_inside_a_rationale_does_not_end_the_string(self):
        # Quotes inside a model's own explanation are the common case, not the
        # exotic one. Without escape tracking the scan closes the string at \" and
        # then treats the tail of the sentence as structure -- which is only
        # harmless until someone puts a brace in it.
        parsed = extract_json('{"rationale": "press the \\"Log in\\\" button", "action": "back"}')
        self.assertEqual(parsed["action"], "back")
        self.assertIn("Log in", parsed["rationale"])
        # And the case that *requires* the tracking rather than surviving without
        # it: a brace sitting between two escaped quotes, as when a model quotes a
        # format string at itself. Toggled quote parity cannot cancel that out --
        # a scanner that ignores escapes counts the brace and reports a truncation
        # that never happened.
        # Balanced braces inside the string are not enough -- a scanner that lost
        # track still happens to close correctly when the stray braces cancel out.
        # An *unbalanced* brace after an escaped quote is what forces the escape
        # tracking to exist: without it the object never closes and the reply is
        # reported as truncated, which is a lie about a perfectly good answer.
        quoting = extract_json('{"rationale": "quote it: \\" { then close", "action": "home"}')
        self.assertEqual(quoting["action"], "home")
        self.assertIn("{", quoting["rationale"])

    def test_an_empty_reply_is_named_as_such(self):
        with self.assertRaises(PolicyError) as ctx:
            extract_json("   ")
        self.assertIn("empty reply", str(ctx.exception))

    def test_a_brace_in_the_prose_before_the_object_is_ignored(self):
        self.assertEqual(extract_json('Try {this}: {"action": "back"}')["action"], "back")

    def test_no_object_at_all(self):
        with self.assertRaises(PolicyError):
            extract_json("I cannot decide right now.")

    def test_truncated_object_says_so(self):
        with self.assertRaises(PolicyError) as ctx:
            extract_json('{"action": "tap_index", "index": ')
        self.assertIn("truncated", str(ctx.exception))

    def test_finish_reason_length_is_not_reported_as_a_bad_decision(self):
        body = json.dumps({"choices": [{"message": {"content": '{"action": "tap"},'}, "finish_reason": "length"}]})
        policy, _ = policy_with((200, body))
        with self.assertRaises(PolicyError) as ctx:
            policy.decide(observation())
        self.assertIn("max_tokens", str(ctx.exception))

    def test_content_parts_list_is_joined(self):
        body = json.dumps({"choices": [{"message": {"content": [{"text": '{"action": '}, {"text": '"back"}'}]}}]})
        policy, _ = policy_with((200, body))
        self.assertEqual(policy.decide(observation()).op, Op.BACK)

    def test_provider_error_body_becomes_the_message(self):
        policy, _ = policy_with((401, json.dumps({"error": {"message": "No auth provided"}})))
        with self.assertRaises(PolicyError) as ctx:
            policy.decide(observation())
        self.assertIn("No auth provided", str(ctx.exception))
        self.assertIn("key was rejected", str(ctx.exception))


class ActionMappingTests(unittest.TestCase):
    """JSON in, validated :class:`Action` out -- and no policy-side gating."""

    def decide(self, payload, **kwargs):
        policy, _ = policy_with((200, reply(payload)), **kwargs)
        return policy.decide(observation())

    def test_the_common_shapes(self):
        self.assertEqual(self.decide({"action": "tap_index", "index": 1}).op, Op.TAP_INDEX)
        self.assertEqual(self.decide({"action": "point", "x": 10, "y": 20}).args["x"], 10)
        self.assertEqual(self.decide({"action": "scroll", "direction": "up"}).args["direction"], "up")
        self.assertEqual(self.decide({"action": "text", "value": "hello"}).args["value"], "hello")
        self.assertEqual(self.decide({"action": "launch_app", "package": "com.foo"}).args["package"], "com.foo")
        self.assertEqual(self.decide({"action": "done", "rationale": "login visible"}).op, Op.DONE)
        self.assertEqual(self.decide({"action": "abort", "rationale": "no route"}).op, Op.ABORT)
        self.assertEqual(self.decide({"action": "noop"}).op, Op.NOOP)

    def test_a_rationale_survives_into_the_trace(self):
        action = self.decide({"action": "back", "rationale": "the dialog has no accept button"})
        self.assertEqual(action.rationale, "the dialog has no accept button")
        self.assertEqual(action.source, "llm", "a model-chosen step must be attributable to the model")

    def test_rationale_is_capped(self):
        action = self.decide({"action": "back", "rationale": "x" * 4000})
        self.assertLessEqual(len(action.rationale), 240, "one model's monologue must not be the trace's payload")

    def test_stringified_numbers_are_accepted(self):
        self.assertEqual(self.decide({"action": "tap", "index": "2"}).args["index"], 2)

    def test_key_names_resolve_to_keycodes(self):
        # `lstrip("KEYCODE_")` used to eat the E of ENTER as well: a prefix must be
        # removed as a prefix.
        self.assertEqual(self.decide({"action": "keyevent", "code": "KEYCODE_ENTER"}).args["code"], 66)
        self.assertEqual(self.decide({"action": "keyevent", "code": "keycode_esc"}).args["code"], 111)
        self.assertEqual(self.decide({"action": "keyevent", "code": 4}).args["code"], 4)
        with self.assertRaises(PolicyError):
            self.decide({"action": "keyevent", "code": "KEYCODE_SLIDE"})

    def test_wait_is_clamped_to_something_a_run_can_afford(self):
        self.assertEqual(self.decide({"action": "wait", "seconds": 600}).args["seconds"], 10.0)

    def test_unknown_action_lists_the_vocabulary(self):
        with self.assertRaises(PolicyError) as ctx:
            self.decide({"action": "teleport"})
        self.assertIn("tap_index", str(ctx.exception))

    def test_an_index_off_the_digest_is_refused_before_dispatch(self):
        count = observation().extra["indexed_count"]
        self.assertGreater(count, 1, "the fixture must expose addressable elements")
        with self.assertRaises(PolicyError) as ctx:
            self.decide({"action": "tap_index", "index": count + 5})
        self.assertIn(f"0..{count - 1}", str(ctx.exception))

    def test_negative_and_non_numeric_indices_are_refused(self):
        for bad in (-1, "three", None, 1.5):
            with self.assertRaises(PolicyError, msg=f"index={bad!r} must not become an action"):
                self.decide({"action": "tap_index", "index": bad})

    def test_missing_arguments_say_which(self):
        with self.assertRaises(PolicyError) as ctx:
            self.decide({"action": "swipe", "x1": 1, "y1": 2})
        self.assertIn("x2", str(ctx.exception))

    def test_a_model_proposing_shell_is_passed_through_not_blocked(self):
        # Deliberate: the profile decides. A policy-side block would be a second,
        # invisible ruleset, and the trace would lose the fact that a model asked.
        action = self.decide({"action": "raw_shell", "command": "dumpsys window | head -5"})
        self.assertEqual(action.op, Op.RAW_SHELL)
        self.assertEqual(action.args["command"], "dumpsys window | head -5")


class TransportTests(unittest.TestCase):
    """Retries, backoff, and not hammering an endpoint that is down."""

    def test_a_rate_limited_reply_is_retried_then_used(self):
        policy, provider = policy_with(
            (429, json.dumps({"error": {"message": "Too Many Requests"}})),
            (200, reply({"action": "back"})),
        )
        self.assertEqual(policy.decide(observation()).op, Op.BACK)
        self.assertEqual(policy.stats["requests"], 2)
        self.assertEqual(policy.stats["retries"], 1)

    def test_retry_after_on_the_wire_is_obeyed(self):
        policy, provider = policy_with(
            (429, '{"error":{"message":"slow down"},"retry_after":7}'),
            (200, reply({"action": "back"})),
        )
        policy.decide(observation())
        self.assertEqual(provider.slept, [7.0])

    def test_backoff_without_retry_after_grows_but_stays_short(self):
        policy, provider = policy_with(*[(429, "{}")] * 2, (200, reply({"action": "back"})), max_retries=2)
        policy.decide(observation())
        self.assertEqual(provider.slept, [0.5, 1.0])

    def test_retries_are_bounded(self):
        policy, provider = policy_with(*[(503, "unavailable")] * 10, max_retries=2)
        with self.assertRaises(PolicyError):
            policy.decide(observation())
        self.assertEqual(provider.sent, 3, "one attempt plus max_retries, then stop")

    def test_persistent_failure_is_counted_and_reraised_as_a_policy_error(self):
        policy, _ = policy_with((500, json.dumps({"error": {"message": "boom"}})), max_retries=0)
        with self.assertRaises(PolicyError):
            policy.decide(observation())
        self.assertEqual(policy.stats["failures"], 1)

    def test_a_dead_endpoint_stops_being_called(self):
        # Without this, every step of a 25-step run spends a full retry budget
        # rediscovering that the network is down.
        policy, provider = policy_with(*[(500, "boom")] * 40, max_retries=2)
        for _ in range(3):
            with self.assertRaises(PolicyError):
                policy.decide(observation())
        before = provider.sent
        with self.assertRaises(PolicyError) as ctx:
            policy.decide(observation())
        self.assertEqual(provider.sent, before, "give up locally rather than after another timeout")
        self.assertIn("consecutive provider failures", str(ctx.exception))

    def test_json_mode_is_dropped_once_when_the_endpoint_rejects_it(self):
        # Free-tier open-weight endpoints routinely 400 on `response_format`; the
        # run should continue rather than die on a capability the harness does not
        # actually depend on.
        policy, provider = policy_with(
            (400, json.dumps({"error": {"message": "response_format is not supported by this model"}})),
            (200, reply({"action": "back"})),
        )
        self.assertEqual(policy.decide(observation()).op, Op.BACK)
        self.assertIn("response_format", provider.requests[0]["body"])
        self.assertNotIn("response_format", provider.requests[1]["body"])

    def test_timeout_is_passed_to_the_socket_layer(self):
        policy, provider = policy_with((200, reply({"action": "back"})), timeout=12.5)
        policy.decide(observation())
        self.assertEqual(provider.requests[0]["timeout"], 12.5)

    def test_an_unreachable_endpoint_raises_policy_error_not_urLError(self):
        # Nothing here has internet access, so this is the honest offline path:
        # it must be an error the engine can count, not a traceback.
        # No poster: this is the one case that exercises the real urllib call, so
        # it points at a closed port on loopback, which refuses instantly and needs
        # no internet -- the honest version of "what happens with no network".
        policy = LLMPolicy(model=DEFAULT_MODEL, base_url="http://127.0.0.1:1", api_key="sk-x", max_retries=0, timeout=2.0)
        with self.assertRaises(PolicyError) as ctx:
            policy.decide(observation())
        self.assertIn("cannot reach", str(ctx.exception))


class FallbackTests(unittest.TestCase):
    """What a provider outage costs a run, and whether the trace can see it."""

    def test_fallback_is_off_by_default(self):
        policy, _ = policy_with((500, "down"), max_retries=0)
        with self.assertRaises(PolicyError):
            policy.decide(observation())
        self.assertEqual(policy.stats["fallbacks"], 0)

    def test_the_step_is_spent_on_the_heuristic_and_labelled(self):
        obs = observation()
        policy, _ = policy_with((500, "down"), (200, reply({"action": "back"})), max_retries=0, use_fallback=True)
        action = policy.decide(obs)
        self.assertEqual(action.source, "fallback")
        self.assertIn("llm unavailable", action.rationale)
        self.assertIn(action.op, set(Op), "the fallback still has to produce a real action")
        self.assertEqual(policy.stats["fallbacks"], 1)
        # The next healthy turn must be attributed to the model again.
        self.assertEqual(policy.decide(obs).source, "llm")

    def test_fallback_does_not_keep_calling_a_dead_endpoint(self):
        policy, provider = policy_with(*[(500, "down")] * 40, max_retries=0, use_fallback=True)
        obs = observation()
        for _ in range(6):
            policy.decide(obs)
        self.assertLessEqual(provider.sent, 3, "it must stop trying, not retry every step forever")
        self.assertGreaterEqual(policy.stats["fallbacks"], 3)


class EngineIntegrationTests(unittest.TestCase):
    """The policy inside a real loop: provenance, the gate, and how it ends."""

    def run_loop(self, policy, *, dry_run=True, transport=None, overrides=None, steps=3):
        """``(engine, report, device)`` over the fake device with one seam swapped."""
        if transport is not None:
            from cloudultron.adb.device import AndroidDevice

            device = AndroidDevice(transport)
        else:
            device, _ = fake_device()
        cfg = config(dry_run=dry_run, max_steps=steps, **(overrides or {}))
        engine = Executor(device, policy, config=cfg)
        return engine, engine.run(max_steps=steps), device

    def test_a_model_chosen_step_is_attributed_to_the_llm(self):
        policy, _ = policy_with(
            (200, reply({"action": "tap_index", "index": 0, "rationale": "open the username field"})),
            (200, reply({"action": "done", "rationale": "the form is open"})),
        )
        engine, report, _ = self.run_loop(policy)
        self.assertEqual(report.terminal, TerminalReason.DONE)
        self.assertEqual(engine.steps[0].decision_source, "llm")
        self.assertIn("open the username field", engine.steps[0].rationale)

    def test_a_model_composed_command_is_the_guards_decision(self):
        # The same reply, two profiles: the default blocks it, an armed one runs
        # it. If a future edit moved the decision into the policy, both would
        # block and every unit test above would stay green.
        def outage_free_policy():
            made, _ = policy_with(
                (200, reply({"action": "raw_shell", "command": "reboot"})),
                (200, reply({"action": "done", "rationale": "finished"})),
            )
            return made

        preview_transport = FakeTransport()
        _, preview_report, _ = self.run_loop(outage_free_policy(), dry_run=False, transport=preview_transport, steps=2)
        self.assertEqual(preview_report.blocked, 1)
        self.assertEqual([c for c in preview_transport.calls if c.startswith("reboot")], [])

        lab_transport = FakeTransport()
        _, lab_report, _ = self.run_loop(
            outage_free_policy(),
            dry_run=False,
            transport=lab_transport,
            overrides={"guard_profile": "test-lab"},
            steps=2,
        )
        self.assertIn("reboot", lab_transport.calls, "an armed profile must let a model's command through")
        self.assertEqual(lab_report.blocked, 0)

    def test_three_bad_replies_end_the_run_as_a_policy_failure(self):
        policy, _ = policy_with(*[(200, reply("I am thinking about it"))] * 5, max_retries=0)
        _, report, _ = self.run_loop(policy, steps=10)
        self.assertEqual(report.terminal, TerminalReason.POLICY_ERRORS)
        self.assertIn("policy raised", report.reason)

    def test_a_provider_outage_ends_the_run_rather_than_hanging(self):
        policy, provider = policy_with(*[(500, "gateway trouble")] * 20, max_retries=1)
        _, report, _ = self.run_loop(policy, steps=10)
        self.assertEqual(report.terminal, TerminalReason.POLICY_ERRORS)
        self.assertLessEqual(provider.sent, 9, "the retry budget is per step and bounded")

    def test_the_model_driven_run_plans_and_does_not_touch_the_device(self):
        # Dry-run default, asserted where it matters: an LLM policy must not
        # inherit "the script author reviewed this" semantics.
        policy, _ = policy_with(
            (200, reply({"action": "tap_index", "index": 1, "rationale": "press log in"})),
            (200, reply({"action": "done"})),
        )
        _, report, _ = self.run_loop(policy, dry_run=True)
        self.assertEqual(report.executed, 0)
        self.assertEqual(report.planned, 1)

    def test_fallback_steps_are_visible_in_the_trace(self):
        policy, _ = policy_with(
            (500, "provider down"),
            (200, reply({"action": "back"})),
            (200, reply({"action": "done"})),
            max_retries=0,
            use_fallback=True,
        )
        engine, report, _ = self.run_loop(policy, steps=4)
        sources = [step.decision_source for step in engine.steps]
        self.assertIn("fallback", sources)
        self.assertIn("llm", sources, "a recovered turn must be attributed to the model again")
        # One call per step, no retry storm; the give-up path is covered in
        # TransportTests, this asserts the *accounting* a reader of the trace sees.
        self.assertEqual(policy.stats["requests"], 3)

class RealHttpTests(unittest.TestCase):
    """The transport itself, against a loopback server.

    ``poster`` covers every decision the policy makes; it cannot cover ``_post``,
    which is the one function that talks to the world. So this starts a real HTTP
    server on 127.0.0.1 and checks the bytes on the wire: the Authorization header,
    the JSON body, and that an ``HTTPError`` status is turned into a counted failure
    with the provider's own message rather than a traceback. No DNS, no TLS, no key,
    and skipped cleanly where a sandbox forbids binding a socket.
    """

    class Handler:
        """Built per-request; see ``serve`` for how responses are wired up."""

    def setUp(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.requests: list[tuple[dict, bytes]] = []
        self.responses: list[tuple[int, bytes]] = []
        test = self

        class Endpoint(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                test.requests.append((dict(self.headers), body, self.path))
                status, payload = test.responses.pop(0) if test.responses else (200, b'{"choices":[]}')
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence the per-request stderr noise
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        except OSError as exc:  # pragma: no cover - environment-dependent
            self.skipTest(f"cannot bind a loopback socket here: {exc}")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        if getattr(self, "server", None) is not None:
            self.server.shutdown()
            self.server.server_close()

    def policy(self, *responses, **kwargs):
        self.responses = list(responses)
        kwargs.setdefault("api_key", "sk-or-loopback-secret")
        kwargs.setdefault("model", DEFAULT_MODEL)
        kwargs.setdefault("sleeper", lambda seconds: None)
        return LLMPolicy(base_url=f"http://127.0.0.1:{self.port}/v1", **kwargs)

    def test_the_request_is_what_the_openrouter_api_expects(self):
        policy = self.policy((200, json.dumps({"choices": [{"message": {"content": '{"action": "back"}'}}]}).encode()))
        self.assertEqual(policy.decide(observation()).op, Op.BACK)
        headers, body, path = self.requests[0]
        self.assertEqual(path, "/v1/chat/completions", "the base_url is a prefix, not the whole endpoint")
        self.assertEqual(headers["Authorization"], "Bearer sk-or-loopback-secret")
        self.assertEqual(headers["Content-Type"], "application/json")
        # OpenRouter's own attribution headers: optional for a call to work, and the
        # reason a project shows up on its leaderboard.
        self.assertEqual(headers["X-Title"], "cloudultron")
        sent = json.loads(body)
        self.assertEqual(sent["model"], DEFAULT_MODEL)
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user"])

    def test_a_429_becomes_one_retry_and_then_a_decision(self):
        # The HTTPError branch of _post -- reading the error body -- is the part a
        # stubbed poster cannot reach.
        policy = self.policy(
            (429, json.dumps({"error": {"message": "rate limited"}}).encode()),
            (200, json.dumps({"choices": [{"message": {"content": '{"action": "home"}'}}]}).encode()),
        )
        self.assertEqual(policy.decide(observation()).op, Op.HOME)
        self.assertEqual(policy.stats["retries"], 1)
        self.assertEqual(len(self.requests), 2)

    def test_a_rejected_key_surfaces_the_provider_sentence(self):
        policy = self.policy((401, json.dumps({"error": {"message": "No valid API key provided"}}).encode()), max_retries=0)
        with self.assertRaises(PolicyError) as ctx:
            policy.decide(observation())
        self.assertIn("No valid API key provided", str(ctx.exception))
        self.assertNotIn("sk-or-loopback-secret", str(ctx.exception))

    def test_an_unknown_model_is_reported_with_the_name_that_was_asked_for(self):
        policy = self.policy((404, b'{"error":{"message":"no endpoints found"}}'), max_retries=0, model="nonsense/xyz:free")
        with self.assertRaises(PolicyError) as ctx:
            policy.decide(observation())
        self.assertIn("nonsense/xyz:free", str(ctx.exception))


class UsageAccountingTests(unittest.TestCase):
    """What the run cost, according to the provider rather than to hope.

    The `:free` default is the mitigation; these tests are the receipt. They cover
    both states a real reply can be in, because "no cost reported" and "cost is
    zero" have to stay distinguishable in the record.
    """

    def test_tokens_and_cost_accumulate_across_steps(self):
        def body(tokens, cost):
            return {
                "choices": [{"message": {"content": '{"action": "back"}'}}],
                "usage": {"prompt_tokens": tokens, "completion_tokens": 5, "total_tokens": tokens + 5, "cost": cost},
                "model": DEFAULT_MODEL,
            }

        policy, _ = policy_with((200, json.dumps(body(100, 0.0001))), (200, json.dumps(body(50, 0.0002))))
        obs = observation()
        policy.decide(obs)
        policy.decide(obs)
        self.assertEqual(policy.usage["prompt_tokens"], 150)
        self.assertEqual(policy.usage["total_tokens"], 160)
        self.assertAlmostEqual(policy.usage["cost_usd"], 0.0003)
        self.assertIn("$0.000300", policy.summary())

    def test_a_server_without_usage_says_nothing_rather_than_free(self):
        policy, _ = policy_with((200, reply({"action": "back"})))
        policy.decide(observation())
        self.assertEqual(policy.usage, {}, "no cost_usd key at all, not a zero")
        self.assertNotIn("$", policy.summary())

    def test_a_different_serving_model_is_named_in_the_summary(self):
        payload = json.dumps({"choices": [{"message": {"content": '{"action": "back"}'}}], "model": "someone/elses-model"})
        policy, _ = policy_with((200, payload), model=DEFAULT_MODEL)
        policy.decide(observation())
        self.assertIn("served by someone/elses-model", policy.summary())

    def test_the_summary_counts_retries_failures_and_fallbacks(self):
        # Recovered after two 429s: retried, but nothing failed, and the line a
        # human reads must not blur the two.
        recovered, _ = policy_with((429, "{}"), (429, "{}"), (200, reply({"action": "back"})))
        self.assertEqual(recovered.decide(observation()).op, Op.BACK)
        self.assertEqual(recovered.stats["retries"], 2)
        self.assertIn("2 retried", recovered.summary())
        self.assertNotIn("failed", recovered.summary())

        broken, _ = policy_with((500, "down"), max_retries=0)
        with self.assertRaises(PolicyError):
            broken.decide(observation())
        self.assertIn("1 failed", broken.summary())

        helped, _ = policy_with((500, "down"), max_retries=0, use_fallback=True)
        helped.decide(observation())
        self.assertIn("1 on heuristic fallback", helped.summary())


class HygieneTests(unittest.TestCase):
    """Claims the file makes about itself, checked mechanically."""

    def test_the_vocabulary_covers_every_operation(self):
        from cloudultron.loop.llm import _ACTION_ALIASES

        reachable = set(_ACTION_ALIASES.values())
        unaddressable = {op for op in Op if op not in reachable}
        self.assertEqual(unaddressable, set(), f"ops a model can never ask for: {unaddressable}")

    def test_no_new_dependency_was_introduced(self):
        source = (__import__("pathlib").Path(__file__).resolve().parent.parent / "src/cloudultron/loop/llm.py").read_text()
        for line in source.splitlines():
            if line.startswith("import ") or (line.startswith("from ") and not line.startswith("from .")):
                root = line.split()[1].split(".")[0]
                self.assertIn(
                    root,
                    {"json", "os", "re", "time", "urllib", "dataclasses", "typing", "__future__"},
                    f"llm.py reached outside the standard library with: {line}",
                )

    def test_the_key_is_read_from_the_environment_only(self):
        # An `--api-key` flag would put the secret in argv, i.e. in `ps` output and
        # in the shell history of whoever typed it. This test is the reason the CLI
        # has no such flag even though it looks like an omission.
        import pathlib

        cli = pathlib.Path(__file__).resolve().parent.parent / "src/cloudultron/cli.py"
        text = cli.read_text()
        self.assertNotIn("--api-key", text)
        self.assertNotIn("OPENROUTER_API_KEY=", text)
        self.assertIn("LLMPolicy.from_env", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
