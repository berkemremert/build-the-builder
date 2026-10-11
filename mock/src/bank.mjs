// Replace these hooks only after the final bank contract and case data arrive.
// The explicit switch prevents an incomplete Worker from serving bank routes.
export const enabled = false;

export async function loadContract(_env) {
  return null;
}

export async function loadData(_env) {
  return null;
}

export const handleBankRequest = null;
