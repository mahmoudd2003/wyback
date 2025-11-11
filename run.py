
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI runner for Wayback → WordPress importer
"""

import argparse
import json
from pathlib import Path
import sys

DEFAULT_CONFIG = {
    "database": {"path": "wayback_import.db"},
    "wordpress": {
        "url": "https://your-site.com",
        "username": "admin",
        "app_password": "xxxx xxxx xxxx xxxx xxxx xxxx",
        "default_category_id": 1
    },
    "wayback": {
        "rate_limit": 3,
        "retries": 5,
        "before_date": "20240801",
        "after_date": None,
        "user_agent": "Mozilla/5.0 (compatible; WaybackImporter/1.1)"
    },
    "processing": {
        "batch_size": 150,
        "image_compression": True,
        "max_image_width": 1920,
        "extract_dates": True,
        "fix_rtl": True
    },
    "filters": {
        "exclude_paths": ["/wp-admin/", "/feed/", "/tag/", "/author/", ".xml", ".json"],
        "min_content_length": 100,
        "allowed_domains": []
    },
    "seo": {
        "generate_redirects": True,
        "add_canonical": True,
        "nofollow_external": True,
        "add_schema": True
    }
}


def create_config_file():
    cfg_path = Path("config.json")
    if cfg_path.exists():
        try:
            overwrite = input("⚠️ ملف config.json موجود. هل تريد الكتابة فوقه؟ (y/n): ")
        except EOFError:
            overwrite = 'n'
        if overwrite.lower() != 'y':
            print("❌ تم الإلغاء")
            return
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
    print(f"✅ تم إنشاء {cfg_path}")
    print("📝 عدّل الملف وأضف بيانات WordPress الخاصة بك")


def load_config(config_path="config.json"):
    p = Path(config_path)
    if not p.exists():
        print(f"❌ ملف {config_path} غير موجود")
        print("💡 استخدم: python run.py init لإنشائه")
        sys.exit(1)
    with open(p, 'r', encoding='utf-8') as f:
        return json.load(f)


def show_statistics(db_path):
    from wayback_importer import Database
    db = Database(db_path)

    print("\n" + "="*60)
    print("📊 إحصائيات المشروع")
    print("="*60)

    stats = {}
    cur = db.conn.execute("SELECT status, COUNT(*) FROM urls GROUP BY status")
    for status, count in cur.fetchall():
        stats[f'urls_{status}'] = count

    cur = db.conn.execute("SELECT COUNT(*) FROM articles")
    stats['total_articles'] = cur.fetchone()[0]

    cur = db.conn.execute("SELECT COUNT(*) FROM articles WHERE wp_post_id IS NOT NULL")
    stats['published_articles'] = cur.fetchone()[0]

    cur = db.conn.execute("SELECT COUNT(*) FROM assets")
    stats['total_images'] = cur.fetchone()[0]

    cur = db.conn.execute("SELECT COUNT(*) FROM assets WHERE uploaded = 1")
    stats['uploaded_images'] = cur.fetchone()[0]

    print(f"""
الروابط:
  • معلقة:     {stats.get('urls_pending', 0)}
  • مجلوبة:    {stats.get('urls_fetched', 0)}
  • فاشلة:     {stats.get('urls_failed', 0)}

المقالات:
  • إجمالي:    {stats['total_articles']}
  • منشورة:    {stats['published_articles']}
  • متبقية:    {stats['total_articles'] - stats['published_articles']}

الصور:
  • إجمالي:    {stats['total_images']}
  • مرفوعة:    {stats['uploaded_images']}
  • متبقية:    {stats['total_images'] - stats['uploaded_images']}
    """)

    cur = db.conn.execute("""
        SELECT timestamp, message FROM logs
        WHERE level = 'error'
        ORDER BY id DESC LIMIT 5
    """)
    errors = cur.fetchall()
    if errors:
        print("\n⚠️ آخر الأخطاء:")
        for ts, msg in errors:
            print(f"  • [{ts}] {msg}")

    print("="*60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="نظام استيراد مواقع من Wayback Machine إلى WordPress",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
أمثلة:
  python run.py init
  python run.py discover example.com --limit 1000
  python run.py fetch --batch 100
  python run.py publish --batch 50
  python run.py fix-links
  python run.py full example.com --limit 500
  python run.py stats
        """
    )

    parser.add_argument('command', choices=[
        'init', 'discover', 'fetch', 'publish', 'fix-links', 'full', 'stats'
    ], help='الأمر المطلوب تنفيذه')

    parser.add_argument('domain', nargs='?', help='اسم النطاق (للأوامر التي تحتاجه)')
    parser.add_argument('--config', default='config.json', help='مسار ملف الإعدادات')
    parser.add_argument('--limit', type=int, default=1000, help='عدد الروابط الأقصى')
    parser.add_argument('--batch', type=int, help='حجم الدفعة (يتجاوز الإعدادات)')
    parser.add_argument('--dry-run', action='store_true', help='محاكاة بدون تنفيذ فعلي')

    args = parser.parse_args()

    if args.command == 'init':
        create_config_file()
        return

    config = load_config(args.config)

    # Lazy import
    from wayback_importer import ImportPipeline, Database

    pipeline_config = {
        'db_path': config['database']['path'],
        'wp_url': config['wordpress']['url'],
        'wp_user': config['wordpress']['username'],
        'wp_password': config['wordpress']['app_password'],
        'default_category_id': config['wordpress'].get('default_category_id', 1),
        'batch_size': args.batch or config['processing']['batch_size'],
        'rate_limit': config['wayback'].get('rate_limit', 3),
        'before_date': config['wayback'].get('before_date'),
        'after_date': config['wayback'].get('after_date'),
        'user_agent': config['wayback'].get('user_agent', "Mozilla/5.0 (compatible; WaybackImporter/1.1)"),
    }

    pipeline = ImportPipeline(pipeline_config)

    if args.command == 'discover':
        if not args.domain:
            print("❌ يجب تحديد النطاق: python run.py discover example.com")
            sys.exit(1)
        pipeline.run_discovery(args.domain, limit=args.limit)

    elif args.command == 'fetch':
        import asyncio
        asyncio.run(pipeline.run_fetching())

    elif args.command == 'publish':
        if args.dry_run:
            print("🧪 وضع المحاكاة - لن يتم النشر الفعلي")
            db = Database(config['database']['path'])
            cur = db.conn.execute("SELECT COUNT(*) FROM articles WHERE wp_post_id IS NULL")
            count = cur.fetchone()[0]
            print(f"📊 سيتم نشر {count} مقال")
        else:
            pipeline.run_publishing()

    elif args.command == 'fix-links':
        pipeline.run_link_fixing()

    elif args.command == 'full':
        if not args.domain:
            print("❌ يجب تحديد النطاق: python run.py full example.com")
            sys.exit(1)
        pipeline.run_full_pipeline(args.domain, limit=args.limit)

    elif args.command == 'stats':
        show_statistics(config['database']['path'])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⚠️ تم الإيقاف بواسطة المستخدم")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ خطأ: {e}")
        sys.exit(1)
