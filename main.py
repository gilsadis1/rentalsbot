\
import re
import os
import smtplib
import sqlite3
import ssl
import yaml
import pytz
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

DB_PATH = "seen_listings.sqlite3"

def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_name TEXT NOT NULL,
            listing_key TEXT NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def normalize_url(base_url, href):
    if not href:
        return None
    href = href.strip()
    if href.startswith("javascript:") or href.startswith("#"):
        return None
    return urljoin(base_url, href)

def is_listing_link(url, domain_hint):
    if not url:
        return False
    parsed = urlparse(url)
    if domain_hint and domain_hint not in parsed.netloc:
        return False

    path_q = (parsed.path or "") + "?" + (parsed.query or "")
    patterns = [
        r"itemId=\d+",
        r"/item/\d+",
        r"/rent/\d+",
        r"/realestate/item",
        r"/realestate/rent/.+/\d+",
        r"/nadlan/.+/\d+",
    ]
    return any(re.search(pat, path_q) for pat in patterns)

def extract_text_nearby(a_tag):
    container = a_tag.find_parent(["article", "li", "div"]) or a_tag
    text = " ".join((container.get_text(separator=" ", strip=True) or "").split())
    return text[:400]

def extract_image_nearby(a_tag, base_url):
    """Extract the best quality image URL near a listing link."""
    container = a_tag.find_parent(["article", "li", "div"]) or a_tag
    
    # Find first real img tag (not lazy placeholder)
    for img in container.find_all("img"):
        # Prefer data-src (usually higher quality) over src (often placeholder)
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if src:
            # Make absolute URL
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(base_url, src)
            # Skip placeholder/icon/logo images
            src_lower = src.lower()
            if ("placeholder" not in src_lower and 
                "icon" not in src_lower and 
                "logo" not in src_lower and
                "avatar" not in src_lower and
                "data:image" not in src_lower and
                len(src) > 20):
                return src
    
    # Try background-image in style
    for elem in container.find_all(style=True):
        style = elem.get("style", "")
        match = re.search(r'url\(["\']?([^"\')+]+)["\']?\)', style)
        if match:
            src = match.group(1)
            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = urljoin(base_url, src)
            if "placeholder" not in src.lower():
                return src
    
    return None

def extract_price(text):
    m = re.search(r"(\d{3,6})\s*₪", text.replace(",", ""))
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def extract_rooms(text):
    m = re.search(r"(\d+(?:\.\d)?)\s*חדר", text)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None

def extract_size(text):
    m = re.search(r"(\d{2,4})\s*(?:מ\"ר|מטר)", text)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None

def passes_filters(text, filters):
    kw_inc = filters.get("must_include_keywords") or []
    kw_exc = filters.get("exclude_keywords") or []
    min_rooms = filters.get("min_rooms")
    min_size = filters.get("min_size_sqm")
    max_price = filters.get("max_price_nis")

    t = text or ""
    t_norm = t.lower()

    if kw_inc:
        if not any(kw.lower() in t_norm for kw in kw_inc):
            return False
    if kw_exc:
        if any(kw.lower() in t_norm for kw in kw_exc):
            return False

    rooms = extract_rooms(t)
    if min_rooms and rooms is not None and rooms < float(min_rooms):
        return False

    size = extract_size(t)
    if min_size and size is not None and size < int(min_size):
        return False

    price = extract_price(t)
    if max_price and price is not None and price > int(max_price):
        return False

    return True

def fetch_listings_for_source(source, filters):
    name = source["name"]
    url = source["url"]
    domain_hint = source.get("domain_hint", None)

    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (compatible; TelAvivRentalBot/1.0)"
        })
        resp.raise_for_status()
    except Exception as e:
        return name, [], f"שגיאה בטעינת {name}: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.find_all("a", href=True)

    items = []
    seen_urls = set()  # Avoid duplicates
    for a in links:
        abs_url = normalize_url(url, a.get("href"))
        if not abs_url:
            continue
        if abs_url in seen_urls:
            continue
        if is_listing_link(abs_url, domain_hint):
            text = extract_text_nearby(a)
            if passes_filters(text, filters):
                image = extract_image_nearby(a, url)
                items.append({"url": abs_url, "text": text, "image": image})
                seen_urls.add(abs_url)
    return name, items, None

def mark_and_filter_new(conn, source_name, items):
    cur = conn.cursor()
    new_items = []
    now = datetime.utcnow().isoformat()

    for it in items:
        key = it["url"]
        try:
            cur.execute(
                "INSERT INTO seen (source_name, listing_key, first_seen_at) VALUES (?, ?, ?)",
                (source_name, key, now),
            )
            new_items.append(it)
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    return new_items

def build_email_html(date_str, groups):
    # Mobile-friendly RTL email layout - stacks on small screens
    style = """
    <!DOCTYPE html>
    <html dir="rtl" lang="he">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body { 
                font-family: Arial, Helvetica, sans-serif; 
                background: #f0f2f5;
                margin: 0;
                padding: 15px;
                font-size: 16px;
                line-height: 1.6;
            }
            .container {
                max-width: 600px;
                margin: 0 auto;
                background: #fff;
                border-radius: 16px;
                padding: 20px;
            }
            h1 {
                color: #1a1a1a;
                font-size: 24px;
                margin: 0 0 8px 0;
            }
            .subtitle {
                color: #666;
                font-size: 14px;
                margin-bottom: 25px;
                padding-bottom: 15px;
                border-bottom: 2px solid #e8e8e8;
            }
            h2 {
                color: #333;
                font-size: 17px;
                margin: 20px 0 12px 0;
            }
            .listing {
                display: block;
                text-decoration: none;
                color: inherit;
                border: 1px solid #e0e0e0;
                border-radius: 12px;
                margin: 15px 0;
                background: #fafafa;
                overflow: hidden;
            }
            .listing-img {
                width: 100%;
                height: 180px;
                object-fit: cover;
                display: block;
            }
            .no-image {
                width: 100%;
                height: 100px;
                background: #e8e8e8;
                text-align: center;
                line-height: 100px;
                color: #999;
                font-size: 14px;
            }
            .listing-content {
                padding: 15px;
            }
            .listing-text {
                color: #333;
                font-size: 15px;
                line-height: 1.7;
                margin-bottom: 12px;
            }
            .listing-cta {
                color: #2196F3;
                font-size: 14px;
                font-weight: bold;
            }
            .empty-msg {
                text-align: center;
                padding: 40px 20px;
                color: #666;
                font-size: 17px;
                background: #f9f9f9;
                border-radius: 12px;
            }
            .footer {
                margin-top: 25px;
                padding-top: 15px;
                border-top: 1px solid #eee;
                color: #999;
                font-size: 11px;
                text-align: center;
            }
        </style>
    </head>
    <body dir="rtl" style="direction:rtl;text-align:right;">
    <div class="container" dir="rtl" align="right">
    """
    
    parts = [style]
    parts.append('<h1 dir="rtl" align="right">🏠 דירות חדשות</h1>')
    parts.append(f'<div class="subtitle" dir="rtl" align="right">{date_str}</div>')
    
    any_items = False
    
    for g in groups:
        source_name = g["source"]
        items = g["items"]
        if not items:
            continue
        any_items = True
        parts.append(f'<h2 dir="rtl" align="right">{source_name} ({len(items)} מודעות)</h2>')
        
        for it in items:
            url = it["url"]
            text = it["text"]
            image = it.get("image")
            snippet = (text[:300] + "…") if len(text) > 320 else text
            
            # Build image HTML
            if image:
                img_html = f'<img src="{image}" class="listing-img" alt="">'
            else:
                img_html = '<div class="no-image">📷 אין תמונה</div>'
            
            # Mobile-friendly: image on top, text below
            parts.append(f'''
            <a href="{url}" class="listing" target="_blank">
                {img_html}
                <div class="listing-content" dir="rtl" align="right">
                    <div class="listing-text" dir="rtl">{snippet}</div>
                    <div class="listing-cta">לצפייה במודעה ←</div>
                </div>
            </a>
            ''')

    if not any_items:
        parts.append('<div class="empty-msg" dir="rtl">לא נמצאו מודעות חדשות 🔍</div>')
    
    parts.append('<div class="footer">נשלח אוטומטית ע״י Rental Bot</div>')
    parts.append('</div></body></html>')

    return "\n".join(parts)

def send_email(config, subject, html_body):
    from_email = config["email"]["from_email"]
    to_emails = config["email"]["to_emails"]
    from_name = config["email"].get("from_name", "Rental Bot")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = ", ".join(to_emails)

    part_html = MIMEText(html_body, "html", _charset="utf-8")
    msg.attach(part_html)

    smtp_host = config["email"]["smtp_host"]
    smtp_port = int(config["email"]["smtp_port"])
    app_pass = os.environ.get("GMAIL_APP_PASSWORD")

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls(context=context)
        server.login(from_email, app_pass)
        server.sendmail(from_email, to_emails, msg.as_string())

def main():
    config = load_config()
    conn = ensure_db()

    groups = []
    errors = []

    for src in config.get("sources", []):
        source_name, items, err = fetch_listings_for_source(src, config.get("filters", {}))
        if err:
            errors.append(err)
        new_items = mark_and_filter_new(conn, source_name, items)
        groups.append({"source": source_name, "items": new_items})

    tz = pytz.timezone("Asia/Jerusalem")
    now = datetime.now(tz)
    today = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M")

    html = build_email_html(today, groups)
    if errors:
        html += "<hr><p><b>אזהרות:</b><br>" + "<br>".join(errors) + "</p>"

    # Include time in subject to prevent Gmail threading
    subject = f"🏠 דירות חדשות – {today} {time_str}"
    send_email(config, subject, html)

if __name__ == "__main__":
    main()
