import { Route, Routes } from 'react-router-dom';
import { CartPage } from './pages/CartPage';
import HomePage from './pages/HomePage';

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/cart" element={<CartPage />} />
    </Routes>
  );
}
