"""Check enum variables in the JSON workflow handed to Freya."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from policy_compiler import compile_policy  # noqa: E402
from qa import validate_workflow  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402


class ExportedEnumSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api = json.loads((ROOT / "tools" / "openapi.json").read_text())
        policy = compile_policy([str(ROOT / "briefs" / "brief-a.md"),
                                 str(ROOT / "briefs" / "brief-b.md")],
                                str(ROOT / "tools" / "openapi.json"), None)
        cls.exported = json.loads(json.dumps(build_workflow(policy, cls.api)))

    def test_spoken_date_and_full_offer_flags_are_registered_in_export(self) -> None:
        required = {
            "multi_offer": {"multi_reply_date_spoken", "multi_fixed_offer_accepted",
                            "multi_offer_fact_question", "multi_offer_fact_kind",
                            "multi_offer_fact_product_id", "multi_offer_fact_date_words"},
            "multi_clarify": {"multi_reply_date_spoken", "multi_fixed_offer_accepted",
                              "multi_offer_fact_question", "multi_offer_fact_kind",
                              "multi_offer_fact_product_id", "multi_offer_fact_date_words"},
            "multi_offer_resume": {"multi_reply_date_spoken", "multi_fixed_offer_accepted",
                                   "multi_offer_fact_question", "multi_offer_fact_kind",
                                   "multi_offer_fact_product_id", "multi_offer_fact_date_words"},
            "multi_support": {"multi_reply_date_spoken", "multi_fixed_offer_accepted",
                              "multi_offer_fact_question", "multi_offer_fact_kind",
                              "multi_offer_fact_product_id", "multi_offer_fact_date_words"},
            "multi_disclosure_clarify": {"multi_reply_date_spoken"},
            "multi_correction": {"multi_correction_claim_action", "multi_correction_claim_product_id"},
            "account_discussion": {"multi_account_fact_question", "multi_account_fact_kind",
                                   "multi_account_fact_product_id"},
        }
        for node_name, names in required.items():
            fields = {field["name"]: field for field in
                      self.exported["nodes"][node_name]["extraction_schema"]}
            for name in names:
                with self.subTest(node=node_name, variable=name):
                    self.assertIn(name, fields)
                    if name in {"multi_reply_date_spoken", "multi_fixed_offer_accepted",
                                "multi_offer_fact_question", "multi_account_fact_question"}:
                        self.assertEqual({"yes", "no"}, set(fields[name]["enum_values"]))

    def test_exported_code_does_not_reset_enum_to_invalid_empty_string(self) -> None:
        self.assertEqual([], validate_workflow(self.exported, self.api))
        for variable in ("multi_reply_date_spoken", "multi_fixed_offer_accepted"):
            with self.subTest(variable=variable):
                broken = copy.deepcopy(self.exported)
                code_spec = (broken["nodes"]["multi_advance"]["pre_actions"][0]
                             ["function"]["parameters"]["properties"]["code"])
                code_spec["default"] = code_spec["default"].replace(
                    f"vars['{variable}'] = 'no'", f"vars['{variable}'] = ''",
                )
                errors = validate_workflow(broken, self.api)
                self.assertTrue(any(f"{variable}='' outside extraction enum" in error
                                    for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
