import { UserRepo } from './UserRepo';
import { Logger } from '@/lib/logger';
import type { User } from '@/types';

export class UserService {
  constructor(private repo: UserRepo, private readonly log: Logger) {}

  rename(id: string, name: string): User {
    const user = this.repo.find(id) ?? { id, name, first: name, last: '' };
    this.log.info(`rename ${id}`);
    return this.repo.save({ ...user, name });
  }

  count(): number {
    return this.repo.all().length;
  }

  label(id: string): string {
    return this.describe(this.repo.find(id));
  }

  private describe(user: User | undefined): string {
    return user ? user.name : 'unknown';
  }
}
