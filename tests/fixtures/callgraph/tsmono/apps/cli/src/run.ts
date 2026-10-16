import * as utils from '@acme/utils';
import { createLogger } from '@acme/logger';
import { build } from '@acme/scripts';

export function run(args: number[]): number {
  const total = utils.sum(args);
  createLogger();
  build();
  return utils.clamp(total, 0, 10);
}
