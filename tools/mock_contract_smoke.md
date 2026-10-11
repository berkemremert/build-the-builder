# Deployed mock contract smoke check

Use this after the final OpenAPI contract, seeded test data, and deployed mock URL are available:

```sh
python3 tools/mock_contract_smoke.py \
  --contract /absolute/path/to/final-openapi.json \
  --cases /absolute/path/to/mock-smoke-cases.json \
  --base-url https://deployed-worker.example/v1
```

The base URL points to the server prefix before the OpenAPI paths. If the URL
has no path prefix, the validator uses the prefix in the contract's effective
`servers` entry. Remote URLs must use HTTPS. The check sends real requests and
may exercise writes declared in the cases, so use dedicated seeded customers.

The cases file is JSON. Give every OpenAPI operation at least one step whose
expected status is 2xx. Steps run in order within each scenario. A complete
synthetic example appears in `tests/test_mock_contract_smoke.py`.

```json
{
  "variables": {"customer_id": "SYNTHETIC-CUSTOMER-01"},
  "sensitive_values": ["{{private_token}}"],
  "scenarios": [
    {
      "variables": {"private_token": "SYNTHETIC-SECRET-01"},
      "steps": [
        {
          "operationId": "getCustomer",
          "path_params": {"customer_id": "{{customer_id}}"},
          "expect_status": 200,
          "expect": {"body.id": "{{customer_id}}"}
        }
      ]
    }
  ]
}
```

If an operation has no `operationId`, specify its `method` and `path` instead.
Steps may also supply `request` (JSON body), `headers`, and `query`. A reference
such as `{{steps.0.body.id}}` uses an earlier response from the same scenario.
An `expect` key names a dotted path under `body` or `status`; array indexes
are numeric path components. `expect_status` accepts an integer or a list of
integers.

The validator checks route and method coverage, request and response schemas,
status codes, field assertions, and whether any protected value appears in a
response. It prints only scenario and step numbers with generic results. It
never prints URLs, seed values, request bodies, or response bodies.
