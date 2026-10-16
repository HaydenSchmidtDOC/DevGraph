import { Logger } from '@acme/logger';

export async function fetchJson(url: string): Promise<any> {
  const log = new Logger();
  log.record(`GET ${url}`);
  const response = await fetch(url);
  return response.json();
}
