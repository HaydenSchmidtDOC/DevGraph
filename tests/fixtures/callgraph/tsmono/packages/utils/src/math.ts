export function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

export function sum(values: number[]): number {
  return values.reduce((total, each) => total + each, 0);
}
