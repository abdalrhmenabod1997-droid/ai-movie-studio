import { useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { useAuth } from '../AuthContext.jsx';

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await login(email.trim(), password);
      const redirectTo = location.state?.from || '/';
      navigate(redirectTo, { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="auth-page">
      <form className="panel auth-form" onSubmit={submit}>
        <span className="eyebrow">AI MOVIE STUDIO</span>
        <h2>تسجيل الدخول</h2>
        <label>
          البريد الإلكتروني
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        </label>
        <label>
          كلمة المرور
          <input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" />
        </label>
        {error && <p className="error">{error}</p>}
        <button disabled={busy}>{busy ? 'جارٍ الدخول…' : 'تسجيل الدخول'}</button>
        <small>
          ليس لديك حساب؟ <Link to="/register">أنشئ حسابًا جديدًا</Link>
        </small>
      </form>
    </main>
  );
}
