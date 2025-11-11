
# Wayback → WordPress Importer (Streamlit)

هذا المشروع يتيح لك استيراد صفحات من Wayback Machine إلى ووردبريس عبر واجهة Streamlit:

- اكتشاف روابط الموقع من أرشيف الإنترنت (CDX)
- جلب الصفحات ومعالجتها (استخراج المحتوى + الصور من لقطات `im_`)
- نشر المقالات كمسودات على WordPress (REST API)
- إصلاح الروابط الداخلية بعد النشر

## الخطوات

### 1) المتطلبات
- Python 3.10+
- حساب Streamlit Cloud (أو تشغيل محليًا)

### 2) الملفات
- `streamlit_app.py` : واجهة Streamlit
- `wayback_importer.py` : المكتبة الأساسية
- `run.py` : تشغيل عبر CLI (اختياري)
- `requirements.txt` : المتطلبات
- `config.json` : مثال إعدادات (للتشغيل المحلي)

### 3) التشغيل محليًا
```bash
python -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

### 4) النشر على Streamlit Cloud
1. ارفع الملفات على GitHub (هذه الأربعة على الأقل):
   - `streamlit_app.py`
   - `wayback_importer.py`
   - `requirements.txt`
   - `run.py` (اختياري)
2. على https://streamlit.app أنشئ تطبيقًا جديدًا من المستودع واختَر ملف `streamlit_app.py` كصفحة رئيسية.
3. من إعدادات التطبيق في Streamlit، أضِف **Secrets** بهذا الشكل:
```toml
[wordpress]
url = "https://your-site.com"
username = "admin"
app_password = "xxxx xxxx xxxx xxxx xxxx xxxx"
default_category_id = 1

[wayback]
before_date = "20240801"
rate_limit = 3
user_agent = "Mozilla/5.0 (compatible; WaybackImporter/1.1)"

[database]
path = "wayback_import.db"
```
4. شغّل التطبيق من لوحة Streamlit، أدخل النطاق واضغط **Run Full Pipeline**.

> **ملاحظة**: مساحة القرص في Streamlit مؤقتة. قاعدة البيانات SQLite سيتم إنشاؤها داخل مجلد التطبيق. لتخزين طويل الأمد، فكّر في استخدام قاعدة خارجية أو تنزيل الملف دوريًا.


### 5) حدود وأمان
- تأكد أن موقع ووردبريس يستخدم HTTPS وأن REST API مفتوح.
- استخدم **Application Password** لحساب ذي صلاحيات كافية.
- احترم حقوق النشر عند استيراد محتوى أرشيفي.


## رخصة
للاستخدام التعليمي والعملي مع احترام تراخيص المحتوى الأصلي.
