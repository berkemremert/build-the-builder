"""Offline regressions for source amendments and renamed API request fields."""

from __future__ import annotations

import copy
import json
import re
import unittest

from probe_changed_brief import _code, synthetic_api, synthetic_policy
from qa import validate_workflow
from workflow_generator import _holiday_dates, _outcome_names, _queue_for, build_workflow


class ChangedBriefGeneralityTests(unittest.TestCase):
    def test_opaque_table_codes_override_stale_extracted_outcomes(self) -> None:
        api = synthetic_api()
        policy = synthetic_policy()
        policy["raw_brief"] = re.sub(r"Code list:.*?\n", "", policy["raw_brief"])
        policy["raw_brief"] += ("\n| Purpose | Queue |\n|---|---|\n"
                                "| support | Q2 |\n| human | Q3 |\n| restructuring | Q1 |\n"
                                "| Role | Outcome code |\n|---|---|\n"
                                "| promise | C2 |\n| paid | C3 |\n| transfer | C4 |\n"
                                "| no promise | C5 |\n| identity failure | C6 |\n"
                                "| not reached | C1 |\n")
        policy["outcomes"] = {role: "C1" for role in policy["outcomes"]}
        workflow = build_workflow(policy, api)
        self.assertEqual([], validate_workflow(workflow, api))
        self.assertEqual({"promise": "C2", "paid": "C3", "transferred": "C4",
                          "no_promise": "C5", "not_verified": "C6", "not_reached": "C1"},
                         _outcome_names(api, policy))
        for role, code in (("support", "Q2"), ("human", "Q3"), ("restructuring", "Q1")):
            self.assertEqual(code, _queue_for(api, role, policy))
            queue = workflow["nodes"]["transfer_" + role]["pre_actions"][0]
            body = queue["function"]["parameters"]["properties"]["body"]["properties"]
            self.assertEqual(code, body["queue"]["default"])

    def test_opaque_queue_without_source_mapping_fails_closed(self) -> None:
        policy = synthetic_policy()
        policy["raw_brief"] = re.sub(r"Code list:.*?\n", "", policy["raw_brief"])
        with self.assertRaisesRegex(ValueError, "Cannot map support queue unambiguously"):
            _queue_for(synthetic_api(), "support", policy)

    def test_identity_failure_farewell_uses_changed_bank_name(self) -> None:
        workflow = build_workflow(synthetic_policy(), synthetic_api())
        farewell = workflow["nodes"]["farewell_identity_failed"]["on_enter"]["text"]
        self.assertEqual(
            "For your security, please use the Meridian Bank mobile app or visit a branch. Goodbye.",
            farewell,
        )

    def test_later_policy_and_api_names_drive_generated_actions(self) -> None:
        api = synthetic_api()
        policy = synthetic_policy(identity="Ask passcode first, then birth year.")
        workflow = build_workflow(policy, api)
        self.assertEqual([], validate_workflow(workflow, api))
        state = {"now": "2031-04-16T10:00", "account_products": [{
            "product_id": "a7", "type": "reserve_account", "past_due": 87,
            "days_late": 4, "floor_amount": 21,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        product = json.loads(state["product_json"])[0]
        self.assertEqual(("2031-04-24", 2), (product["deadline"], product["attempts"]))
        self.assertNotIn("2031-04-18", _code(workflow, "prepare_confirmation"))
        first_verify = workflow["nodes"]["identity_dob"]["tools"][0]
        first_field = first_verify["function"]["parameters"]["properties"]["body"]["properties"]["field"]["default"]
        self.assertEqual("passcode", first_field)
        self.assertIn("'C6'", _code(workflow, "finish_identity_failed"))
        support = workflow["nodes"]["transfer_support"]["pre_actions"][0]
        self.assertEqual("Q2", support["function"]["parameters"]["properties"]["body"]["properties"]["queue"]["default"])
        promise = workflow["nodes"]["create_promise_call"]["pre_actions"][0]
        body = promise["function"]["parameters"]["properties"]["body"]["properties"]
        self.assertEqual("{{pending_product_id}}", body["account_ref"]["default"])
        self.assertEqual("{{pending_amount}}", body["payment_total"]["default"])
        self.assertEqual("{{pending_date}}", body["settlement_day"]["default"])

    def test_qa_rejects_unbound_engine_promise_fields(self) -> None:
        api = synthetic_api()
        workflow = build_workflow(synthetic_policy(), api)
        broken = copy.deepcopy(workflow)
        action = broken["nodes"]["create_promise_call"]["pre_actions"][0]
        body = action["function"]["parameters"]["properties"]["body"]["properties"]
        body["payment_total"]["default"] = "{{payment_total}}"
        errors = validate_workflow(broken, api)
        self.assertTrue(any("payment_total.default" in error and "pending_amount" in error
                            for error in errors), errors)

    def test_later_source_code_list_overrides_stale_extracted_mapping(self) -> None:
        policy = synthetic_policy()
        policy["outcomes"]["not_verified"] = "C1"
        workflow = build_workflow(policy, synthetic_api())
        self.assertIn("'C6'", _code(workflow, "finish_identity_failed"))

    def test_renamed_account_response_collection_is_read_and_checked(self) -> None:
        api = synthetic_api()
        response = api["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        response["properties"]["accounts"] = response["properties"].pop("products")
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        self.assertEqual([{"path": "data.accounts", "variable": "account_products"}],
                         workflow["nodes"]["load_account"]["response_mapping"])
        state = {"now": "2031-04-16T10:00", "load_account_response.data.accounts": [{
            "product_id": "a7", "type": "reserve_account", "past_due": 87,
            "days_late": 4, "floor_amount": 21,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        self.assertEqual("1", state["product_count"])
        self.assertEqual("a7", json.loads(state["product_json"])[0]["product_id"])
        broken = copy.deepcopy(workflow)
        broken["nodes"]["load_account"]["response_mapping"][0]["path"] = "data.products"
        self.assertTrue(any("data.accounts" in error for error in validate_workflow(broken, api)))

    def test_explicit_opaque_close_reasons_and_invalid_code(self) -> None:
        api = synthetic_api()
        policy = synthetic_policy()
        policy["raw_brief"] += ("\nReason code list: failed=R1, declined=R2, "
                                "wrong_number=R3, service_error=R4, refused=R5.\n")
        workflow = build_workflow(policy, api)
        self.assertEqual([], validate_workflow(workflow, api))
        self.assertIn("'R1'", _code(workflow, "finish_identity_failed"))
        self.assertIn("'R3'", _code(workflow, "finish_wrong_number"))
        policy["raw_brief"] += "Reason code list: failed=R9.\n"
        with self.assertRaisesRegex(ValueError, "R9.*close_call"):
            build_workflow(policy, api)

    def test_reason_table_drives_close_and_note_codes(self) -> None:
        policy = synthetic_policy()
        policy["raw_brief"] += ("\n| Reason | Close code |\n|---|---|\n"
                                "| failed | R1 |\n| declined | R2 |\n"
                                "| wrong number | R3 |\n| service error | R4 |\n"
                                "| refused | R5 |\n"
                                "| Reason | Note code |\n|---|---|\n| refused | R5 |\n")
        workflow = build_workflow(policy, synthetic_api())
        self.assertEqual([], validate_workflow(workflow, synthetic_api()))
        self.assertIn("'R1'", _code(workflow, "finish_identity_failed"))
        self.assertIn("'R3'", _code(workflow, "finish_wrong_number"))
        self.assertIn("'R5'", _code(workflow, "prepare_notes"))

    def test_changed_success_sentence_is_used_and_missing_sentence_is_silent(self) -> None:
        policy = synthetic_policy()
        no_source = build_workflow(policy, synthetic_api())
        self.assertEqual("silent", no_source["nodes"]["promise_success_notice"]["on_enter"]["type"])
        policy["raw_brief"] += ('\nAfter recording the promise, tell the customer verbatim: '
                                '"A written confirmation will follow."\n')
        workflow = build_workflow(policy, synthetic_api())
        self.assertEqual({"type": "tts", "text": "A written confirmation will follow."},
                         workflow["nodes"]["promise_success_notice"]["on_enter"])
        self.assertEqual([], validate_workflow(workflow, synthetic_api()))

    def test_same_sentence_holiday_revoke_and_add_are_independent(self) -> None:
        policy = {"raw_brief": (
            "2031-04-18 is a bank holiday. Later memo: 2031-04-18 is no longer a bank holiday, "
            "and 2031-04-21 is now a bank holiday."
        )}
        self.assertEqual(["2031-04-21"], _holiday_dates(policy))


if __name__ == "__main__":
    unittest.main()
