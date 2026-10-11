import assert from 'node:assert/strict';
import { test } from 'node:test';
import worker, { createWorker } from '../src/index.mjs';

const call = (app, path, options = {}) => app.fetch(
  new Request(`https://mock.example${path}`, options), {}, {},
);

test('scaffold is alive but refuses readiness and bank calls', async () => {
  const health = await call(worker, '/healthz');
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { status: 'alive' });
  assert.equal(health.headers.get('Cache-Control'), 'no-store');

  const readiness = await call(worker, '/readyz');
  assert.equal(readiness.status, 503);
  assert.deepEqual(await readiness.json(), { ready: false });

  const bankCall = await call(worker, '/v1/get_account', { method: 'POST' });
  assert.equal(bankCall.status, 503);
  assert.deepEqual(await bankCall.json(), { error: 'bank_mock_not_configured' });
});

test('health routes reject other methods without exposing configuration', async () => {
  for (const path of ['/healthz', '/readyz']) {
    const response = await call(worker, path, { method: 'POST' });
    assert.equal(response.status, 405);
    assert.equal(response.headers.get('Allow'), 'GET');
  }
});

test('configured hooks receive contract, data, and the original request', async () => {
  const contract = { openapi: '3.0.3', paths: { '/lookup': { post: {} } } };
  const data = { accounts: [] };
  const app = createWorker({
    enabled: true,
    loadContract: async () => contract,
    loadData: async () => data,
    handleBankRequest: async (request, context) => {
      assert.equal(new URL(request.url).pathname, '/v1/lookup');
      assert.equal(context.contract, contract);
      assert.equal(context.data, data);
      return new Response('handled', { status: 202 });
    },
  });
  assert.equal((await call(app, '/readyz')).status, 200);
  const response = await call(app, '/v1/lookup', { method: 'POST' });
  assert.equal(response.status, 202);
  assert.equal(await response.text(), 'handled');
});

test('missing or failing hooks never make the bank service ready', async () => {
  const incomplete = createWorker({
    enabled: true,
    loadContract: async () => ({ paths: {} }),
    loadData: async () => null,
    handleBankRequest: async () => new Response('unexpected'),
  });
  assert.equal((await call(incomplete, '/readyz')).status, 503);
  assert.equal((await call(incomplete, '/anything')).status, 503);

  const failed = createWorker({
    enabled: true,
    loadContract: async () => { throw new Error('private contract error'); },
    loadData: async () => ({}),
    handleBankRequest: async () => new Response('unexpected'),
  });
  const response = await call(failed, '/readyz');
  assert.equal(response.status, 503);
  assert.doesNotMatch(await response.text(), /private contract error/);
});
