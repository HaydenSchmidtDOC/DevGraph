export function Button({ label }: { label: string }) {
  return <button type="button">{label}</button>;
}

export const Card = ({ title }: { title: string }) => <section aria-label={title} />;
