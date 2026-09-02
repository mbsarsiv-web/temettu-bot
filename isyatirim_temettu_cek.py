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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive"
}

REQUEST_DELAY = 0.5  
RETRY_COUNT = 5
RETRY_WAIT = 3.0

def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s

def fetch_with_retry(session, url, method="GET", json_payload=None, extra_headers=None):
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            req_headers = session.headers.copy()
            if extra_headers:
                req_headers.update(extra_headers)
                
            if method == "POST":
                resp = session.post(url, json=json_payload, headers=req_headers, timeout=30)
            else:
                resp = session.get(url, headers=req_headers, timeout=30)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
        time.sleep(RETRY_WAIT)
    return None

def fetch_api_data(session, tanim_kodu):
    payload = {"hisseKodu": "", "hisseTanimKodu": tanim_kodu, "yil": 0, "zaman": "HEPSI", "endeksKodu": "09", "sektorKodu": ""}
    api_headers = {
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json; charset=utf-8"
    }
    res_text = fetch_with_retry(session, API_URL, method="POST", json_payload=payload, extra_headers=api_headers)
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
            if re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", code.strip()):
                tickers.add(code.strip())
    if not tickers:
        api_data = fetch_api_data(session, "04")
        for satir in api_data:
            kod = satir.get("SHHE_HS_KOD") or satir.get("HISSE_KODU") or ""
            if not kod:
                for k, v in satir.items():
                    if "KOD" in k.upper(): kod = v; break
            if kod and isinstance(kod, str):
                kod = kod.strip().upper()
                if re.fullmatch(r"[A-Z][A-Z0-9]{1,5}", kod):
                    tickers.add(kod)
    return sorted(list(tickers))

def find_dividend_table(html):
    try:
        # KESİN DÜZELTME: Pandas'ın virgülleri silip (1,94 -> 194) yapmasını engellemek için 
        # thousands='_' ataması yapıldı. Artık 1,94'e dokunamayacak, saf metin olarak bırakacak!
        tables = pd.read_html(StringIO(html), thousands='_', decimal='.')
    except Exception:
        return None
    adaylar = []
    for tbl in tables:
        cols = [str(c).lower() for c in tbl.columns]
        joined = " ".join(cols)
        if "verim" in joined and "tarih" in joined:
            adaylar.append(tbl)
            
    if not adaylar:
        return None
        
    adaylar.sort(key=lambda t: len(t), reverse=True)
    return adaylar[0]

def clean_dividend_table(df, kod):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ['_'.join(map(str, col)).strip() for col in df.columns]
        
    rename_map = {}
    for c in df.columns:
        c_str = str(c).lower().strip()
        if "tarih" in c_str or "dağ" in c_str or "yıl" in c_str:
            if "Dagitim_Tarihi" not in rename_map.values():
                rename_map[c] = "Dagitim_Tarihi"
        elif "verim" in c_str:
            if "Temettu_Verim_%" not in rename_map.values():
                rename_map[c] = "Temettu_Verim_%"
                
    df = df.rename(columns=rename_map)
    
    if "Dagitim_Tarihi" not in df.columns:
        for c in df.columns:
            if "tarih" in str(c).lower():
                df = df.rename(columns={c: "Dagitim_Tarihi"})
                break
                
    if "Dagitim_Tarihi" not in df.columns or "Temettu_Verim_%" not in df.columns:
        return pd.DataFrame()
        
    df = df.loc[:, ~df.columns.duplicated()]
    keep_cols = ["Dagitim_Tarihi", "Temettu_Verim_%"]
    df = df[keep_cols].copy()
    
    df = df[df["Dagitim_Tarihi"].notna()]
    df = df.drop_duplicates()
    df.insert(0, "Kod", kod)
    return df

def parse_turkce_sayi(val):
    if pd.isna(val) or not val: return 0.0
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip()
    if not s: return 0.0
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s:
        s = s.replace(',', '.')
    try: return float(s)
    except ValueError: return 0.0

def get_mantiki_yil(val):
    if pd.isna(val) or not val: return None
    str_val = str(val).strip()
    m = re.search(r'Date\(([-0-9]+)\)', str_val)
    if m:
        try: return str(time.gmtime(int(m.group(1))/1000).tm_year)
        except Exception: pass
    year_match = re.search(r'\b(19[8-9]\d|20\d\d)\b', str_val)
    if year_match: return year_match.group(1)
    return None

def parse_yield(val):
    if pd.isna(val): return ""
    s = str(val).strip().replace('%', '').replace('₺', '').replace('TL', '')
    if not s or s == '-': return ""
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s: 
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return ""

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
        if "Dagitim_Tarihi" in df_scraped.columns:
            df_scraped["Yil"] = df_scraped["Dagitim_Tarihi"].apply(get_mantiki_yil)
            df_scraped = df_scraped.dropna(subset=["Yil"])
            if "Temettu_Verim_%" in df_scraped.columns:
                df_scraped["Temettu_Verim_%"] = df_scraped["Temettu_Verim_%"].apply(parse_yield)
                df_verim_final = df_scraped[pd.to_numeric(df_scraped['Temettu_Verim_%'], errors='coerce').notnull()].copy()
                df_verim_final["Temettu_Verim_%"] = df_verim_final["Temettu_Verim_%"].astype(float)
                df_verim_final = df_verim_final.groupby(["Kod", "Yil"], as_index=False)["Temettu_Verim_%"].sum()

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

    df_base = pd.DataFrame({"Kod": tickers})
    df_base = pd.merge(df_base, df_ipo_final, on="Kod", how="left")
    
    if df_div_final.empty and df_verim_final.empty:
        df_events = pd.DataFrame(columns=["Kod", "Yil", "Tutar", "Temettu_Verim_%"])
    else:
        df_events = pd.merge(df_div_final, df_verim_final, on=["Kod", "Yil"], how="outer")
        
    df_master = pd.merge(df_base, df_events, on="Kod", how="left")
    
    df_master["Tutar"] = df_master["Tutar"].fillna(0.0)
    df_master["Temettu_Verim_%"] = df_master["Temettu_Verim_%"].fillna("")
    df_master["Yil"] = df_master["Yil"].fillna("")
    df_master["Arz_Yili"] = df_master["Arz_Yili"].fillna("")
    df_master = df_master[df_master["Kod"].str.strip() != ""]

    out_path = "bist_temettu_master.csv"
    df_master.to_csv(out_path, index=False, encoding="utf-8", decimal=".", sep=";")
    upload_to_drive(out_path)
    print("Görev başarıyla tamamlandı!")

if __name__ == "__main__":
    main()
