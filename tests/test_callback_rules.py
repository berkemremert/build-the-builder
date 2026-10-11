"""Source-grounded callback date and time boundaries."""

import json
from pathlib import Path
import unittest

from callback_rules import (callback_guard_code, callback_request_fields,
                            callback_window, validate_callback)
from policy_compiler import compile_policy
from probe_changed_brief import synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import build_workflow


PUBLIC = ("A callback is for today or the next business day, from 10:00 to 19:00, "
          "both included, and later than now.")
NOW = "2026-10-14T11:00"


class CallbackRulesTests(unittest.TestCase):
    def test_public_boundaries(self):
        window = callback_window(PUBLIC)
        self.assertEqual({"business_days": 1, "start": "10:00", "end": "19:00"}, window)
        for day, time in (("2026-10-14", "11:01"), ("2026-10-15", "19:00")):
            with self.subTest(day=day, time=time):
                validate_callback(day, time, NOW, [], (0, 1, 2, 3, 4), window)
        for day, time in (("2026-10-14", "10:59"), ("2026-10-14", "11:00"),
                          ("2026-10-15", "19:01"), ("2026-10-16", "12:00"),
                          ("2026-10-17", "12:00")):
            with self.subTest(day=day, time=time), self.assertRaises(ValueError):
                validate_callback(day, time, NOW, [], (0, 1, 2, 3, 4), window)

    def test_holiday_skips_and_amended_window(self):
        source = PUBLIC + "\nLater memo: callbacks are for today or the next two business days, from 09:00 to 17:00, both included."
        window = callback_window(source)
        self.assertEqual({"business_days": 2, "start": "09:00", "end": "17:00"}, window)
        validate_callback("2026-10-19", "17:00", NOW, ["2026-10-15"], (0, 1, 2, 3, 4), window)
        with self.assertRaises(ValueError):
            validate_callback("2026-10-15", "12:00", NOW, ["2026-10-15"], (0, 1, 2, 3, 4), window)
        amended = callback_window(PUBLIC + "\nLater memo: callback hours are now 09:00–17:00, both included.")
        self.assertEqual({"business_days": 1, "start": "09:00", "end": "17:00"}, amended)

    def test_missing_or_ambiguous_source_fails_closed(self):
        self.assertIsNone(callback_window("No callback rule supplied."))
        with self.assertRaisesRegex(ValueError, "both the day horizon and time window"):
            callback_window("A callback is for today or the next business day.")
        with self.assertRaisesRegex(ValueError, "endpoints"):
            callback_window("Callbacks are for today or the next business day, from 10:00 to 19:00.")

    def test_request_fields_and_generated_guard(self):
        fields = callback_request_fields({"properties": {
            "customer_id": {"type": "string"}, "callback_day": {"type": "string", "format": "date"},
            "callback_slot": {"type": "string", "description": "HH:MM"}}})
        self.assertEqual({"date": "callback_day", "time": "callback_slot"}, fields)
        hook = callback_guard_code("callback_day", "callback_slot", [], (0, 1, 2, 3, 4), callback_window(PUBLIC))
        exec(hook, {"config": {"body": {"callback_day": "2026-10-14", "callback_slot": "11:01"}},
                    "vars": {"now": NOW}})
        with self.assertRaises(ValueError):
            exec(hook, {"config": {"body": {"callback_day": "2026-10-20", "callback_slot": "23:00"}},
                        "vars": {"now": NOW}})

    def test_public_generator_guards_before_bank_call(self):
        root = Path(__file__).resolve().parents[1]
        api = json.loads((root / "tools" / "openapi.json").read_text())
        policy = compile_policy([str(root / "briefs" / "brief-a.md")],
                                str(root / "tools" / "openapi.json"), None)
        workflow = build_workflow(policy, api)
        self.assertEqual([], validate_workflow(workflow, api))
        tool = next(tool for tool in workflow["nodes"]["third_party"]["tools"]
                    if tool["function"]["name"] == "schedule_callback")
        hook = tool["pre_process_code"]
        for day, time, valid in (("2026-10-14", "10:59", False),
                                 ("2026-10-14", "11:01", True),
                                 ("2026-10-15", "19:00", True),
                                 ("2026-10-16", "12:00", False),
                                 ("2026-10-15", "19:01", False),
                                 ("2026-10-17", "12:00", False)):
            with self.subTest(day=day, time=time):
                context = {"config": {"body": {"date": day, "time": time}},
                           "vars": {"now": NOW}}
                if valid:
                    exec(hook, context)
                else:
                    with self.assertRaises(ValueError):
                        exec(hook, context)

    def test_renamed_api_fields_and_missing_source_window(self):
        api = synthetic_api()
        body = api["paths"]["/schedule_callback"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        body["properties"]["preferred_date"] = body["properties"].pop("callback_day")
        body["properties"]["preferred_time"] = body["properties"].pop("callback_slot")
        body["required"] = ["preferred_date" if name == "callback_day" else
                            "preferred_time" if name == "callback_slot" else name
                            for name in body["required"]]
        policy = synthetic_policy()
        workflow = build_workflow(policy, api)
        tool = next(tool for tool in workflow["nodes"]["third_party"]["tools"]
                    if tool["function"]["name"] == "schedule_callback")
        with self.assertRaisesRegex(ValueError, "not established"):
            exec(tool["pre_process_code"], {"config": {"body": {
                "preferred_date": "2031-04-16", "preferred_time": "12:00"}},
                "vars": {"now": "2031-04-16T11:00"}})
        policy["raw_brief"] += "\nCallbacks are for today or the next business day, from 10:00 to 19:00, both included."
        workflow = build_workflow(policy, api)
        tool = next(tool for tool in workflow["nodes"]["third_party"]["tools"]
                    if tool["function"]["name"] == "schedule_callback")
        exec(tool["pre_process_code"], {"config": {"body": {
            "preferred_date": "2031-04-16", "preferred_time": "12:00"}},
            "vars": {"now": "2031-04-16T11:00"}})


if __name__ == "__main__":
    unittest.main()
