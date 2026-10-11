# Deployed mock contract smoke check

Use this after the final OpenAPI contract, seeded test data, and deployed mock URL are available:

```sh
python3 tools/mock_contract_smoke.py \
  --contract /absolute/path/to/final-openapi.json \
  --cases /absolute/path/to/mock-smoke-cases.json \
  --base-url https://deployed-worker.example/v1
```

The base URL points to the server prefix before the OpenAPI paths and must
match each operation's effective OpenAPI `servers` URL. For the default Worker,
pass its origin without a trailing slash. If the contract paths start with
`/v1`, they supply that segment themselves. If the Worker has
`MOCK_BASE_PATH=/mock`, pass
`origin/mock` and set that as the contract server URL. Paths declared in the
contract are appended verbatim. Remote URLs must use HTTPS. The check sends
real requests and may exercise writes declared in the cases, so use dedicated
seeded customers. `/healthz` and `/readyz` are at the Worker origin root and
are outside the bank OpenAPI operations.

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
