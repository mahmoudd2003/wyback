
# streamlit_app.py
# -*- coding: utf-8 -*-
import streamlit as st
import os
import asyncio
from pathlib import Path

# Local imports
from wayback_importer import ImportPipeline

st.set_page_config(page_title="Wayback → WordPress Importer", page_icon="🗂️", layout="wide")

st.title("🗂️ Wayback → WordPress Importer (Streamlit)")
st.caption("اكتشاف الروابط من Wayback، جلب المحتوى، رفع الصور، نشر المقالات على ووردبريس، وإصلاح الروابط الداخلية.")

with st.expander("ℹ️ إرشادات سريعة", expanded=False):
    st.markdown("""
    - **حفظ الأسرار (Secrets)** من إعدادات التطبيق في Streamlit:
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
    - يمكن تعديل القيم مؤقتًا من الواجهة هنا.
    - للتشغيل الكامل: أدخل النطاق ثم اضغط **Run Full Pipeline**.
    """)

# ---- Load secrets (if any) ----
def get_secret(section: str, key: str, default=None):
    try:
        return st.secrets[section][key]
    except Exception:
        return default

default_wp_url = get_secret("wordpress", "url", "")
default_wp_user = get_secret("wordpress", "username", "")
default_wp_pass = get_secret("wordpress", "app_password", "")
default_cat_id = int(get_secret("wordpress", "default_category_id", 1))

default_db_path = get_secret("database", "path", "wayback_import.db")
default_rate = int(get_secret("wayback", "rate_limit", 3))
default_before = get_secret("wayback", "before_date", "20240801")
default_after = get_secret("wayback", "after_date", None)
default_ua = get_secret("wayback", "user_agent", "Mozilla/5.0 (compatible; WaybackImporter/1.1)")

with st.sidebar:
    st.header("⚙️ الإعدادات")
    wp_url = st.text_input("WordPress URL", value=default_wp_url, placeholder="https://your-site.com")
    wp_user = st.text_input("WordPress Username", value=default_wp_user)
    wp_pass = st.text_input("WordPress App Password", value=default_wp_pass, type="password")
    default_category_id = st.number_input("Default Category ID", value=default_cat_id, min_value=1, step=1)

    st.markdown("---")
    db_path = st.text_input("Database path (SQLite)", value=default_db_path)
    rate_limit = st.number_input("Wayback Rate Limit (req/s)", min_value=1, max_value=10, value=default_rate, step=1)
    before_date = st.text_input("Wayback BEFORE date (YYYYMMDD)", value=default_before)
    after_date = st.text_input("Wayback AFTER date (YYYYMMDD or blank)", value=default_after or "")
    ua = st.text_input("User-Agent", value=default_ua)

    st.markdown("---")
    batch_size = st.number_input("Batch size", min_value=10, max_value=500, value=150, step=10)

# Inputs
domain = st.text_input("🕸️ النطاق المراد استيراده (مثال: example.com)", value="", placeholder="example.com")
limit = st.number_input("الحد الأقصى لعدد الروابط (Discovery Limit)", min_value=50, max_value=20000, value=500, step=50)

# Initialize pipeline (lazy)
def build_pipeline():
    cfg = {
        'db_path': db_path,
        'wp_url': wp_url.strip(),
        'wp_user': wp_user.strip(),
        'wp_password': wp_pass.strip(),
        'default_category_id': int(default_category_id),
        'batch_size': int(batch_size),
        'rate_limit': int(rate_limit),
        'before_date': before_date.strip() or None,
        'after_date': (after_date or "").strip() or None,
        'user_agent': ua.strip()
    }
    return ImportPipeline(cfg)

def run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # In case an event loop is already running
        loop = asyncio.get_event_loop()
        return loop.run_until_complete(coro)

col1, col2, col3, col4, col5 = st.columns(5)
run_full = col1.button("🚀 Run Full Pipeline", use_container_width=True, type="primary", disabled=not domain)
btn_discover = col2.button("🔍 Discover", use_container_width=True, disabled=not domain)
btn_fetch = col3.button("📥 Fetch", use_container_width=True)
btn_publish = col4.button("📝 Publish", use_container_width=True)
btn_fix = col5.button("🔗 Fix Links", use_container_width=True)

log_area = st.empty()

def status_log(msg):
    with log_area.container():
        st.write(msg)

if run_full:
    if not (wp_url and wp_user and wp_pass):
        st.error("أدخل إعدادات WordPress الصحيحة من الشريط الجانبي أو عبر Secrets.")
    else:
        pipe = build_pipeline()
        with st.status("تشغيل العملية الكاملة...", expanded=True) as status:
            st.write("🔍 اكتشاف الروابط...")
            pipe.run_discovery(domain, limit=int(limit))

            st.write("📥 جلب ومعالجة المحتوى...")
            run_async(pipe.run_fetching())

            st.write("📝 النشر على WordPress...")
            pipe.run_publishing()

            st.write("🔗 إصلاح الروابط الداخلية...")
            pipe.run_link_fixing()

            status.update(label="✅ اكتملت العملية بنجاح", state="complete")

if btn_discover:
    pipe = build_pipeline()
    with st.spinner("اكتشاف الروابط..."):
        pipe.run_discovery(domain, limit=int(limit))
    st.success("تم الاكتشاف. استخدم Fetch للمتابعة.")

if btn_fetch:
    pipe = build_pipeline()
    with st.spinner("جلب ومعالجة المحتوى..."):
        run_async(pipe.run_fetching())
    st.success("تم الجلب والمعالجة.")

if btn_publish:
    if not (wp_url and wp_user and wp_pass):
        st.error("أدخل إعدادات WordPress الصحيحة أولًا.")
    else:
        pipe = build_pipeline()
        with st.spinner("النشر على WordPress..."):
            pipe.run_publishing()
        st.success("اكتمل النشر.")

if btn_fix:
    pipe = build_pipeline()
    with st.spinner("إصلاح الروابط الداخلية..."):
        pipe.run_link_fixing()
    st.success("تم إصلاح الروابط.")

st.markdown("---")
st.subheader("📊 إحصائيات سريعة")
if Path(db_path).exists():
    from wayback_importer import Database
    db = Database(db_path)
    cur = db.conn.execute("SELECT status, COUNT(*) FROM urls GROUP BY status")
    rows = cur.fetchall()
    cols = st.columns(3)
    status_map = dict(rows)
    cols[0].metric("Pending URLs", status_map.get('pending', 0))
    cols[1].metric("Fetched URLs", status_map.get('fetched', 0))
    cols[2].metric("Failed URLs", status_map.get('failed', 0))

    cur = db.conn.execute("SELECT COUNT(*) FROM articles")
    total_articles = cur.fetchone()[0]
    cur = db.conn.execute("SELECT COUNT(*) FROM articles WHERE wp_post_id IS NOT NULL")
    published_articles = cur.fetchone()[0]
    st.metric("Published Articles", published_articles, delta=published_articles - 0)
    st.caption(f"Total Articles in DB: {total_articles}")
else:
    st.info("قاعدة البيانات غير موجودة بعد. ابدأ بـ Discover/Fetch.")
