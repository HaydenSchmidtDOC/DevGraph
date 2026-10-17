import { Link } from 'react-router-dom';
import { Button, Card } from './components';
import * as Ui from './components';
import Modal from './Modal';
import Toast from './Toast';
import { Header } from './layout/Header';

function ListRow({ row }: { row: string }) {
  return <li>{row}</li>;
}

function Footer() {
  return (
    <footer>
      <Link to="/">home</Link>
    </footer>
  );
}

export function App({ Slot }: { Slot: () => JSX.Element }) {
  const rows = ['a', 'b'];
  return (
    <main>
      <Header />
      <Card title="cart" />
      <Button label="save" />
      <Ui.Icon />
      <Slot />
      <Modal />
      <Toast />
      <Footer />
      <ul>
        {rows.map((row) => (
          <ListRow key={row} row={row} />
        ))}
      </ul>
    </main>
  );
}
