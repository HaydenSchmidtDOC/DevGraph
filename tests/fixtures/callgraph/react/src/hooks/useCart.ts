import { formatPrice } from '../lib/format';

export function useCart() {
  const total = formatPrice(1250);
  return { total };
}
