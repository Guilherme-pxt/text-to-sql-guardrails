"""The LLM provider, the generation parser, the LLM judge and the Jev client, all offline."""

import json
import math
import tempfile
import unittest
from pathlib import Path

from sqlguard.generate import messages, parse
from sqlguard.jev import BudgetExceeded, JevClient, JevError, load_api_key
from sqlguard.judges import (PRE, Routing, jev_pre_probs, jev_pre_request, llm_pre, name_matches, route_post,
                             route_pre)
from sqlguard.llm import CachedProvider, ChatResult, LLMError, OllamaProvider, ScriptedProvider, prob_of
from tests.helpers import FakeJev, answer_with, shared_db


class OllamaPayloadTest(unittest.TestCase):
    def test_shared_server_settings_on_every_request(self):
        p = OllamaProvider("qwen2.5-coder:7b")
        body = p.payload([{"role": "user", "content": "hi"}], 10, 0)
        self.assertEqual(body["keep_alive"], -1)
        self.assertEqual(body["options"]["num_ctx"], 24576)
        self.assertEqual(body["model"], "qwen2.5-coder:7b")
        self.assertEqual(body["options"]["temperature"], 0.0)
        self.assertNotIn("logprobs", body)
        self.assertEqual(p.payload([], 1, 10)["top_logprobs"], 10)


class CacheTest(unittest.TestCase):
    def test_hit_miss_and_replay_only(self):
        with tempfile.TemporaryDirectory() as d:
            inner = ScriptedProvider(lambda m: ChatResult("SELECT 1", 2.5))
            c = CachedProvider(inner, Path(d) / "c.sqlite")
            msgs = [{"role": "user", "content": "x"}]
            first, second = c.chat(msgs), c.chat(msgs)
            self.assertEqual((first.cached, second.cached), (False, True))
            self.assertEqual(second.latency_s, 2.5)   # the stored latency, not the replay time
            self.assertEqual(len(inner.calls), 1)
            c.allow_miss = False
            with self.assertRaises(LLMError):
                c.chat([{"role": "user", "content": "y"}])


class ParseTest(unittest.TestCase):
    def test_intents(self):
        self.assertEqual(parse("```sql\nSELECT 1;\n```"), ("sql", "SELECT 1;", ""))
        self.assertEqual(parse("SELECT 2")[0:2], ("sql", "SELECT 2"))
        self.assertEqual(parse("CLARIFY: which year?"), ("clarify", None, "which year?"))
        self.assertEqual(parse("REFUSE: personal data")[0], "refuse")
        self.assertEqual(parse("I cannot help with that.")[0], "empty")
        self.assertEqual(parse("```sql\nDELETE FROM x;\n```\n```sql\nSELECT 1;\n```")[1], "DELETE FROM x;\nSELECT 1;")

    def test_prompt_carries_policy(self):
        sys_prompt = messages("q")[0]["content"]
        self.assertIn("[restricted]", sys_prompt)
        self.assertIn("CLARIFY:", sys_prompt)
        self.assertIn("Revenue:", sys_prompt)


class JudgeTest(unittest.TestCase):
    def test_prob_of_reads_first_token(self):
        lp = [{"Yes": math.log(0.6), " yes": math.log(0.2), "No": math.log(0.2)}]
        self.assertAlmostEqual(prob_of(lp, {"yes": ["yes"], "no": ["no"]})["yes"], 0.8)
        self.assertEqual(prob_of([], {"yes": ["yes"], "no": ["no"]}, "no.")["yes"], 0.0)

    def test_llm_pre_asks_four_questions(self):
        def fn(msgs):
            u = msgs[-1]["content"]
            if "one letter" in u:
                return answer_with("C")
            if "clarifying" in u:
                return answer_with("yes", 0.7)
            return answer_with("no", 0.9)
        p = ScriptedProvider(fn)
        probs, lat, cached = llm_pre(p, "How many orders?", {})
        self.assertEqual(len(p.calls), 4)
        self.assertTrue(all(c["max_tokens"] == 1 for c in p.calls))
        self.assertEqual(probs["kind"], "personal_data")
        self.assertAlmostEqual(probs["clarify"], 0.7)
        self.assertAlmostEqual(lat, 0.4)

    def test_name_matches(self):
        db = shared_db()
        m = name_matches(db, "How many orders did Halvorsen place?")
        self.assertIn("2 customers", m["Halvorsen"])
        self.assertIn("1 customer:", name_matches(db, "Orders of Halvorsen Marine")["Halvorsen Marine"])
        self.assertIn("tier", name_matches(db, "tickets for the Pro")["Pro"])
        self.assertEqual(name_matches(db, "How many orders in 2025?"), {})

    def test_routing_order(self):
        r = Routing()
        base = {"read_data": 0.9, "kind": "read_data", "answerable": 0.9, "clarify": 0.1, "injection": 0.1}
        self.assertIsNone(route_pre(base, r))
        self.assertEqual(route_pre({**base, "read_data": 0.2, "kind": "personal_data"}, r)[0], "refuse")
        self.assertEqual(route_pre({**base, "injection": 0.6}, r)[0], "refuse")
        self.assertEqual(route_pre({**base, "answerable": 0.1}, r)[0], "refuse")
        self.assertEqual(route_pre({**base, "clarify": 0.6}, r)[0], "confirm")
        self.assertEqual(route_pre({**base, "answerable": 0.5}, r)[0], "confirm")
        self.assertEqual(route_post(0.2, r)[0], "confirm")
        self.assertEqual(route_post(0.8, r)[0], "execute")


class JevClientTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = {"JEV_API_KEY": "test-key-123"}

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, transport, **kw):
        return JevClient(Path(self.tmp.name) / "c.sqlite", transport=transport, env=self.env, sleep=lambda s: None,
                         **kw)

    def test_request_shape_cache_and_key_hygiene(self):
        fake = FakeJev(noul=0.2)
        c = self.client(fake)
        body = jev_pre_request("jev-1.14.0", "How many orders?", {})
        self.assertEqual(set(body["questions"]), set(PRE))
        r1, r2 = c.ask(body), c.ask(body)
        self.assertEqual(len(fake.requests), 1)
        self.assertTrue(r2.cached)
        self.assertEqual(fake.headers[0]["Authorization"], "Bearer test-key-123")
        probs = jev_pre_probs(r1)
        self.assertAlmostEqual(probs["read_data"], 0.9)
        self.assertAlmostEqual(probs["clarify"], 0.2)
        stored = c.db.execute("SELECT value FROM cache").fetchone()[0]
        self.assertNotIn("test-key-123", stored)
        self.assertNotIn("test-key-123", repr(c))
        self.assertNotIn("test-key-123", json.dumps(body))

    def test_retries_then_fails(self):
        fake = FakeJev(status=429)
        c = self.client(fake, max_attempts=3)
        with self.assertRaises(JevError):
            c.ask(jev_pre_request("jev-1.14.0", "q", {}))
        self.assertEqual(len(fake.requests), 3)

    def test_budget_cap(self):
        c = self.client(FakeJev(), max_usd=0.0)
        with self.assertRaises(BudgetExceeded):
            c.ask(jev_pre_request("jev-1.14.0", "q", {}))

    def test_key_file(self):
        Path(self.tmp.name, ".env.jev").write_text("JEV_API_KEY=abc\n")
        self.assertEqual(load_api_key(self.tmp.name, env={}), "abc")
        with self.assertRaises(JevError):
            load_api_key(Path(self.tmp.name) / "nowhere", env={})


if __name__ == "__main__":
    unittest.main()
