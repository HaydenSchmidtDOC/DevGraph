import * as http from './http';
import type { User } from '../types';

export async function save(user: User) {
  return http.put(`/users/${user.id}`, user);
}

export async function fetchUser(id: string): Promise<User> {
  return http.get(`/users/${id}`);
}
