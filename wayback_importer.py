
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wayback → WordPress Importer (fixed & improved)
- DB with indices + WAL
- CDX discovery with duplicate 'filter' params via list-of-tuples
- Async fetcher with retries + UA
- Content processing (trafilatura fallback to BeautifulSoup)
- Image handling from Wayback using "im_" snapshots
- WP Publisher with retries + featured image + alt_text
- Internal links fixing with normalized comparison
"""

import asyncio
import aiohttp
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import json
import time
import re
from typing import List, Dict, Optional, Tuple

try:
    from slugify import slugify
except Exception:
    def slugify(s):  # minimal fallback
        return re.sub(r'[^a-zA-Z0-9\-]+', '-', s.strip().lower()).strip('-')

# Optional deps
try:
    import trafilatura  # for better content extraction
except Exception:
    trafilatura = None

try:
    from dateutil import parser as dateparser
except Exception:
    dateparser = None


# ============================== Utils ======================================

def make_requests_session(ua: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {"User-Agent": ua or "Mozilla/5.0 (compatible; WaybackImporter/1.1)", "Accept-Encoding": "gzip, deflate"}
    )
    retries = Retry(total=5, backoff_factor=1.2, status_forcelist=[429, 500, 502, 503, 504])
    s.mount('http://', HTTPAdapter(max_retries=retries))
    s.mount('https://', HTTPAdapter(max_retries=retries))
    return s


def wayback_image_url(snapshot_url: str, img_src: str) -> str:
    """Return a robust Wayback image URL (im_) for a given img src within a given snapshot page."""
    if not img_src:
        return img_src

    # already a data URL
    if img_src.startswith('data:'):
        return img_src

    # If image is already from Wayback: force im_
    if 'web.archive.org' in img_src:
        return re.sub(r'/web/(\d+)[^/]+/', r'/web/\1im_/', img_src)

    # If the image is relative to the snapshot page
    m = re.match(r'(https?://web\.archive\.org/web/)(\d+)[^/]+/(.*)', snapshot_url)
    if m:
        ts = m.group(2)
        # Make absolute based on the original page url (group 3 is the original url, but we can use the snapshot base)
        # Build a base that ends after "timestampid_/"
        base = re.sub(r'(https?://web\.archive\.org/web/\d+)[^/]+/(.*)', r'\1id_/', snapshot_url)
        abs_src = urljoin(base, img_src)
        # Replace id_ with im_
        abs_src = re.sub(r'/web/(\d+)id_/', r'/web/\1im_/', abs_src)
        return abs_src

    return img_src


def normalize_url(host_or_url: str) -> Tuple[str, str]:
    """Return (host, path) normalized for consistent internal link matching."""
    pu = urlparse(host_or_url)
    host = pu.netloc.lower().lstrip('www.')
    path = re.sub(r'//+', '/', pu.path.rstrip('/'))
    return host, path


def to_iso_utc(dt_str: Optional[str]) -> str:
    if not dt_str:
        return datetime.now(timezone.utc).isoformat()
    if dateparser:
        try:
            dt = dateparser.parse(dt_str, fuzzy=True)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat()


# ============================== Database ===================================

class Database:
    def __init__(self, db_path="wayback_import.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._tune()
        self.init_tables()

    def _tune(self):
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")

    def init_tables(self):
        cur = self.conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY,
                original_url TEXT UNIQUE,
                snapshot_url TEXT,
                timestamp TEXT,
                status TEXT DEFAULT 'pending',
                retries INTEGER DEFAULT 0,
                discovered_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY,
                url_id INTEGER,
                title TEXT,
                content TEXT,
                excerpt TEXT,
                pub_date TEXT,
                author TEXT DEFAULT 'admin',
                category TEXT,
                tags TEXT,
                wp_post_id INTEGER,
                wp_permalink TEXT,
                status TEXT DEFAULT 'draft',
                FOREIGN KEY(url_id) REFERENCES urls(id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY,
                article_id INTEGER,
                original_url TEXT,
                local_path TEXT,
                wp_media_id INTEGER,
                wp_url TEXT,
                uploaded INTEGER DEFAULT 0,
                alt_text TEXT,
                FOREIGN KEY(article_id) REFERENCES articles(id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY,
                timestamp TEXT,
                level TEXT,
                message TEXT,
                context TEXT
            )
        """)

        # Indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_articles_wp ON articles(wp_post_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_assets_article_uploaded ON assets(article_id, uploaded)")

        self.conn.commit()

    def add_url(self, original_url, snapshot_url, timestamp):
        try:
            self.conn.execute("""
                INSERT INTO urls (original_url, snapshot_url, timestamp, discovered_at)
                VALUES (?, ?, ?, ?)
            """, (original_url, snapshot_url, timestamp, datetime.now().isoformat()))
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def get_pending_urls(self, limit=100):
        cur = self.conn.execute("""
            SELECT id, original_url, snapshot_url
            FROM urls
            WHERE status = 'pending' AND retries < 5
            LIMIT ?
        """, (limit,))
        return cur.fetchall()

    def bump_retry(self, url_id):
        self.conn.execute("UPDATE urls SET retries = retries + 1 WHERE id = ?", (url_id,))
        self.conn.commit()

    def update_url_status(self, url_id, status):
        self.conn.execute("UPDATE urls SET status = ? WHERE id = ?", (status, url_id))
        self.conn.commit()

    def save_article(self, url_id, data):
        cur = self.conn.execute("""
            INSERT INTO articles (url_id, title, content, excerpt, pub_date, category, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (url_id, data['title'], data['content'], data['excerpt'],
              data['pub_date'], data['category'], ','.join(data.get('tags', []))))
        self.conn.commit()
        return cur.lastrowid

    def get_url_mapping(self):
        cur = self.conn.execute("""
            SELECT u.original_url, a.wp_permalink
            FROM urls u
            JOIN articles a ON u.id = a.url_id
            WHERE a.wp_permalink IS NOT NULL
        """)
        return {row[0]: row[1] for row in cur.fetchall()}

    def log(self, level, message, context=None):
        self.conn.execute("""
            INSERT INTO logs (timestamp, level, message, context)
            VALUES (?, ?, ?, ?)
        """, (datetime.now().isoformat(), level, message, json.dumps(context or {}, ensure_ascii=False)))
        self.conn.commit()


# ============================== Discovery ==================================

class WaybackDiscovery:
    CDX_API = "https://web.archive.org/cdx/search/cdx"

    def __init__(self, db: Database, ua: str, after_date: Optional[str] = None, before_date: Optional[str] = None):
        self.db = db
        self.ua = ua
        self.after_date = after_date
        self.before_date = before_date
        self.session = make_requests_session(self.ua)

    def discover_urls(self, domain: str, limit: int = 1000) -> int:
        """Use CDX with repeated 'filter' params via list-of-tuples, collapse urlkey+digest."""
        params = [
            ('url', f'{domain}/*'),
            ('matchType', 'prefix'),
            ('filter', 'statuscode:200'),
            ('filter', 'mimetype:text/html'),
            ('collapse', 'urlkey'),
            ('collapse', 'digest'),
            ('limit', str(limit)),
            ('output', 'json'),
        ]
        if self.before_date:
            params.append(('to', self.before_date))
        if self.after_date:
            params.append(('from', self.after_date))

        resp = self.session.get(self.CDX_API, params=params, timeout=30)
        if resp.status_code != 200:
            self.db.log('error', f'CDX API failed: {resp.status_code}')
            return 0

        data = resp.json()
        headers = data[0] if data else []
        rows = data[1:] if len(data) > 1 else []

        discovered = 0
        for row in rows:
            item = dict(zip(headers, row))
            original_url = item.get('original', '')
            timestamp = item.get('timestamp', '')

            # Simple filtering
            low = original_url.lower()
            if any(x in low for x in ['/wp-admin/', '/feed/', '/tag/', '/author/', '.xml', '.json']):
                continue

            snapshot_url = f"https://web.archive.org/web/{timestamp}id_/{original_url}"
            self.db.add_url(original_url, snapshot_url, timestamp)
            discovered += 1

        self.db.log('info', f'Discovered {discovered} URLs from {domain}')
        return discovered


# ============================== Fetcher ====================================

class WaybackFetcher:
    def __init__(self, db: Database, ua: str, rate_limit=3, max_retries=5):
        self.db = db
        self.rate_limit = rate_limit
        self.semaphore = asyncio.Semaphore(rate_limit)
        self.ua = ua
        self.max_retries = max_retries

    async def fetch_page(self, session, url_id, snapshot_url):
        async with self.semaphore:
            for attempt in range(1, self.max_retries + 1):
                try:
                    await asyncio.sleep(1 / max(1, self.rate_limit))
                    async with session.get(snapshot_url, timeout=30) as resp:
                        if resp.status in (429, 500, 502, 503, 504):
                            await asyncio.sleep(2 ** attempt)
                            continue
                        if resp.status != 200:
                            self.db.bump_retry(url_id)
                            if attempt == self.max_retries:
                                self.db.update_url_status(url_id, 'failed')
                            await asyncio.sleep(0.2)
                            continue
                        html = await resp.text()
                        self.db.update_url_status(url_id, 'fetched')
                        return html
                except Exception as e:
                    self.db.log('error', f'Fetch failed for URL {url_id}', {'error': str(e), 'attempt': attempt})
                    self.db.bump_retry(url_id)
                    await asyncio.sleep(2 ** attempt)
            return None

    async def fetch_batch(self, urls_batch):
        headers = {"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"}
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            tasks = [self.fetch_page(session, uid, url) for uid, _, url in urls_batch]
            results = await asyncio.gather(*tasks)
            return list(zip([u[0] for u in urls_batch], results))


# ============================ Content Processor ============================

class ContentProcessor:
    def __init__(self, db: Database):
        self.db = db

    def clean_wayback_artifacts(self, html):
        soup = BeautifulSoup(html, 'html.parser')
        # Remove Wayback toolbar/scripts
        for elem in soup.find_all(['div', 'script', 'style']):
            _id = elem.get('id', '') or ''
            if isinstance(_id, str) and _id.lower().startswith('wm-'):
                elem.decompose()
        # Remove common trackers
        for script in soup.find_all('script'):
            st = (script.string or '')[:300].lower()
            if any(x in st for x in ['analytics', 'gtag', 'fbevents']):
                script.decompose()
        return soup

    def extract_metadata(self, soup, original_url):
        # Title
        title_tag = (soup.find('h1') or soup.find('title'))
        title = title_tag.get_text(strip=True) if title_tag else 'بدون عنوان'

        # Date
        pub_date = None
        date_elem = soup.find('time') or soup.find(class_=re.compile('date|publish', re.I))
        if date_elem:
            pub_date = date_elem.get('datetime') or date_elem.get_text(strip=True)
        pub_date_iso = to_iso_utc(pub_date)

        # Description
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        excerpt = meta_desc.get('content', '')[:300] if meta_desc else ''

        # Category from path
        path = urlparse(original_url).path
        parts = [p for p in path.split('/') if p and not p.endswith('.html')]
        category = parts[0] if parts else 'عام'

        return {
            'title': title.strip() or 'بدون عنوان',
            'excerpt': excerpt,
            'pub_date': pub_date_iso,
            'category': category,
            'tags': []
        }

    def extract_content(self, html, base_url):
        if trafilatura:
            try:
                out = trafilatura.extract(html, include_images=False, favor_recall=True, url=base_url, output='html')
                if out and len(out) > 100:
                    return out
            except Exception:
                pass
        # fallback
        soup = BeautifulSoup(html, 'html.parser')
        main = soup.find('article') or soup.find('main') or soup.find(class_=re.compile('content|post|article', re.I)) or soup.find('body')
        if not main:
            return "<p>فشل استخراج المحتوى</p>"
        for elem in main.find_all(['script', 'style', 'nav', 'aside', 'footer']):
            elem.decompose()
        return str(main)

    def extract_images(self, content_html, snapshot_url):
        soup = BeautifulSoup(content_html, 'html.parser')
        images = []
        for img in soup.find_all('img'):
            src = img.get('src', '')
            if not src or src.startswith('data:'):
                continue
            # force wayback image
            arch_src = wayback_image_url(snapshot_url, src)
            images.append({
                'url': arch_src,
                'alt': img.get('alt', '') or '',
                'original_src_in_html': src,
            })
        return images

    def absolutize_links(self, content_html, snapshot_url):
        """Make links absolute relative to snapshot base (id_)."""
        soup = BeautifulSoup(content_html, 'html.parser')
        base = re.sub(r'(https?://web\.archive\.org/web/\d+)[^/]+/(.*)', r'\1id_/', snapshot_url)
        for tag in soup.find_all(['a', 'img']):
            attr = 'href' if tag.name == 'a' else 'src'
            v = tag.get(attr)
            if not v:
                continue
            if v.startswith('data:'):
                continue
            # keep absolute as is, else join
            if not urlparse(v).scheme:
                tag[attr] = urljoin(base, v)
        return str(soup)

    def process_page(self, url_id, html, original_url, snapshot_url):
        if not html:
            return None

        soup = self.clean_wayback_artifacts(html)
        metadata = self.extract_metadata(soup, original_url)
        content_raw = self.extract_content(str(soup), original_url)
        content_abs = self.absolutize_links(content_raw, snapshot_url)
        images = self.extract_images(content_abs, snapshot_url)

        data = {
            **metadata,
            'content': content_abs,
            'images': images
        }
        article_id = self.db.save_article(url_id, data)

        # save images records
        for img in images:
            self.db.conn.execute("""
                INSERT INTO assets (article_id, original_url, alt_text)
                VALUES (?, ?, ?)
            """, (article_id, img['url'], img['alt']))
        self.db.conn.commit()

        return article_id


# ============================ WordPress Publisher ==========================

class WordPressPublisher:
    def __init__(self, site_url, username, app_password, db: Database, ua: str, default_category_id: int = 1):
        self.site_url = site_url.rstrip('/')
        self.auth = (username, app_password)
        self.db = db
        self.default_category_id = default_category_id
        self.session = make_requests_session(ua)

    def upload_image(self, image_url, alt_text: str = '') -> Optional[Dict]:
        try:
            # Download image (from Wayback preferably im_)
            img_resp = self.session.get(image_url, timeout=30)
            if img_resp.status_code != 200 or not img_resp.content:
                return None

            filename = Path(urlparse(image_url).path).name or 'image.jpg'
            files = {'file': (filename, img_resp.content, img_resp.headers.get('content-type', 'image/jpeg'))}

            upload_resp = self.session.post(
                f"{self.site_url}/wp-json/wp/v2/media",
                auth=self.auth,
                files=files,
                timeout=60
            )
            if upload_resp.status_code == 201:
                media_data = upload_resp.json()
                media_id = media_data['id']
                # set alt text if provided
                if alt_text:
                    self.session.post(
                        f"{self.site_url}/wp-json/wp/v2/media/{media_id}",
                        auth=self.auth,
                        json={"alt_text": alt_text},
                        timeout=30
                    )
                return {'id': media_id, 'url': media_data.get('source_url')}
        except Exception as e:
            self.db.log('error', f'Image upload failed: {image_url}', {'error': str(e)})
        return None

    def publish_article(self, article_id):
        cur = self.db.conn.execute("""
            SELECT url_id, title, content, excerpt, pub_date, category
            FROM articles WHERE id = ?
        """, (article_id,))
        row = cur.fetchone()
        if not row:
            return False

        url_id, title, content, excerpt, pub_date, category = row

        # images
        assets = self.db.conn.execute("""
            SELECT id, original_url, alt_text FROM assets
            WHERE article_id = ? AND uploaded = 0
        """, (article_id,)).fetchall()

        soup = BeautifulSoup(content, 'html.parser')
        featured_media_id = None

        for asset_id, img_url, alt_text in assets:
            media = self.upload_image(img_url, alt_text=alt_text or '')
            if media:
                # update DB
                self.db.conn.execute("""
                    UPDATE assets SET wp_media_id = ?, wp_url = ?, uploaded = 1
                    WHERE id = ?
                """, (media['id'], media['url'], asset_id))
                # replace in HTML
                for img in soup.find_all('img'):
                    src = img.get('src', '')
                    if src and img_url in src:
                        img['src'] = media['url']
                        if alt_text:
                            img['alt'] = alt_text
                if not featured_media_id:
                    featured_media_id = media['id']

        self.db.conn.commit()
        updated_content = str(soup)

        # prepare slug from original url path
        url_row = self.db.conn.execute("SELECT original_url FROM urls WHERE id = ?", (url_id,)).fetchone()
        original_url = url_row[0] if url_row else ''
        path = urlparse(original_url).path.rstrip('/')
        base_slug = slugify((path.split('/')[-1]) or 'index')

        post_data = {
            'title': title,
            'content': updated_content,
            'excerpt': excerpt,
            'status': 'draft',
            'date_gmt': pub_date,  # already iso utc
            'categories': [self.default_category_id],
            'slug': base_slug,
        }
        if featured_media_id:
            post_data['featured_media'] = featured_media_id

        resp = self.session.post(
            f"{self.site_url}/wp-json/wp/v2/posts",
            auth=self.auth,
            json=post_data,
            timeout=60
        )

        if resp.status_code == 201:
            post = resp.json()
            self.db.conn.execute("""
                UPDATE articles
                SET wp_post_id = ?, wp_permalink = ?, status = 'published'
                WHERE id = ?
            """, (post['id'], post.get('link', ''), article_id))
            self.db.conn.commit()
            print(f"✅ تم نشر: {title}")
            return True

        self.db.log('error', f'Failed to publish article {article_id}', {'status': resp.status_code, 'body': resp.text[:300]})
        return False


# ============================= Link Fixer ==================================

class LinkFixer:
    def __init__(self, db: Database, wp_publisher: WordPressPublisher):
        self.db = db
        self.wp = wp_publisher

    def fix_internal_links(self):
        url_map = self.db.get_url_mapping()

        cur = self.db.conn.execute("""
            SELECT id, wp_post_id, content
            FROM articles
            WHERE wp_post_id IS NOT NULL
        """)

        fixed_count = 0
        for article_id, post_id, content in cur.fetchall():
            soup = BeautifulSoup(content, 'html.parser')
            updated = False

            # Build normalized map for speed
            norm_map = {}
            for old_url, new_permalink in url_map.items():
                norm_map[normalize_url(old_url)] = new_permalink

            for link in soup.find_all('a', href=True):
                host, path = normalize_url(link['href'])
                key = (host, path)
                if key in norm_map:
                    if link['href'] != norm_map[key]:
                        link['href'] = norm_map[key]
                        updated = True

            if updated:
                new_content = str(soup)
                r = self.wp.session.post(
                    f"{self.wp.site_url}/wp-json/wp/v2/posts/{post_id}",
                    auth=self.wp.auth,
                    json={'content': new_content},
                    timeout=60
                )
                if r.status_code == 200:
                    self.db.conn.execute("UPDATE articles SET content = ? WHERE id = ?", (new_content, article_id))
                    fixed_count += 1

        self.db.conn.commit()
        print(f"🔗 تم إصلاح الروابط في {fixed_count} مقال")
        return fixed_count


# ============================== Pipeline ===================================

class ImportPipeline:
    def __init__(self, config):
        self.db = Database(config.get('db_path', 'wayback_import.db'))
        self.ua = config.get('user_agent', "Mozilla/5.0 (compatible; WaybackImporter/1.1)")
        self.discovery = WaybackDiscovery(
            self.db,
            ua=self.ua,
            after_date=config.get('after_date'),
            before_date=config.get('before_date'),
        )
        self.fetcher = WaybackFetcher(self.db, ua=self.ua, rate_limit=config.get('rate_limit', 3), max_retries=5)
        self.processor = ContentProcessor(self.db)
        self.wp = WordPressPublisher(
            config['wp_url'],
            config['wp_user'],
            config['wp_password'],
            self.db,
            ua=self.ua,
            default_category_id=config.get('default_category_id', 1)
        )
        self.link_fixer = LinkFixer(self.db, self.wp)
        self.batch_size = config.get('batch_size', 100)

    def run_discovery(self, domain, limit=1000):
        print("\n" + "="*60)
        print("المرحلة 1: اكتشاف الروابط")
        print("="*60)
        found = self.discovery.discover_urls(domain, limit=limit)
        print(f"✅ تم اكتشاف {found} رابط")

    async def run_fetching(self):
        print("\n" + "="*60)
        print("المرحلة 2: جلب ومعالجة المحتوى")
        print("="*60)

        pending = self.db.get_pending_urls(self.batch_size)
        if not pending:
            print("⚠️ لا توجد روابط معلقة")
            return

        print(f"📥 جلب {len(pending)} صفحة...")
        results = await self.fetcher.fetch_batch(pending)

        processed = 0
        for url_id, html in results:
            if html:
                original_url = next((u[1] for u in pending if u[0] == url_id), '')
                snapshot_url = next((u[2] for u in pending if u[0] == url_id), '')
                self.processor.process_page(url_id, html, original_url, snapshot_url)
                processed += 1

        print(f"✅ تمت معالجة {processed} مقال")

    def run_publishing(self):
        print("\n" + "="*60)
        print("المرحلة 3: النشر على WordPress")
        print("="*60)

        cur = self.db.conn.execute("""
            SELECT id FROM articles
            WHERE wp_post_id IS NULL
            LIMIT ?
        """, (self.batch_size,))
        articles = [row[0] for row in cur.fetchall()]

        if not articles:
            print("⚠️ لا توجد مقالات جديدة للنشر")
            return

        published = 0
        for article_id in articles:
            if self.wp.publish_article(article_id):
                published += 1
            time.sleep(0.5)  # respect server load

        print(f"✅ تم نشر {published} مقال")

    def run_link_fixing(self):
        print("\n" + "="*60)
        print("المرحلة 4: إصلاح الروابط الداخلية")
        print("="*60)
        self.link_fixer.fix_internal_links()

    def run_full_pipeline(self, domain, limit=1000):
        print("\n🚀 بدء عملية الاستيراد الكاملة")
        print(f"الموقع: {domain}")
        print(f"الحد الأقصى: {limit} رابط\n")

        self.run_discovery(domain, limit)
        asyncio.run(self.run_fetching())
        self.run_publishing()
        self.run_link_fixing()

        print("\n" + "="*60)
        print("✅ اكتملت العملية بنجاح!")
        print("="*60)
