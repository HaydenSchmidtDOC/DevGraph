import { Button } from '../components/Button';

export function Header() {
  return (
    <header>
      <Button label="menu" />
      <Title />
    </header>
  );
}

function Title() {
  return <h1>Shop</h1>;
}
