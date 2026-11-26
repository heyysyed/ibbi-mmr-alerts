import hashlib
import io
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import List
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pypdf import PdfReader
import requests

load_dotenv()

IBBI_AUCTION_URL = "https://ibbi.gov.in/liquidation-auction-notices/lists"
IBBI_PUBLIC_ANN_URL = "https://ibbi.gov.in/public-announcement"
BAANKNET_URL = "https://ibbi.baanknet.com/auction-listing"

MMR_LOCATIONS = [
    "mumbai", "mumbai suburban", "thane", "navi mumbai", "kalyan",
    "dombivli", "bhiwandi", "vasai", "virar", "palghar", "panvel",
    "chembur", "andheri", "bandra", "worli", "lower parel", "parel",
    "fort", "malad", "borivali", "kandivali", "mulund", "ghatkopar",
    "vashi", "belapur", "airoli", "nerul", "kharghar", "ulwe", "shahapur"
]

PROPERTY_TERMS = [
    "land", "building", "flat", "office", "commercial", "residential",
    "plot", "godown", "warehouse", "industrial", "premises", "sq. mtr",
    "sq. ft", "sq.ft", "cts", "property", "immovable", "gala", "shop",
    "going concern", "resolution plan", "form g", "eoi"
]


@dataclass
class AlertRecord:
    category: str
    entity: str
    asset_or_plan: str
    reserve_or_date: str
    url: str

    @property
    def uid(self) -> str:
        payload = f"{self.category}|{self.entity}|{self.reserve_or_date}|{self.url}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def init_db(path: str = "alerts.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_alerts (
            uid TEXT PRIMARY KEY,
            category TEXT,
            entity TEXT,
            url TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def extract_pdf_snippet(pdf_url: str, max_chars: int = 800) -> str:
    try:
        r = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        reader = PdfReader(io.BytesIO(r.content))
        chunks = []
        for page in reader.pages[:3]:
            chunks.append(page.extract_text() or "")
            if sum(len(c) for c in chunks) >= max_chars:
                break
        return re.sub(r"\s+", " ", " ".join(chunks))[:max_chars]
    except Exception:
        return ""


def fetch_ibbi_table(url: str, category: str) -> List[AlertRecord]:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    records: List[AlertRecord] = []

    for row in soup.select("table tbody tr"):
        cols = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cols) < 4:
            continue

        link_tag = row.find("a", href=True)
        link = link_tag["href"] if link_tag else url
        if link.startswith("/"):
            link = f"https://ibbi.gov.in{link}"

        row_text = " | ".join(cols)
        entity = cols[2] if len(cols) > 2 else cols[0]
        extra = extract_pdf_snippet(link) if link.lower().endswith(".pdf") else ""
        full_desc = row_text if not extra else f"{row_text} | PDF: {extra[:220]}"

        records.append(
            AlertRecord(
                category=category,
                entity=entity,
                asset_or_plan=full_desc,
                reserve_or_date=cols[3] if len(cols) > 3 else "See notice",
                url=link,
            )
        )
    return records


def fetch_baanknet_listings() -> List[AlertRecord]:
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(BAANKNET_URL, headers=headers, timeout=25)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    records: List[AlertRecord] = []

    page_text = soup.get_text("\n", strip=True)
    blocks = re.split(r"(?=Auction ID:\s*\d+)", page_text)
    for blk in blocks:
        m_id = re.search(r"Auction ID:\s*(\d+)", blk)
        if not m_id:
            continue
        auction_id = m_id.group(1)
        m_price = re.search(r"Reserve Price\s*([^\n]+)", blk)
        m_date = re.search(r"Auction Start Date:\s*([^\n]+)", blk)
        clean_blk = re.sub(r"\s+", " ", blk).strip()

        records.append(
            AlertRecord(
                category="BAANKNET IBC Auction",
                entity=f"Auction ID {auction_id}",
                asset_or_plan=clean_blk[:350],
                reserve_or_date=f"Reserve: {m_price.group(1) if m_price else 'N/A'} | Start: {m_date.group(1) if m_date else 'Check portal'}",
                url=BAANKNET_URL,
            )
        )
    return records


def matches_mmr_property(rec: AlertRecord) -> bool:
    text = f"{rec.entity} {rec.asset_or_plan}".lower()
    has_location = any(re.search(rf"\b{re.escape(loc)}\b", text) for loc in MMR_LOCATIONS)
    has_property = any(term in text for term in PROPERTY_TERMS)
    return has_location and has_property


def send_telegram_alert(rec: AlertRecord) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    message = (
        f"🚨 {rec.category} (MMR)\n\n"
        f"Entity: {rec.entity}\n"
        f"Details: {rec.asset_or_plan[:320]}\n"
        f"Price/Date: {rec.reserve_or_date}\n"
        f"Link: {rec.url}"
    )

    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    resp.raise_for_status()


def run_pipeline() -> None:
    conn = init_db()
    try:
        candidates: List[AlertRecord] = []
        for fn, label in [
            (lambda: fetch_ibbi_table(IBBI_AUCTION_URL, "IBBI Liquidation Auction"), "IBBI Auctions"),
            (lambda: fetch_ibbi_table(IBBI_PUBLIC_ANN_URL, "IBBI Public Announcement / Resolution Plan"), "IBBI Public Announcements"),
            (fetch_baanknet_listings, "BAANKNET"),
        ]:
            try:
                rows = fn()
                print(f"[{label}] Fetched {len(rows)} items.")
                candidates.extend(rows)
            except Exception as exc:
                print(f"[{label}] Warning: {exc}")

        sent = 0
        for rec in candidates:
            if not matches_mmr_property(rec):
                continue
            if conn.execute("SELECT 1 FROM sent_alerts WHERE uid = ?", (rec.uid,)).fetchone():
                continue

            send_telegram_alert(rec)
            conn.execute(
                "INSERT INTO sent_alerts (uid, category, entity, url) VALUES (?, ?, ?, ?)",
                (rec.uid, rec.category, rec.entity, rec.url),
            )
            conn.commit()
            sent += 1
            print(f"Sent alert: {rec.category} -> {rec.entity}")
            time.sleep(1)

        print(f"Finished. New MMR alerts sent: {sent}")
    finally:
        conn.close()


def send_test_message() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "✅ Cloud Test Alert: GitHub Actions -> Telegram pipeline is live and working!",
        },
        timeout=20,
    )
    resp.raise_for_status()
    print("Test Telegram message sent successfully.")


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        send_test_message()
    else:
        run_pipeline()
