# Team-owned bank mock scaffold

This Worker is deliberately unconfigured. `GET /healthz` returns 200 for process liveness. `GET /readyz` and every bank route return 503 until the final bank contract, case data, and handlers are wired. Do not deploy this placeholder as the final mock.

## Complete when the final package arrives

1. Implement the four exports in `src/bank.mjs`: set `enabled` to `true`, load the final OpenAPI contract in `loadContract`, load final case data in `loadData`, and implement `handleBankRequest(request, { env, ctx, contract, data, operationPath })` to match every required operation and response exactly. `operationPath` is the path from the OpenAPI contract, after removing any configured base prefix. Keep secrets and personal data out of committed files; use appropriate Cloudflare bindings or secrets if the final package requires them.
2. Add operation-level tests from the final OpenAPI and cases. Confirm writes have the persistence and readback behavior required by the organizer. The scaffold does not assume operation names, payloads, or scoring rules.
3. Run `npm ci`, `npm test`, and `npm run check:bundle` from this directory. Start local development with `npx wrangler dev`, then check that `/readyz` returns 200 and exercise each final bank endpoint locally.
4. Check Cloudflare authentication with `npx wrangler whoami`. Deploy only the completed mock with `npx wrangler deploy`. Confirm the deployed `/readyz` and operation responses. Pass its HTTPS **service base URL** as the second positional argument to the final CLI; that CLI appends each path from the effective OpenAPI contract. Keep the Worker live until results are available.

The recommended service base URL is the Worker origin with no trailing slash, such as `https://build-the-builder-bank-mock.<account>.workers.dev`. With the default `MOCK_BASE_PATH: ""`, a contract path `/get_account` reaches `/get_account`. If a prefix is required, set `MOCK_BASE_PATH` to a leading-slash path such as `/mock`, deploy, and give the CLI `https://build-the-builder-bank-mock.<account>.workers.dev/mock`; the same contract path then reaches `/mock/get_account`. `/healthz` and `/readyz` remain at the origin root. If the team account needs a different Worker name or a custom domain, update `wrangler.jsonc` before deployment. No bank endpoint is supplied by this scaffold.
