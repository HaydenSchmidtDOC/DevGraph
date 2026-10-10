import { getItem } from './storage';
import type { Session } from '../types';

export function loadSession(): Session {
  const raw = getItem('session');
  return parseSession(raw);
}

function parseSession(raw: string | null): Session {
  return raw ? JSON.parse(raw) : { user: { id: '', name: '', first: '', last: '' } };
}
