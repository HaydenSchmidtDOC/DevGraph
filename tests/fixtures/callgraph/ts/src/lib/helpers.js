import _ from 'lodash';
import { setItem } from './storage';

export function save(data) {
  setItem('draft', JSON.stringify(data));
}

export function formatName(user) {
  return _.capitalize(user.first) + ' ' + _.capitalize(user.last);
}
