import * as bank from './bank.mjs';

function json(body, status, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      ...headers,
    },
  });
}

async function configuredBank(implementation, env) {
  if (implementation.enabled !== true ||
      typeof implementation.loadContract !== 'function' ||
      typeof implementation.loadData !== 'function' ||
      typeof implementation.handleBankRequest !== 'function') {
    return null;
  }
  const [contract, data] = await Promise.all([
    implementation.loadContract(env),
    implementation.loadData(env),
  ]);
  if (!contract || typeof contract !== 'object' ||
      typeof contract.openapi !== 'string' ||
      !contract.paths || typeof contract.paths !== 'object' ||
      Object.keys(contract.paths).length === 0 ||
      !data || typeof data !== 'object') {
    return null;
  }
  return { contract, data };
}

export function createWorker(implementation = bank) {
  return {
    async fetch(request, env, ctx) {
      const path = new URL(request.url).pathname;
      if (path === '/healthz') {
        return request.method === 'GET'
          ? json({ status: 'alive' }, 200)
          : json({ error: 'method_not_allowed' }, 405, { Allow: 'GET' });
      }

      let configured;
      try {
        configured = await configuredBank(implementation, env);
      } catch (_error) {
        configured = null;
      }

      if (path === '/readyz') {
        if (request.method !== 'GET') {
          return json({ error: 'method_not_allowed' }, 405, { Allow: 'GET' });
        }
        return json({ ready: Boolean(configured) }, configured ? 200 : 503);
      }
      if (!configured) {
        return json({ error: 'bank_mock_not_configured' }, 503);
      }

      try {
        const response = await implementation.handleBankRequest(request, {
          env,
          ctx,
          contract: configured.contract,
          data: configured.data,
        });
        return response instanceof Response
          ? response
          : json({ error: 'invalid_bank_response' }, 500);
      } catch (_error) {
        return json({ error: 'bank_handler_failed' }, 500);
      }
    },
  };
}

export default createWorker();
