import { useState } from 'react';
import { save } from '../api/users';
import { fetchPosts } from '../api';
import { capitalize } from '@/lib/text';
import type { User } from '@/types';

export async function persistProfile(user: User) {
  await save(user);
}

export async function loadPosts(userId: string) {
  return fetchPosts(userId);
}

export function Profile({ user }: { user: User }) {
  const [editing] = useState(false);
  const title = capitalize(user.first);
  return <h1>{editing ? '...' : title}</h1>;
}
