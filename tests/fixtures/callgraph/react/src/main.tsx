import { App } from './App';
import Toast from './Toast';

export function Root() {
  return <App Slot={() => <Toast />} />;
}
