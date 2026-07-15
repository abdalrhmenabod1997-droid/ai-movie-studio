import { Link } from 'react-router-dom';

export default function LegalPage({ title, updated, children }) {
  return (
    <main className="legal-page">
      <div className="panel legal-content">
        <Link to="/" className="back-link">‹ العودة إلى الاستوديو</Link>
        <span className="eyebrow">AI MOVIE STUDIO</span>
        <h1>{title}</h1>
        {updated && <p className="legal-updated">آخر تحديث: {updated}</p>}
        {children}
      </div>
    </main>
  );
}
