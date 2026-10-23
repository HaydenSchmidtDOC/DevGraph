import { useCart } from '../hooks/useCart';
import { Button, Card } from '../components';
import { Badge, Field } from '../components/Badge';

export function CartPage() {
  const { total } = useCart();
  return (
    <>
      <Card title={total} />
      <Button label="pay" />
      <Badge text={total} />
      <Field name="coupon" />
    </>
  );
}
