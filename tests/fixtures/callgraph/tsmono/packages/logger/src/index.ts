import { levelName } from './levels';

export class Logger {
  record(message: string): void {
    this.write(levelName(1), message);
  }

  write(level: string, message: string): void {
    console.log(`[${level}] ${message}`);
  }
}

export function createLogger(): Logger {
  return new Logger();
}
