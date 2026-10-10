import { createRoot } from 'react-dom/client';
import { App } from './App';

export function mount(el: HTMLElement) {
  createRoot(el).render(<App />);
}
