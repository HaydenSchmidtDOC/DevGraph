import { useCart } from '../hooks/useCart';
import { Button, Card } from '../components';

export function CartPage() {
  const { total } = useCart();
  return (
    <>
      <Card title={total} />
      <Button label="pay" />
    </>
  );
}
