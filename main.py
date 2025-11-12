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
    for a in links:
        abs_url = normalize_url(url, a.get("href"))
        if not abs_url:
            continue
        if is_listing_link(abs_url, domain_hint):
            text = extract_text_nearby(a)
            if passes_filters(text, filters):
                items.append({"url": abs_url, "text": text})
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
    parts = [f"<h2>דירות חדשות – {date_str}</h2>"]
    any_items = False
    for g in groups:
        source_name = g["source"]
        items = g["items"]
        if not items:
            continue
        any_items = True
        parts.append(f"<h3>{source_name}</h3><ul>")
        for it in items:
            url = it["url"]
            text = it["text"]
            snippet = (text[:240] + "…") if len(text) > 260 else text
            parts.append(f'<li><a href="{url}">{url}</a><br><small>{snippet}</small></li>')
        parts.append("</ul>")

    if not any_items:
        parts.append("<p>לא נמצאו מודעות חדשות היום לפי הסינון שלך.</p>")

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
    today = datetime.now(tz).strftime("%d.%m.%Y")

    html = build_email_html(today, groups)
    if errors:
        html += "<hr><p><b>אזהרות:</b><br>" + "<br>".join(errors) + "</p>"

    subject = f"עדכון דירות בת״א – {today}"
    send_email(config, subject, html)

if __name__ == "__main__":
    main()
