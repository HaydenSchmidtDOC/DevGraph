import { App } from './App';

export function Root() {
  return <App Slot={() => <span />} />;
}
