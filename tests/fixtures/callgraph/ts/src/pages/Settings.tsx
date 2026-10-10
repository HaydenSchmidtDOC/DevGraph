import { useState } from 'react';
import * as api from '@/api';
import { save as saveDraft } from '../lib/helpers.js';
import { UserService } from '@/services/UserService';
import { UserRepo } from '@/services/UserRepo';
import { Logger } from '@/lib/logger';

export async function loadSettings(id: string) {
  return api.fetchUser(id);
}

export function keepDraft(form: Record<string, string>) {
  saveDraft(form);
}

export function renameUser(id: string, name: string) {
  const svc = new UserService(new UserRepo(), new Logger());
  return svc.rename(id, name);
}

export function Settings() {
  const [name, setName] = useState('');
  return <input value={name} onChange={(e) => setName(e.target.value)} />;
}
