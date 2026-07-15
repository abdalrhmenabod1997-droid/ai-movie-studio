import LegalPage from './LegalPage.jsx';

export default function Contact() {
  return (
    <LegalPage title="تواصل معنا">
      <p>يسعدنا تواصلك معنا لأي استفسار أو ملاحظة أو مشكلة تقنية تواجهها أثناء استخدام المنصة.</p>

      <h2>البريد الإلكتروني</h2>
      <p>
        راسلنا على{' '}
        <a href="mailto:support@aimoviestudio.example">support@aimoviestudio.example</a>{' '}
        وسنرد عليك في أقرب وقت ممكن.
      </p>

      <h2>الدعم الفني</h2>
      <p>
        إذا واجهت مشكلة في إنشاء أحد مشاريعك، يرجى ذكر رقم المشروع (يظهر في تفاصيل المشروع) عند
        التواصل معنا، لمساعدتنا على تشخيص المشكلة بسرعة أكبر.
      </p>

      <p className="legal-disclaimer">
        عنوان البريد الإلكتروني أعلاه نموذج توضيحي؛ يرجى استبداله بعنوان الدعم الفعلي قبل الإطلاق.
      </p>
    </LegalPage>
  );
}
