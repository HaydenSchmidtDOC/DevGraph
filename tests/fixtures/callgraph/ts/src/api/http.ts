export async function get(url: string) {
  const res = await fetch(url);
  return check(res);
}

export async function put(url: string, body: unknown) {
  const res = await fetch(url, { method: 'PUT', body: JSON.stringify(body) });
  return check(res);
}

function check(res: Response) {
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
