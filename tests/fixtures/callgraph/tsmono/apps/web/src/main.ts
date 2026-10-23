import { loadUser } from 'api-client/lib/users';
import { clamp } from '@acme/utils';
import { levelName } from '@acme/logger/levels';
import { render } from './view';

export async function start(): Promise<void> {
  const name = await loadUser(1);
  render(name, clamp(3, 0, 2));
  levelName(2);
}
