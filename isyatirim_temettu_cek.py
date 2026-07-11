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
API_URL = "https://www.isyatirim.com.tr/_layouts/15/IsYatirim.Website/StockInfo/CompanyInfoAjax.aspx/GetSermayeArttirimlari"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

REQUEST_DELAY = 0.5
RETRY_COUNT = 3
RETRY_WAIT = 2.0
DIVIDEND_COLUMN_HINTS = ["Dağ. Tarihi", "Temettü Verim", "Hisse Başı"]

def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def fetch_with_retry(session, url, method="GET", json_payload=None):
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            if method == "POST":
                resp = session.post(url, json=json_payload, timeout=20)
            else:
                resp = session.get(url, timeout=20)
            if resp.status_code == 200:
                return resp.text
        except requests.RequestException:
            pass
        time.sleep(RETRY_WAIT * attempt)
    return None

def get_all_tickers(session):
    html = fetch_with_retry(session, LIST_URL)
    if not html:
        raise RuntimeError("Hisse listesi alınamadı.")
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
    df = df.rename(columns=rename_map)
    keep_cols = [c for c in ["Dagitim_Tarihi", "Temettu_Verim_%"] if c in df.columns]
    df = df[keep_cols].copy()
    if "Dagitim_Tarihi" in df.columns:
        df = df[df["Dagitim_Tarihi"].astype(str).str.match(r"^\d{2}\.\d{2}\.\d{4}$")]
    df = df.drop_duplicates()
    df.insert(0, "Kod", kod)
    return df

def fetch_api_data(session, tanim_kodu):
    payload = {"hisseKodu": "", "hisseTanimKodu": tanim_kodu, "yil": 0, "zaman": "HEPSI", "endeksKodu": "09", "sektorKodu": ""}
    res_text = fetch_with_retry(session, API_URL, method="POST", json_payload=payload)
    if not res_text: return []
    try:
        js = json.loads(res_text)
        data = js.get("value") or js.get("d") or js
        if isinstance(data, str): data = json.loads(data)
        if isinstance(data, list): return data
        for k in data:
            if isinstance(data[k], list): return data[k]
    except Exception:
        pass
    return []

def parse_turkce_sayi(val):
    if pd.isna(val) or not val: return 0.0
    if isinstance(val, (int, float)): return float(val)
    str_val = str(val).strip().replace(".", "").replace(",", ".")
    try: return float(str_val)
    except ValueError: return 0.0

def get_mantiki_yil(val):
    if pd.isna(val) or not val: return None
    str_val = str(val).strip()
    m = re.search(r'Date\(([-0-9]+)\)', str_val)
    if m:
        ms = int(m.group(1))
        try: return str(time.gmtime(ms/1000).tm_year)
        except Exception: return None
    year_match = re.search(r'\b(19[8-9]\d|20\d\d)\b', str_val)
    if year_match: return year_match.group(1)
    return None

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
        service.files().update(fileId=items[0]['id'], media_body=media).execute()
    else:
        file_metadata = {'name': filename, 'parents': [folder_id]}
        service.files().create(body=file_metadata, media_body=media, fields='id').execute()

def main():
    session = get_session()
    tickers = get_all_tickers(session)
    
    # 1. Şirket Kartlarından Temettü Verimleri Çekiliyor
    all_rows = []
    for kod in tickers:
        html = fetch_with_retry(session, BASE_URL.format(kod))
        if not html: continue
        tbl = find_dividend_table(html)
        if tbl is None: continue
        cleaned = clean_dividend_table(tbl, kod)
        if not cleaned.empty: all_rows.append(cleaned)
        time.sleep(REQUEST_DELAY)
            
    df_verim_final = pd.DataFrame(columns=["Kod", "Yil", "Temettu_Verim_%"])
    if all_rows:
        df_scraped = pd.concat(all_rows, ignore_index=True)
        df_scraped["Yil"] = df_scraped["Dagitim_Tarihi"].apply(get_mantiki_yil)
        df_scraped["Temettu_Verim_%"] = df_scraped["Temettu_Verim_%"].astype(str).str.replace('.', '', regex=False).str.replace(',', '.', regex=False)
        df_scraped["Temettu_Verim_%"] = pd.to_numeric(df_scraped["Temettu_Verim_%"], errors='coerce').fillna(0.0)
        df_verim_final = df_scraped.groupby(["Kod", "Yil"], as_index=False)["Temettu_Verim_%"].sum()

    # 2. API'den Nakit Temettü Tutarları Çekiliyor
    raw_api_temettu = fetch_api_data(session, "04")
    processed_dividends = []
    for satir in raw_api_temettu:
        kod = satir.get("SHHE_HS_KOD") or satir.get("HISSE_KODU") or ""
        tarih = satir.get("SHHE_TARIH") or satir.get("TARIH") or ""
        if not kod or not tarih:
            for k, v in satir.items():
                if not kod and "KOD" in k.upper(): kod = v
                if not tarih and "TARIH" in k.upper(): tarih = v
        nakit_temettu = 0.0
        for k, v in satir.items():
            if ("TEM" in k.upper() and "TUTAR" in k.upper()) or "NAKIT" in k.upper():
                val = parse_turkce_sayi(v)
                if val > nakit_temettu: nakit_temettu = val
        if kod and tarih and nakit_temettu > 0:
            kod = str(kod).strip().upper()
            yil = get_mantiki_yil(tarih)
            if yil: processed_dividends.append({"Kod": kod, "Yil": yil, "Tutar": nakit_temettu})
            
    df_div_final = pd.DataFrame(columns=["Kod", "Yil", "Tutar"])
    if processed_dividends:
        df_div_final = pd.DataFrame(processed_dividends)
        df_div_final = df_div_final.groupby(["Kod", "Yil"], as_index=False)["Tutar"].sum()

    # 3. API'den Halka Arz Yılları Çekiliyor
    raw_api_arz = fetch_api_data(session, "99")
    processed_ipo = []
    for satir in raw_api_arz:
        kod = satir.get("SHHE_HS_KOD") or satir.get("HISSE_KODU") or ""
        tarih = satir.get("SHHE_TARIH") or satir.get("TARIH") or ""
        if not kod or not tarih:
            for k, v in satir.items():
                if not kod and "KOD" in k.upper(): kod = v
                if not tarih and "TARIH" in k.upper(): tarih = v
        if kod and tarih:
            kod = str(kod).strip().upper()
            yil = get_mantiki_yil(tarih)
            if yil: processed_ipo.append({"Kod": kod, "Arz_Yili": int(yil)})
            
    df_ipo_final = pd.DataFrame(columns=["Kod", "Arz_Yili"])
    if processed_ipo:
        df_ipo_final = pd.DataFrame(processed_ipo)
        df_ipo_final = df_ipo_final.groupby("Kod", as_index=False)["Arz_Yili"].min()

    # 4. Veriler Tek Master Dosyada Birleştiriliyor
    if df_div_final.empty and df_verim_final.empty:
        sys.exit(1)
        
    df_master = pd.merge(df_div_final, df_verim_final, on=["Kod", "Yil"], how="outer")
    df_master = pd.merge(df_master, df_ipo_final, on="Kod", how="outer")
    
    df_master["Tutar"] = df_master["Tutar"].fillna(0.0)
    df_master["Temettu_Verim_%"] = df_master["Temettu_Verim_%"].fillna(0.0)
    df_master["Yil"] = df_master["Yil"].fillna("")
    df_master["Arz_Yili"] = df_master["Arz_Yili"].fillna("")
    df_master = df_master[df_master["Kod"].str.strip() != ""]

    out_path = "bist_temettu_master.csv"
    df_master.to_csv(out_path, index=False, encoding="utf-8")
    upload_to_drive(out_path)

if __name__ == "__main__":
    main()
