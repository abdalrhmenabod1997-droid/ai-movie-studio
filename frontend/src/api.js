export const API = import.meta.env.VITE_API_URL || 'http://127.0.0.1:8000';
export const join = (url) => (url?.startsWith('http') ? url : `${API}${url || ''}`);

const TOKEN_KEY = 'ai_movie_studio_token';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

// Wraps fetch: attaches the Authorization header automatically, and throws a
// friendly Error (with server-provided message when available) on non-2xx
// responses. On 401 it also clears the stored token so the app can redirect
// the user back to the login page.
export async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const response = await fetch(`${API}${path}`, { ...options, headers });

  if (response.status === 401) {
    setToken(null);
  }

  let data = null;
  const text = await response.text();
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON response */ }

  if (!response.ok) {
    const message = data?.detail || 'حدث خطأ غير متوقع. حاول مرة أخرى.';
    throw new Error(typeof message === 'string' ? message : 'حدث خطأ غير متوقع. حاول مرة أخرى.');
  }
  return data;
}
