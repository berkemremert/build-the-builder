import assert from 'node:assert/strict';
import { test } from 'node:test';
import worker from '../src/index.mjs';
import fixture from '../data/test-customers.json' with { type: 'json' };

const writes = [];
const env = {
  MOCK_BASE_PATH: '', MOCK_STATE_SCOPE: 'validation-test',
  CUSTOMER_STATE: {
    getByName: (name) => ({
      appendValue: async (key, value) => { writes.push({ name, key, value }); return writes.length; },
    }),
  },
};

async function call(service, data, method = 'POST') {
  return worker.fetch(new Request(`https://mock.example/voice/v1/${service}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: method === 'POST' ? JSON.stringify({ data, call: { lang: 'tr' } }) : undefined,
  }), env, {});
}

test('all supplied fixture answers retain exact HTTP status and body', async () => {
  let checked = 0;
  for (const customer of fixture.customers) {
    const services = customer.services;
    for (const service of ['cards.list', 'contact.email.get', 'statement.pdf.send']) {
      const item = services[service];
      if (!item) continue;
      const data = service === 'cards.list' ? { customerNo: customer.customerNo, aiFlow: 'CardOps' }
        : service === 'contact.email.get' ? { customerNo: Number(customer.customerNo), intAddressInfoType: 'E' }
          : { customerNo: customer.customerNo, cardNumber: 'fixture-card', currencyType: 'TL',
              statementDate: '01.10.2026', emailAddress: 'fixture@example.com' };
      const response = await call(service, data);
      assert.equal(response.status, item.http, `${service} ${customer.customerNo}`);
      assert.deepEqual(await response.json(), item.response, `${service} ${customer.customerNo}`);
      checked += 1;
    }
    for (const service of ['cards.balance', 'statement.week.get', 'statement.week.decide', 'statement.week.change']) {
      for (const [cardNumber, byCard] of Object.entries(services[service] ?? {})) {
        const choices = service.endsWith('.decide') || service.endsWith('.change')
          ? Object.entries(byCard).map(([week, item]) => [week, item])
          : [[null, byCard]];
        for (const [week, item] of choices) {
          if (item.delaySeconds) continue;
          const data = { cardNumber };
          if (week !== null) Object.assign(data, { newStatementWeek: Number(week), statementDay: 1 });
          const response = await call(service, data);
          assert.equal(response.status, item.http, `${service} ${cardNumber} ${week}`);
          assert.deepEqual(await response.json(), item.response, `${service} ${cardNumber} ${week}`);
          checked += 1;
        }
      }
    }
  }
  assert.ok(checked > 600, `only ${checked} fixtures checked`);
  assert.ok(writes.some((item) => item.key === 'statement.week.change'));
  assert.ok(writes.some((item) => item.key === 'statement.pdf.send'));
});

test('unknown data and required-field errors use contract code families', async () => {
  const scenarios = [
    ['cards.list', { customerNo: '99999999' }, 404, 'FB_IVR_CHANNEL_102'],
    ['cards.balance', { cardNumber: 'unknown' }, 404, 'FB_IVR_CHANNEL_120'],
    ['statement.week.get', { cardNumber: 'unknown' }, 404, 'FB_IVR_CHANNEL_121'],
    ['statement.week.decide', { cardNumber: 'unknown', newStatementWeek: 2, statementDay: 5 }, 400, 'INVALID_ARGUMENTS_ERROR'],
    ['statement.week.change', { cardNumber: 'unknown', newStatementWeek: 2, statementDay: 5 }, 400, 'INVALID_ARGUMENTS_ERROR'],
    ['contact.email.get', { customerNo: '99999999', intAddressInfoType: 'E' }, 400, 'INVALID_ARGUMENTS_ERROR'],
    ['statement.pdf.send', { customerNo: '99999999', cardNumber: 'unknown', currencyType: 'TL', statementDate: '01.10.2026', emailAddress: 'x@example.com' }, 404, 'FB_IVR_CHANNEL_107'],
    ['cards.balance', { cardNumber: '' }, 400, 'INVALID_ARGUMENTS_ERROR'],
  ];
  for (const [service, data, status, code] of scenarios) {
    const response = await call(service, data);
    assert.equal(response.status, status, service);
    const body = await response.json();
    assert.equal(body.result.returnCode, code, service);
    assert.equal(body.data, undefined, service);
  }
});
