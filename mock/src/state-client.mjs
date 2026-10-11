export function createCustomerStateService(env) {
  const namespace = env?.CUSTOMER_STATE;
  if (!namespace || typeof namespace.getByName !== 'function') {
    throw new Error('Customer state binding is unavailable');
  }
  return {
    forCustomer(customerId) {
      if (typeof customerId !== 'string' || !customerId.trim()) {
        throw new TypeError('Customer ID must be a nonempty string');
      }
      const stub = namespace.getByName(`customer:${customerId}`);
      return {
        read: (key) => stub.readValue(key),
        write: (key, value) => stub.writeValue(key, value),
        increment: (key, delta = 1) => stub.incrementValue(key, delta),
        append: (key, value) => stub.appendValue(key, value),
      };
    },
  };
}
