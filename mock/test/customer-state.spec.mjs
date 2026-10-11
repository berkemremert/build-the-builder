import { env } from 'cloudflare:workers';
import { evictDurableObject } from 'cloudflare:test';
import { expect, it } from 'vitest';
import { createWorker } from '../src/index.mjs';
import { createCustomerStateService } from '../src/state-client.mjs';

it('keeps writes across new calls and a Durable Object eviction', async () => {
  const firstRequest = createCustomerStateService(env).forCustomer('durable-customer');
  expect(await firstRequest.read('attempts')).toBeNull();
  expect(await firstRequest.increment('attempts')).toBe(1);
  expect(await firstRequest.append('promises', { amount: 120 })).toBe(1);
  expect(await firstRequest.write('closed', { outcome: 'complete' })).toBe(true);

  const secondRequest = createCustomerStateService(env).forCustomer('durable-customer');
  expect(await secondRequest.read('attempts')).toBe(1);
  expect(await secondRequest.read('promises')).toEqual([{ amount: 120 }]);
  expect(await secondRequest.read('closed')).toEqual({ outcome: 'complete' });

  await evictDurableObject(env.CUSTOMER_STATE.getByName(
    JSON.stringify(['customer-state-v1', env.MOCK_STATE_SCOPE, 'durable-customer']),
  ));
  const afterEviction = createCustomerStateService(env).forCustomer('durable-customer');
  expect(await afterEviction.read('attempts')).toBe(1);
  expect(await afterEviction.increment('attempts')).toBe(2);
});

it('routes different customers to independent durable state', async () => {
  const state = createCustomerStateService(env);
  const one = state.forCustomer('customer-one');
  const two = state.forCustomer('customer-two');
  await one.increment('attempts');
  await one.append('notes', { reason: 'example' });
  await two.increment('attempts');
  await two.increment('attempts');
  expect(await one.read('attempts')).toBe(1);
  expect(await two.read('attempts')).toBe(2);
  expect(await one.read('notes')).toEqual([{ reason: 'example' }]);
  expect(await two.read('notes')).toBeNull();
});

it('passes durable state into separate bank HTTP requests', async () => {
  const app = createWorker({
    enabled: true,
    loadContract: async () => ({ openapi: '3.0.3', paths: { '/verify': { post: {} } } }),
    loadData: async () => ({ customers: [] }),
    handleBankRequest: async (request, { state }) => {
      const id = new URL(request.url).searchParams.get('customer');
      const customer = state.forCustomer(id);
      const attempts = request.method === 'POST'
        ? await customer.increment('verification_attempts')
        : await customer.read('verification_attempts');
      return Response.json({ attempts });
    },
  });
  const call = (id, method) => app.fetch(
    new Request(`https://mock.example/verify?customer=${id}`, { method }), env, {},
  );
  expect(await (await call('http-one', 'POST')).json()).toEqual({ attempts: 1 });
  expect(await (await call('http-one', 'GET')).json()).toEqual({ attempts: 1 });
  expect(await (await call('http-two', 'POST')).json()).toEqual({ attempts: 1 });
  expect(await (await call('http-one', 'POST')).json()).toEqual({ attempts: 2 });
});

it('keeps concurrent counter updates atomic for one customer', async () => {
  const customer = createCustomerStateService(env).forCustomer('parallel-customer');
  await Promise.all(Array.from({ length: 12 }, () => customer.increment('attempts')));
  expect(await customer.read('attempts')).toBe(12);
});

it('keeps validation and scored scopes separate for the same customer ID', async () => {
  const binding = env.CUSTOMER_STATE;
  const validation = createCustomerStateService({
    CUSTOMER_STATE: binding, MOCK_STATE_SCOPE: 'validation-test',
  }).forCustomer('shared-customer');
  const scored = createCustomerStateService({
    CUSTOMER_STATE: binding, MOCK_STATE_SCOPE: 'scored-test',
  }).forCustomer('shared-customer');
  await validation.increment('attempts');
  expect(await validation.read('attempts')).toBe(1);
  expect(await scored.read('attempts')).toBeNull();
  await scored.increment('attempts');
  expect(await scored.read('attempts')).toBe(1);
  expect(await validation.read('attempts')).toBe(1);
});
