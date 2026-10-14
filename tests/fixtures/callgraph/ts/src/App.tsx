import { Profile } from '@/pages/Profile';
import { loadSession } from '@/lib/session.js';

export function App() {
  const session = loadSession();
  return <Profile user={session.user} />;
}
