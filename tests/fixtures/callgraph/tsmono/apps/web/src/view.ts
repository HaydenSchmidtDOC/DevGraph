import _ from 'lodash';
import { slugify } from '@acme/utils';

export function render(name: string, count: number): string {
  document.title = slugify(name);
  return _.capitalize(name) + count;
}
