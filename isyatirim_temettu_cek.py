import re
import time
import sys
import os
import json
from io import StringIO
import requests
import pandas as pd
import yfinance as yf

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_URL = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/sirket-karti.aspx?hisse={}"
LIST_URL = "https://www.isyatirim.com.tr/tr-tr/analiz/hisse/Sayfalar/sirket-karti.aspx?hisse=AYGAZ"
API_URL = "https://www.isyatirim.com.tr/_layouts/15/IsYatirim.Website/StockInfo/CompanyInfoAjax.aspx/GetSermayeArttirimlari"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive"
}

REQUEST_DELAY = 0.5
RETRY_COUNT = 3
RETRY_WAIT = 2.0
DIVIDEND_COLUMN_HINTS = ["Dağ. Tarihi", "Temettü Verim", "Hisse Başı"]

# Dolar kuru önbelleği
USD_CACHE = {}

def load_usd_rates():
    """yfinance üzerinden geçmiş USD/TRY kurlarını güvenli bir şekilde çeker ve hafızaya alır."""
    print("yfinance üzerinden USD/TRY geçmiş kurları çekiliyor...")
    try:
        # Yahoo Finance bot engelini aşmak için custom session ekliyoruz
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        
        # USDTRY=X sembolü ile çekimi önceliklendiriyoruz
        df_usd = yf.download('USDTRY=X', start='2005-01-01', progress=False, session=session)
        if df_usd.empty:
            df_usd = yf.download('TRY=X', start='2005-01-01', progress=False, session=session)
            
        if not df_usd.empty:
            close_series = df_usd['Close']
            if isinstance(close_series, pd.DataFrame):
                close_series = close_series.iloc[:, 0]
            
            close_series = close_series.resample('D').ffill()
            for date, price in close_series.items():
                date_str = date.strftime('%Y-%m-%d')
                USD_CACHE[date_str] = float(price)
        print(f"Toplam {len(USD_CACHE)} günlük kur verisi hafızaya alındı.")
    except Exception as e:
        print(f"Kur verisi çekilirken hata oluştu: {e}")

def get_usd_rate(date_str):
    if not date_str: return None
    if date_str in USD_CACHE: return USD_CACHE[date_str]
    keys = sorted(USD_CACHE.keys())
    if not keys: return None
    if date_str < keys[0]: return USD_CACHE[keys[0]]
    for k in reversed(keys):
        if k <= date_str: return USD_CACHE[k]
    return None

def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def fetch_with_retry(session, url, method="GET", json_payload=None):
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            if method == "POST":
                resp = session.post(url, json=json_payload, timeout=25)
            else:
                resp = session.get(url, timeout=25)
            if resp.status_code == 200:
                return resp.text
            else:
                print(f"Uyarı: HTTP {resp.status_code} alındı.")
        except requests.RequestException as e:
            print(f"Bağlantı Hatası: {e}")
        time.sleep(RETRY_WAIT * attempt)
    return None

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
    except Exception: pass
    return []

def get_all_tickers(session):
    html = fetch_with_retry(session, LIST_URL)
    tickers = set()
    if html:
        pairs = re.findall(r'>([A-Z][A-Z0-9]{1,5})\s*\|\s*([^<\n]{2,60})<', html)
        for code, _name in pairs:
            if re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", code.strip()): tickers.add(code.strip())
    if not tickers:
        api_data = fetch_api_data(session, "04")
        for satir in api_data:
            kod = satir.get("SHHE_HS_KOD") or satir.get("HISSE_KODU") or ""
            if not kod:
                for k, v in satir.items():
                    if "KOD" in k.upper(): kod = v; break
            if kod and isinstance(kod, str):
                kod = kod.strip().upper()
                if re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", kod): tickers.add(kod)
    if not tickers: raise RuntimeError("Hisse listesi alınamadı.")
    return sorted(list(tickers))

def find_dividend_table(html):
    try: tables = pd.read_html(StringIO(html))
    except ValueError: return None
    for tbl in tables:
        if sum(1 for hint in DIVIDEND_COLUMN_HINTS if hint in " ".join([str(c) for c in tbl.columns])) >= 2: return tbl
    return None

def clean_dividend_table(df, kod):
    rename_map = {}
    for c in df.columns:
        if "Dağ. Tarihi" in str(c): rename_map[c] = "Dagitim_Tarihi"
        elif "Temettü Verim" in str(c): rename_map[c] = "Temettu_Verim_%"
    df = df.rename(columns=rename_map)
    keep_cols = [c for c in ["Dagitim_Tarihi", "Temettu_Verim_%"] if c in df.columns]
    df = df[keep_cols].copy()
    if "Dagitim_Tarihi" in df.columns:
        df = df[df["Dagitim_Tarihi"].notna()]
        df = df[df["Dagitim_Tarihi"].astype(str).str.strip() != ""]
    df = df.drop_duplicates()
    df.insert(0, "Kod", kod)
    return df

def parse_turkce_sayi(val):
    if pd.isna(val) or not val: return 0.0
    if isinstance(val, (int, float)): return float(val)
    try: return float(str(val).strip().replace(".", "").replace(",", "."))
    except ValueError: return 0.0

def get_exact_date(val):
    if pd.isna(val) or not val: return None
    m = re.search(r'Date\(([-0-9]+)\)', str(val).strip())
    if m:
        try: return time.strftime('%Y-%m-%d', time.gmtime(int(m.group(1))/1000))
        except Exception: return None
    return None

def get_mantiki_yil(val):
    if pd.isna(val) or not val: return None
    m = re.search(r'Date\(([-0-9]+)\)', str(val).strip())
    if m:
        try: return str(time.gmtime(int(m.group(1))/1000).tm_year)
        except Exception: return None
    year_match = re.search(r'\b(19[8-9]\d|20\d\d)\b', str(val).strip())
    if year_match: return year_match.group(1)
    return None

def parse_yield(val):
    if pd.isna(val): return 0.0
    s = str(val).strip()
    if not s: return 0.0
    try: return float(s.replace('.', '').replace(',', '.')) if ',' in s else float(s)
    except ValueError: return 0.0

def upload_to_drive(filename):
    service_account_info = json.loads(os.environ['GCP_SERVICE_ACCOUNT_JSON'])
    folder_id = os.environ['DRIVE_FOLDER_ID']
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info, scopes=['https://www.googleapis.com/auth/drive']
    )
    service = build('drive', 'v3', credentials=credentials)
    query = f"'{folder_id}' in parents and name='{filename}' and trashed=false"
    items = service.files().list(q=query, fields="files(id)").execute().get('files', [])
    media = MediaFileUpload(filename, mimetype='text/csv', resumable=True)
    if items:
        service.files().update(fileId=items[0]['id'], media_body=media).execute()
        print(f"{filename} Drive'da güncellendi.")
    else:
        service.files().create(body={'name': filename, 'parents': [folder_id]}, media_body=media, fields='id').execute()
        print(f"{filename} Drive'a yeni dosya olarak yüklendi.")

def main():
    print("Sistem başlatılıyor...")
    load_usd_rates()
    session = get_session()
    
    tickers = get_all_tickers(session)
    print(f"Toplam {len(tickers)} adet hisse senedi bulundu.")
    
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
        df_scraped = df_scraped.dropna(subset=["Yil"])
        df_scraped["Temettu_Verim_%"] = df_scraped["Temettu_Verim_%"].apply(parse_yield)
        df_verim_final = df_scraped.groupby(["Kod", "Yil"], as_index=False)["Temettu_Verim_%"].sum()

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
            tam_tarih = get_exact_date(tarih)
            
            usd_kuru = get_usd_rate(tam_tarih)
            tutar_usd = (nakit_temettu / usd_kuru) if (usd_kuru and usd_kuru > 0) else 0.0
            
            if yil: processed_dividends.append({"Kod": kod, "Yil": yil, "Tutar": nakit_temettu, "Tutar_USD": tutar_usd})
            
    df_div_final = pd.DataFrame(columns=["Kod", "Yil", "Tutar", "Tutar_USD"])
    if processed_dividends:
        df_div_final = pd.DataFrame(processed_dividends).groupby(["Kod", "Yil"], as_index=False)[["Tutar", "Tutar_USD"]].sum()

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
        df_ipo_final = pd.DataFrame(processed_ipo).groupby("Kod", as_index=False)["Arz_Yili"].min()

    df_base = pd.DataFrame({"Kod": tickers})
    df_base = pd.merge(df_base, df_ipo_final, on="Kod", how="left")
    
    if df_div_final.empty and df_verim_final.empty:
        df_events = pd.DataFrame(columns=["Kod", "Yil", "Tutar", "Tutar_USD", "Temettu_Verim_%"])
    else:
        df_events = pd.merge(df_div_final, df_verim_final, on=["Kod", "Yil"], how="outer")
        
    df_master = pd.merge(df_base, df_events, on="Kod", how="left")
    
    df_master["Tutar"] = df_master["Tutar"].fillna(0.0)
    df_master["Tutar_USD"] = df_master["Tutar_USD"].fillna(0.0)
    df_master["Temettu_Verim_%"] = df_master["Temettu_Verim_%"].fillna(0.0)
    df_master["Yil"] = df_master["Yil"].fillna("")
    df_master["Arz_Yili"] = df_master["Arz_Yili"].fillna("")
    df_master = df_master[df_master["Kod"].str.strip() != ""]

    out_path = "bist_temettu_master.csv"
    df_master.to_csv(out_path, index=False, encoding="utf-8")
    upload_to_drive(out_path)

if __name__ == "__main__":
    main()
