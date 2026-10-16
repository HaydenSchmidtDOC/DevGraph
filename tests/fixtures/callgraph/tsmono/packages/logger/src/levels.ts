const NAMES = ['debug', 'info', 'warn', 'error'];

export function levelName(level: number): string {
  return NAMES[level] ?? 'unknown';
}
