export interface User {
  id: string;
  name: string;
  first: string;
  last: string;
}

export interface Session {
  user: User;
}
