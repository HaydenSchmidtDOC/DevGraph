import { get } from './http.js';

export function fetchPosts(userId: string) {
  return get(`/posts?user=${userId}`);
}
