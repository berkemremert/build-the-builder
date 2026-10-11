import { DurableObject } from 'cloudflare:workers';

function requireKey(key) {
  if (typeof key !== 'string' || !key.trim()) {
    throw new TypeError('State key must be a nonempty string');
  }
  return key;
}

// Each customer ID maps to one SQLite-backed Durable Object. No request state
// is kept in Worker module globals, so reads survive new requests and isolates.
export class CustomerState extends DurableObject {
  readValue(key) {
    const value = this.ctx.storage.kv.get(requireKey(key));
    return value === undefined ? null : value;
  }

  writeValue(key, value) {
    this.ctx.storage.kv.put(requireKey(key), value);
    return true;
  }

  incrementValue(key, delta = 1) {
    requireKey(key);
    if (!Number.isSafeInteger(delta)) {
      throw new TypeError('Counter increment must be a safe integer');
    }
    return this.ctx.storage.transactionSync(() => {
      const current = this.ctx.storage.kv.get(key) ?? 0;
      if (!Number.isSafeInteger(current) || !Number.isSafeInteger(current + delta)) {
        throw new TypeError('Stored counter must remain a safe integer');
      }
      const next = current + delta;
      this.ctx.storage.kv.put(key, next);
      return next;
    });
  }

  appendValue(key, value) {
    requireKey(key);
    return this.ctx.storage.transactionSync(() => {
      const current = this.ctx.storage.kv.get(key) ?? [];
      if (!Array.isArray(current)) {
        throw new TypeError('Stored event list must be an array');
      }
      const next = [...current, value];
      this.ctx.storage.kv.put(key, next);
      return next.length;
    });
  }
}
