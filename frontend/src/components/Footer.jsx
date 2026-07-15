import { Link } from 'react-router-dom';

export default function Footer() {
  return (
    <footer className="site-footer">
      <nav>
        <Link to="/about">من نحن</Link>
        <Link to="/faq">الأسئلة الشائعة</Link>
        <Link to="/contact">تواصل معنا</Link>
        <Link to="/privacy">سياسة الخصوصية</Link>
        <Link to="/terms">شروط الاستخدام</Link>
      </nav>
      <small>© {new Date().getFullYear()} AI Movie Studio. جميع الحقوق محفوظة.</small>
    </footer>
  );
}
