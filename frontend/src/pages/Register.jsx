import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuth } from '../AuthContext.jsx';

export default function Register() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setError('');
    if (password.length < 8) { setError('كلمة المرور يجب ألا تقل عن 8 أحرف.'); return; }
    if (password !== confirm) { setError('كلمتا المرور غير متطابقتين.'); return; }
    setBusy(true);
    try {
      await register(email.trim(), password);
      navigate('/', { replace: true });
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
        <h2>إنشاء حساب جديد</h2>
        <label>
          البريد الإلكتروني
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
        </label>
        <label>
          كلمة المرور
          <input type="password" required value={password} onChange={(e) => setPassword(e.target.value)} placeholder="8 أحرف على الأقل" />
        </label>
        <label>
          تأكيد كلمة المرور
          <input type="password" required value={confirm} onChange={(e) => setConfirm(e.target.value)} placeholder="أعد كتابة كلمة المرور" />
        </label>
        {error && <p className="error">{error}</p>}
        <button disabled={busy}>{busy ? 'جارٍ الإنشاء…' : 'إنشاء الحساب'}</button>
        <small>
          لديك حساب بالفعل؟ <Link to="/login">تسجيل الدخول</Link>
        </small>
      </form>
    </main>
  );
}
