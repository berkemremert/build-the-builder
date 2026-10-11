"""Schema-driven product item mappings for changed get_account responses."""

from __future__ import annotations

from pathlib import Path
import json
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from account_item_bindings import product_item_fields  # noqa: E402
from tests.probe_changed_brief import synthetic_api, synthetic_policy, _code  # noqa: E402
from workflow_generator import build_workflow  # noqa: E402
from qa import validate_workflow  # noqa: E402


class ProductItemFieldsTests(unittest.TestCase):
    def test_public_and_synthetic_item_fields(self) -> None:
        standard = synthetic_api()["paths"]["/get_account"]["post"]["responses"]["200"]\
            ["content"]["application/json"]["schema"]["properties"]["products"]["items"]
        self.assertEqual({"product_id": "product_id", "kind": "type", "past_due": "past_due",
                          "days_late": "days_late", "floors": ["floor_amount"], "card_last4": None},
                         product_item_fields(standard))

        renamed = {"type": "object", "properties": {
            "account_ref": {"type": "string"}, "product_kind": {"type": "string"},
            "arrears_total": {"type": "number"}, "delinquency_days": {"type": "integer"},
            "required_payment": {"type": "number"},
        }}
        self.assertEqual({"product_id": "account_ref", "kind": "product_kind",
                          "past_due": "arrears_total", "days_late": "delinquency_days",
                          "floors": ["required_payment"], "card_last4": None},
                         product_item_fields(renamed))

    def test_missing_or_ambiguous_core_fields_fail_closed(self) -> None:
        base = {"type": "object", "properties": {
            "product_id": {"type": "string"}, "type": {"type": "string"},
            "past_due": {"type": "number"}, "days_late": {"type": "integer"},
            "floor_amount": {"type": "number"},
        }}
        for missing in ("product_id", "type", "past_due", "days_late", "floor_amount"):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                item = {**base, "properties": {k: v for k, v in base["properties"].items() if k != missing}}
                product_item_fields(item)
        duplicate = {**base, "properties": {**base["properties"], "arrears_total": {"type": "number"}}}
        with self.assertRaises(ValueError):
            product_item_fields(duplicate)

    def test_generated_account_prep_uses_renamed_fields(self) -> None:
        api = synthetic_api()
        item = (api["paths"]["/get_account"]["post"]["responses"]["200"]
                ["content"]["application/json"]["schema"]["properties"]["products"]["items"])
        item["properties"] = {
            "account_ref": {"type": "string"}, "product_kind": {"type": "string"},
            "arrears_total": {"type": "number"}, "delinquency_days": {"type": "integer"},
            "required_payment": {"type": "number"},
        }
        workflow = build_workflow(synthetic_policy(), api)
        self.assertEqual([], validate_workflow(workflow, api))
        state = {"now": "2031-04-16T10:00", "account_products": [{
            "account_ref": "a7", "product_kind": "reserve_account", "arrears_total": 87,
            "delinquency_days": 4, "required_payment": 21,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        product = json.loads(state["product_json"])[0]
        self.assertEqual({"product_id": "a7", "label": "reserve account", "past_due": 87.0,
                          "floor": 21.0, "days_late": 4, "band": "",
                          "deadline": "2031-04-24", "attempts": 2}, product)
        state = {"now": "2031-04-16T10:00", "account_products": [{
            "account_ref": "a7", "product_kind": "reserve_account", "arrears_total": 87,
            "required_payment": 21,
        }]}
        exec(_code(workflow, "prepare_account"), {"vars": state})
        self.assertEqual([], json.loads(state["product_json"]))
        self.assertEqual("0", state["product_count"])


if __name__ == "__main__":
    unittest.main()
