# Freya Bank test mock

This Cloudflare Worker serves the seven `POST /voice/v1/*` operations from the final service contract. Responses, HTTP statuses, and the one delayed response come from `data/test-customers.json`, copied from the final case package. The test environment contains synthetic customers. The Worker ignores authorization, headers, `call`, and `aiFlow` as specified by that package.

The service base URL is `https://build-the-builder-bank-mock.berkemremert.workers.dev`. The health routes are `GET /healthz` and `GET /readyz`. The seven operation paths are:

- `/voice/v1/cards.list`
- `/voice/v1/cards.balance`
- `/voice/v1/statement.week.get`
- `/voice/v1/statement.week.decide`
- `/voice/v1/statement.week.change`
- `/voice/v1/contact.email.get`
- `/voice/v1/statement.pdf.send`

Each operation accepts a JSON request with a `data` object. Customer numbers and requested weeks accept either strings or numbers. Card numbers must match the masked values in the case data exactly. A missing customer, card, or week receives the service's documented 404 or 400 error family. Successful `statement.week.change` and `statement.pdf.send` calls append an audit record to the customer's SQLite Durable Object. Responses remain the exact fixture bodies; the test environment defines fixed answers for subsequent reads.

Run `npm ci`, `npm test`, `npm run test:durable`, and `npm run check:bundle` here. `npm run test:durable` starts a local Cloudflare pool and needs permission to bind a loopback socket. Deploy with `npx wrangler deploy` after configuring credentials outside the repository. Keep `MOCK_STATE_SCOPE` in `wrangler.jsonc` at `validation` while validating writes. Before scoring, set a fresh scope, redeploy, check `/readyz` and a read-only request, and avoid scored-customer writes until the run starts.
