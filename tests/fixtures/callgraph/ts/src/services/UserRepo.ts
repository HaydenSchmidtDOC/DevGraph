import type { User } from '../types';

export class UserRepo {
  private items: User[] = [];

  find(id: string): User | undefined {
    return this.items.find((u) => u.id === id);
  }

  save(user: User): User {
    this.items.push(user);
    return user;
  }

  all(): User[] {
    return this.items.slice();
  }
}
