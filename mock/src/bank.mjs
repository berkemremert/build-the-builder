import fixture from '../data/test-customers.json' with { type: 'json' };

export const enabled = true;

const SERVICE_NAMES = new Set([
  'cards.list', 'cards.balance', 'statement.week.get',
  'statement.week.decide', 'statement.week.change',
  'contact.email.get', 'statement.pdf.send',
]);

const CUSTOMER_FIELDS = new Map([
  ['cards.list', ['customerNo']],
  ['contact.email.get', ['customerNo', 'intAddressInfoType']],
  ['statement.pdf.send', ['customerNo', 'cardNumber', 'currencyType', 'statementDate', 'emailAddress']],
]);
const CARD_FIELDS = new Map([
  ['cards.balance', ['cardNumber']],
  ['statement.week.get', ['cardNumber']],
  ['statement.week.decide', ['cardNumber', 'newStatementWeek', 'statementDay']],
  ['statement.week.change', ['cardNumber', 'newStatementWeek', 'statementDay']],
]);

const customers = new Map(fixture.customers.map((item) => [String(item.customerNo), item]));
const cards = new Map();
for (const customer of fixture.customers) {
  for (const service of CARD_FIELDS.keys()) {
    for (const cardNumber of Object.keys(customer.services[service] ?? {})) {
      let entry = cards.get(cardNumber);
      if (!entry) {
        entry = { customer, services: {} };
        cards.set(cardNumber, entry);
      }
      entry.services[service] = customer.services[service][cardNumber];
    }
  }
}

export async function loadContract(_env) {
  return {
    openapi: '3.0.3',
    paths: Object.fromEntries([...SERVICE_NAMES].map((name) => [`/voice/v1/${name}`, { post: {} }])),
  };
}

export async function loadData(_env) {
  return fixture;
}

function resultError(code, message, type = 'ERROR') {
  const guid = crypto.randomUUID().replaceAll('-', '');
  return {
    result: {
      guid, returnCode: code, messageText: message, code, message, type,
      details: {
        guid, rootCause: code === 'INVALID_ARGUMENTS_ERROR'
          ? 'InvalidArgumentsException'
          : 'com.freyabank.ivrgateway.ivrchannel.rest.exception.NotFoundException',
        errorOrigin: 'ivr-gateway-test:ivr-channel-v0',
      },
    },
  };
}

function json(body, status) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

function invalid(field) {
  const names = {
    customerNo: 'customer number', cardNumber: 'card number',
    newStatementWeek: 'new statement week', statementDay: 'statement day',
    intAddressInfoType: 'intAddressInfoType', currencyType: 'currency type',
    statementDate: 'statement date', emailAddress: 'email address',
  };
  return json(resultError('INVALID_ARGUMENTS_ERROR', `${names[field] ?? field} cannot be empty`), 400);
}

function missing(service) {
  const errors = {
    'cards.list': ['FB_IVR_CHANNEL_102', 'Müşteriye ait geçerli kart bilgisi bulunamadı.'],
    'cards.balance': ['FB_IVR_CHANNEL_120', 'Kart bakiye servisi hatası: kart bulunamadı'],
    'statement.week.get': ['FB_IVR_CHANNEL_121', 'Kart bulunamadı'],
    'statement.pdf.send': ['FB_IVR_CHANNEL_107', 'Pdf dokümanı bulunamadı'],
  };
  const error = errors[service];
  return error ? json(resultError(error[0], error[1], 'WARNING'), 404) : invalid(
    service === 'contact.email.get' ? 'customerNo' : 'newStatementWeek',
  );
}

export async function handleBankRequest(request, { operationPath, state }) {
  if (!operationPath.startsWith('/voice/v1/')) return json(resultError('INVALID_ARGUMENTS_ERROR', 'service not found'), 404);
  const service = operationPath.slice('/voice/v1/'.length);
  if (!SERVICE_NAMES.has(service)) return json(resultError('INVALID_ARGUMENTS_ERROR', 'service not found'), 404);
  if (request.method !== 'POST') return json(resultError('INVALID_ARGUMENTS_ERROR', 'POST required'), 405);

  let body;
  try { body = await request.json(); } catch { return invalid('data'); }
  const data = body?.data;
  if (!data || typeof data !== 'object' || Array.isArray(data)) return invalid('data');
  for (const field of CUSTOMER_FIELDS.get(service) ?? CARD_FIELDS.get(service) ?? []) {
    if (data[field] === undefined || data[field] === null || String(data[field]).trim() === '') {
      return invalid(field);
    }
  }

  const customer = customers.get(String(data.customerNo));
  const card = cards.get(String(data.cardNumber));
  let item;
  if (service === 'cards.list' || service === 'contact.email.get' || service === 'statement.pdf.send') {
    item = customer?.services[service];
  } else if (service === 'statement.week.decide' || service === 'statement.week.change') {
    item = card?.services[service]?.[String(data.newStatementWeek)];
  } else {
    item = card?.services[service];
  }
  if (!item) return missing(service);

  if (item.delaySeconds) await new Promise((resolve) => setTimeout(resolve, item.delaySeconds * 1000));

  // Persist side effects without changing the exact per-service fixture answers.
  if (item.http === 200 && service === 'statement.week.change') {
    await state.forCustomer(card.customer.customerNo).append('statement.week.change', {
      cardNumber: String(data.cardNumber), newStatementWeek: String(data.newStatementWeek),
      statementDay: data.statementDay,
    });
  }
  if (item.http === 200 && service === 'statement.pdf.send') {
    await state.forCustomer(customer.customerNo).append('statement.pdf.send', {
      cardNumber: String(data.cardNumber), currencyType: String(data.currencyType),
      statementDate: String(data.statementDate), emailAddress: String(data.emailAddress),
    });
  }
  return json(item.response, item.http);
}
