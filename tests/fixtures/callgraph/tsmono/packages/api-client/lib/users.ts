import { fetchJson } from './client';
import { formatName } from '@acme/utils';

export async function loadUser(id: number): Promise<string> {
  const user = await fetchJson(`/users/${id}`);
  return formatName(user.first, user.last);
}
