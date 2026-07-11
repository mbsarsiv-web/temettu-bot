import re
import time
import sys
import os
import json
from io import StringIO
import requests
import pandas as pd

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_URL = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/sirket-karti.aspx?hisse={}"
LIST_URL = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/sirket-karti.aspx?hisse=AYGAZ"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

REQUEST_DELAY = 0.6
RETRY_COUNT = 3
RETRY_WAIT = 2.0
DIVIDEND_COLUMN_HINTS = ["Dağ. Tarihi", "Temettü Verim", "Hisse Başı"]

def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def fetch_with_retry(session, url):
    last_err = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = session.get(url, timeout=20)
            if resp.status_code == 200:
                return resp.text
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(RETRY_WAIT * attempt)
    return None

def get_all_tickers(session):
    html = fetch_with_retry(session, LIST_URL)
    if not html:
        raise RuntimeError("Hisse listesi alinamadi.")
    pairs = re.findall(r'>([A-Z][A-Z0-9]{1,5})\s*\|\s*([^<\n]{2,60})<', html)
    tickers = []
    seen = set()
    for code, _name in pairs:
        code = code.strip()
        if code not in seen and re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", code):
            seen.add(code)
            tickers.append(code)
    return tickers

def find_dividend_table(html):
    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        return None
    for tbl in tables:
        cols = [str(c) for c in tbl.columns]
        joined = " ".join(cols)
        hits = sum(1 for hint in DIVIDEND_COLUMN_HINTS if hint in joined)
        if hits >= 2:
            return tbl
    return None

def clean_dividend_table(df, kod):
    rename_map = {}
    for c in df.columns:
        c_str = str(c)
        if "Dağ. Tarihi" in c_str: rename_map[c] = "Dagitim_Tarihi"
        elif "Temettü Verim" in c_str: rename_map[c] = "Temettu_Verim_%"
        elif "Hisse Başı" in c_str: rename_map[c] = "Hisse_Basi_TL"
        elif "Toplam Temettü" in c_str: rename_map[c] = "Toplam_Temettu_TL"
        elif "Dağıtma Oranı" in c_str: rename_map[c] = "Dagitma_Orani_%"
        elif c_str.strip() == "Kod": rename_map[c] = "Kod"
    df = df.rename(columns=rename_map)

    keep_cols = [c for c in ["Kod", "Dagitim_Tarihi", "Temettu_Verim_%", "Hisse_Basi_TL", "Toplam_Temettu_TL", "Dagitma_Orani_%"] if c in df.columns]
    df = df[keep_cols].copy()

    if "Dagitim_Tarihi" in df.columns:
        df = df[df["Dagitim_Tarihi"].astype(str).str.match(r"^\d{2}\.\d{2}\.\d{4}$")]

    numeric_cols = ["Temettu_Verim_%", "Hisse_Basi_TL", "Toplam_Temettu_TL", "Dagitma_Orani_%"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace('.', '', regex=False).str.replace(',', '.', regex=False)
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df = df.drop_duplicates()
    if "Kod" not in df.columns: df.insert(0, "Kod", kod)
    else: df["Kod"] = kod
    return df

def upload_to_drive(filename):
    service_account_info = json.loads(os.environ['GCP_SERVICE_ACCOUNT_JSON'])
    folder_id = os.environ['DRIVE_FOLDER_ID']

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=['https://www.googleapis.com/auth/drive']
    )
    service = build('drive', 'v3', credentials=credentials)

    query = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    results = service.files().list(q=query, fields="files(id)").execute()
    items = results.get('files', [])

    media = MediaFileUpload(filename, mimetype='text/csv', resumable=True)

    if items:
        file_id = items[0]['id']
        service.files().update(fileId=file_id, media_body=media).execute()
    else:
        file_metadata = {'name': filename, 'parents': [folder_id]}
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def main():
    session = get_session()
    tickers = get_all_tickers(session)
    
    all_rows = []
    for i, kod in enumerate(tickers, start=1):
        html = fetch_with_retry(session, BASE_URL.format(kod))
        if not html: continue
        tbl = find_dividend_table(html)
        if tbl is None: continue
        cleaned = clean_dividend_table(tbl, kod)
        if not cleaned.empty:
            all_rows.append(cleaned)
        time.sleep(REQUEST_DELAY)
            
    if not all_rows:
        sys.exit(1)

    result = pd.concat(all_rows, ignore_index=True)
    out_path = "temettu_tum_hisseler.csv"
    result.to_csv(out_path, index=False, encoding="utf-8")
    upload_to_drive(out_path)

if __name__ == "__main__":
    main()
