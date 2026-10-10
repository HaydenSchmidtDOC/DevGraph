export function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function formatDate(d: Date): string {
  return d.toLocaleDateString();
}
