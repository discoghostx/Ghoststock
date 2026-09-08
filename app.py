import streamlit as st
import yfinance as yf
import plotly.graph_objects as go
import pandas as pd
from google import genai
import requests
import json
import re

# ================= ตั้งค่าหน้าจอ & CSS =================
st.set_page_config(page_title="Investment Tear Sheet", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; }
    .row-item { display: flex; justify-content: space-between; border-bottom: 1px solid #333; padding: 6px 0px; font-size: 15px; }
    .row-label { color: #a0aab5; }
    .row-value { font-weight: 600; font-family: 'Courier New', Courier, monospace; }
    .section-head { color: #4fc3f7; font-weight: bold; margin-top: 15px; margin-bottom: 5px; border-bottom: 2px solid #4fc3f7; padding-bottom: 5px; }
    .part-title { color: #ffffff; background-color: #1e1e1e; padding: 12px; border-radius: 5px; margin-top: 20px; margin-bottom: 15px; border-left: 5px solid #4fc3f7; font-size: 1.1rem; font-weight: bold; }
    .step-box { background-color: #262730; padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid #333; height: 100%; }
    .step-title { color: #ffeb3b; font-weight: bold; margin-bottom: 5px; }
    .news-card { background-color: #262730; padding: 14px; border-radius: 6px; margin-bottom: 12px; border-left: 4px solid #4fc3f7; }
</style>
""", unsafe_allow_html=True)

# ================= ตั้งค่า API Key ของแท้ =================
BACKEND_GEMINI_API_KEY = "AIzaSyBdIj0mzt7MxJhwMm8bMvTVvzWx9_28DVI"

def display_row(label, value):
    st.markdown(f"<div class='row-item'><span class='row-label'>{label}</span><span class='row-value'>{value}</span></div>", unsafe_allow_html=True)

def safe_format(val, fmt="{:.2f}", suffix=""):
    if val is None or val == "N/A": 
        return "N/A"
    try: 
        return fmt.format(val) + suffix
    except: 
        return "N/A"

@st.cache_data(ttl=3600, show_spinner=False)
def get_year_end_prices_yf(ticker: str, fiscal_year_end_dates: list):
    stock = yf.Ticker(ticker)
    hist = stock.history(period="12y")
    if hist.empty:
        return {}
    hist.index = hist.index.tz_localize(None)
    prices = {}
    for d in fiscal_year_end_dates:
        if not d:
            continue
        target = pd.Timestamp(d)
        window = hist[(hist.index >= target - pd.Timedelta(days=10)) & (hist.index <= target + pd.Timedelta(days=10))]
        if not window.empty:
            diffs = pd.Series(window.index - target, index=range(len(window.index)))
            closest_idx = diffs.abs().argmin()
            prices[d] = float(window['Close'].iloc[closest_idx])
    return prices

# ================= ฟังก์ชันดึงงบรายปีจาก Yahoo Finance =================
@st.cache_data(ttl=3600, show_spinner=False)
def get_standardized_yahoo_table(ticker):
    try:
        stock = yf.Ticker(ticker)
        income_stmt = stock.income_stmt
        if income_stmt is None or income_stmt.empty:
            income_stmt = stock.financials
            
        balance_sheet = stock.balance_sheet
        cashflow = stock.cashflow

        if income_stmt is None or income_stmt.empty:
            return pd.DataFrame(), f"⚠️ ไม่พบข้อมืองบการเงินจาก Yahoo Finance สำหรับ {ticker}"
        
        inc_T = income_stmt.T.sort_index()
        bal_T = balance_sheet.T.sort_index() if balance_sheet is not None and not balance_sheet.empty else pd.DataFrame()
        cf_T = cashflow.T.sort_index() if cashflow is not None and not cashflow.empty else pd.DataFrame()

        fy_end_dates_yf = {str(date_idx.year): date_idx.strftime('%Y-%m-%d') for date_idx in inc_T.index}
        price_by_year = get_year_end_prices_yf(ticker, list(fy_end_dates_yf.values())) if fy_end_dates_yf else {}

        rows = []
        prev_ebit, prev_rev = None, None

        for date_idx, row in inc_T.iterrows():
            fy = str(date_idx.year)
            end_date = fy_end_dates_yf.get(fy)

            def get_val(df, keys):
                if df is None or df.empty or date_idx not in df.index:
                    return None
                r = df.loc[date_idx]
                for k in keys:
                    if k in r and pd.notna(r[k]):
                        return r[k]
                return None

            rev_v = get_val(inc_T, ["Total Revenue", "Operating Revenue", "Revenue"])
            ebit_v = get_val(inc_T, ["Operating Income", "EBIT", "Operating Income Loss"])
            ni_v = get_val(inc_T, ["Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"])
            eps_v = get_val(inc_T, ["Diluted EPS", "Basic EPS"])
            gp_v = get_val(inc_T, ["Gross Profit"])
            
            ocf_v = get_val(cf_T, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities", "Net Cash Provided By Used In Operating Activities"])
            
            fcf_v = get_val(cf_T, ["Free Cash Flow"])
            if fcf_v is None:
                capex_v = get_val(cf_T, ["Capital Expenditure", "Capital Expenditures", "Purchase Of Property Plant And Equipment"])
                if ocf_v is not None and capex_v is not None:
                    fcf_v = ocf_v + capex_v if capex_v < 0 else ocf_v - capex_v
                else:
                    fcf_v = ocf_v

            lt_debt = get_val(bal_T, ["Long Term Debt", "LongTermDebtNoncurrent"])
            st_debt = get_val(bal_T, ["Current Debt", "Short Term Debt", "Current Debt And Capital Lease Obligation", "ShortTermBorrowings"])
            total_debt_v = None
            if lt_debt is not None or st_debt is not None:
                total_debt_v = (lt_debt or 0) + (st_debt or 0)
            else:
                tot_alt = get_val(bal_T, ["Total Debt"])
                if tot_alt is not None:
                    total_debt_v = tot_alt

            price = price_by_year.get(end_date) if end_date else None
            pe = round(price / eps_v, 2) if (price and eps_v and eps_v != 0) else None

            gross_margin = round((gp_v / rev_v) * 100, 1) if (gp_v and rev_v) else None
            op_margin = round((ebit_v / rev_v) * 100, 1) if (ebit_v and rev_v) else None

            op_leverage = None
            if prev_ebit and prev_rev and ebit_v and rev_v and prev_ebit != 0 and prev_rev != 0:
                ebit_growth = (ebit_v - prev_ebit) / abs(prev_ebit)
                rev_growth = (rev_v - prev_rev) / abs(prev_rev)
                if rev_growth != 0:
                    op_leverage = round(ebit_growth / rev_growth, 2)

            rows.append({
                "Year": fy,
                "Price ($)": round(price, 2) if price else "N/A",
                "P/E": pe if pe else "N/A",
                "Revenue ($B)": round(rev_v / 1e9, 2) if rev_v else None,
                "EBIT ($B)": round(ebit_v / 1e9, 2) if ebit_v else None,
                "Net Income ($B)": round(ni_v / 1e9, 2) if ni_v else None,
                "EPS ($)": round(eps_v, 2) if eps_v else None,
                "OCF ($B)": round(ocf_v / 1e9, 2) if ocf_v else "N/A",
                "FCF ($B)": round(fcf_v / 1e9, 2) if fcf_v else "N/A",
                "Total Debt ($B)": round(total_debt_v / 1e9, 2) if total_debt_v else "N/A",
                "Gross Margin (%)": gross_margin if gross_margin else "N/A",
                "Op. Margin (%)": op_margin if op_margin else "N/A",
                "Op. Leverage": op_leverage if op_leverage else "N/A",
            })
            if ebit_v and rev_v:
                prev_ebit, prev_rev = ebit_v, rev_v

        df_result = pd.DataFrame(rows)
        return df_result, f"✅ [Yahoo Finance] โหลดงบมาตรฐานย้อนหลัง {len(df_result)} ปีสำเร็จ"
    except Exception as e:
        return pd.DataFrame(), f"⚠️ เกิดข้อผิดพลาด Yahoo Finance Annual: {e}"

# ================= ฟังก์ชันค้นหา CIK ใน SEC EDGAR =================
@st.cache_data(ttl=86400, show_spinner=False)
def get_sec_cik(ticker: str):
    try:
        headers = {"User-Agent": "InstitutionalEquityResearch analyst@institutionalresearchlab.org"}
        url = "https://www.sec.gov/files/company_tickers.json"
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code == 200:
            data = res.json()
            ticker_clean = ticker.upper().replace(".", "-")
            for item in data.values():
                if item.get("ticker", "").upper().replace(".", "-") == ticker_clean:
                    return str(item.get("cik_str")).zfill(10)
    except Exception:
        pass
    return None

# ================= PART 3: Hybrid SEC EDGAR + Auto Yahoo Fallback =================
@st.cache_data(ttl=3600, show_spinner=False)
def get_standardized_sec_edgar_table(ticker: str):
    cik = get_sec_cik(ticker)
    
    if cik:
        try:
            headers = {"User-Agent": "InstitutionalEquityResearch analyst@institutionalresearchlab.org"}
            url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
            res = requests.get(url, headers=headers, timeout=10)
            
            if res.status_code == 200:
                sec_json = res.json()
                us_gaap = sec_json.get("facts", {}).get("us-gaap", {})
                
                if us_gaap:
                    def extract_sec_fact(tag_list, unit="USD"):
                        for tag in tag_list:
                            if tag in us_gaap and unit in us_gaap[tag].get("units", {}):
                                entries = us_gaap[tag]["units"][unit]
                                annuals = [e for e in entries if e.get("form") in ["10-K", "10-K/A"] and e.get("fp") == "FY"]
                                if annuals:
                                    annuals_sorted = sorted(annuals, key=lambda x: x.get("filed", ""))
                                    res_dict, dates_dict = {}, {}
                                    for e in annuals_sorted:
                                        fy = str(e.get("fy"))
                                        if fy and "val" in e:
                                            res_dict[fy] = e["val"]
                                            dates_dict[fy] = e.get("end")
                                    if res_dict:
                                        return res_dict, dates_dict
                        return {}, {}

                    rev_d, dates_map = extract_sec_fact(["Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerExcludingAssessedTax"])
                    ebit_d, _ = extract_sec_fact(["OperatingIncomeLoss"])
                    ni_d, _ = extract_sec_fact(["NetIncomeLoss"])
                    eps_d, _ = extract_sec_fact(["EarningsPerShareDiluted", "EarningsPerShareBasic"], unit="USD/shares")
                    gp_d, _ = extract_sec_fact(["GrossProfit"])
                    ocf_d, _ = extract_sec_fact(["NetCashProvidedByUsedInOperatingActivities"])
                    capex_d, _ = extract_sec_fact(["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"])
                    lt_debt_d, _ = extract_sec_fact(["LongTermDebtNoncurrent"])
                    st_debt_d, _ = extract_sec_fact(["DebtCurrent", "ShortTermBorrowings"])

                    all_years = sorted(list(set(list(rev_d.keys()) + list(ni_d.keys()))))
                    if len(all_years) >= 2:
                        price_by_year = get_year_end_prices_yf(ticker, list(dates_map.values()))
                        rows = []
                        prev_ebit, prev_rev = None, None

                        for fy in all_years:
                            end_date = dates_map.get(fy)
                            price = price_by_year.get(end_date)
                            
                            rev_v = rev_d.get(fy)
                            ebit_v = ebit_d.get(fy)
                            ni_v = ni_d.get(fy)
                            eps_v = eps_d.get(fy)
                            gp_v = gp_d.get(fy)
                            ocf_v = ocf_d.get(fy)
                            capex_v = capex_d.get(fy)
                            
                            fcf_v = None
                            if ocf_v is not None and capex_v is not None:
                                fcf_v = ocf_v - abs(capex_v)
                            else:
                                fcf_v = ocf_v
                                
                            lt_debt = lt_debt_d.get(fy, 0) or 0
                            st_debt = st_debt_d.get(fy, 0) or 0
                            total_debt_v = lt_debt + st_debt if (lt_debt_d.get(fy) is not None or st_debt_d.get(fy) is not None) else None

                            pe = round(price / eps_v, 2) if (price and eps_v and eps_v != 0) else None
                            gross_margin = round((gp_v / rev_v) * 100, 1) if (gp_v and rev_v) else None
                            op_margin = round((ebit_v / rev_v) * 100, 1) if (ebit_v and rev_v) else None

                            op_leverage = None
                            if prev_ebit and prev_rev and ebit_v and rev_v and prev_rev != 0:
                                ebit_growth = (ebit_v - prev_ebit) / abs(prev_ebit)
                                rev_growth = (rev_v - prev_rev) / abs(prev_rev)
                                if rev_growth != 0:
                                    op_leverage = round(ebit_growth / rev_growth, 2)

                            rows.append({
                                "Year": fy,
                                "Price ($)": round(price, 2) if price else "N/A",
                                "P/E": pe if pe else "N/A",
                                "Revenue ($B)": round(rev_v / 1e9, 2) if rev_v else None,
                                "EBIT ($B)": round(ebit_v / 1e9, 2) if ebit_v else None,
                                "Net Income ($B)": round(ni_v / 1e9, 2) if ni_v else None,
                                "EPS ($)": round(eps_v, 2) if eps_v else None,
                                "OCF ($B)": round(ocf_v / 1e9, 2) if ocf_v else "N/A",
                                "FCF ($B)": round(fcf_v / 1e9, 2) if fcf_v else "N/A",
                                "Total Debt ($B)": round(total_debt_v / 1e9, 2) if total_debt_v else "N/A",
                                "Gross Margin (%)": gross_margin if gross_margin else "N/A",
                                "Op. Margin (%)": op_margin if op_margin else "N/A",
                                "Op. Leverage": op_leverage if op_leverage else "N/A",
                            })
                            if ebit_v and rev_v:
                                prev_ebit, prev_rev = ebit_v, rev_v

                        df_sec = pd.DataFrame(rows).tail(10).reset_index(drop=True)
                        return df_sec, f"✅ [SEC EDGAR] โหลดงบการเงินรายปีมาตรฐานย้อนหลัง {len(df_sec)} ปีสำเร็จ (CIK: {cik})"
        except Exception:
            pass

    df_yf, _ = get_standardized_yahoo_table(ticker)
    if not df_yf.empty:
        status_msg = f"ℹ️ [Auto-Fallback] หุ้น {ticker} เป็นหุ้นต่างประเทศ/ADR จึงสลับมาโหลดงบมาตรฐานพร้อม FCF จาก Yahoo Finance สำเร็จ ({len(df_yf)} ปีย้อนหลัง)"
        return df_yf, status_msg
    else:
        return pd.DataFrame(), f"⚠️ ไม่พบข้อมืองบการเงินทั้งใน SEC EDGAR และ Yahoo Finance สำหรับหุ้น {ticker}"

# ================= ฟังก์ชันดึงงบรายไตรมาสจาก Yahoo Finance =================
@st.cache_data(ttl=3600, show_spinner=False)
def get_standardized_yahoo_quarterly_table(ticker):
    try:
        stock = yf.Ticker(ticker)
        q_stmt = stock.quarterly_income_stmt
        if q_stmt is None or q_stmt.empty:
            q_stmt = stock.quarterly_financials
            
        q_bs = stock.quarterly_balance_sheet
        q_cf = stock.quarterly_cashflow

        if q_stmt is None or q_stmt.empty:
            return pd.DataFrame(), "⚠️ ไม่พบข้อมืองบการเงินรายไตรมาสจาก Yahoo Finance"

        inc_T = q_stmt.T.sort_index()
        bal_T = q_bs.T.sort_index() if q_bs is not None and not q_bs.empty else pd.DataFrame()
        cf_T = q_cf.T.sort_index() if q_cf is not None and not q_cf.empty else pd.DataFrame()

        eps_keys = ["Diluted EPS", "Basic EPS"]
        eps_series = None
        for k in eps_keys:
            if k in inc_T.columns:
                eps_series = inc_T[k]
                break
        
        ttm_eps_series = eps_series.rolling(window=4, min_periods=1).sum() if eps_series is not None else None

        quarter_end_dates_map = {date_idx: date_idx.strftime('%Y-%m-%d') for date_idx in inc_T.index}
        price_by_quarter = get_year_end_prices_yf(ticker, list(quarter_end_dates_map.values()))

        rows = []
        prev_ebit, prev_rev = None, None
        inc_T_recent = inc_T.tail(4)

        for date_idx, row in inc_T_recent.iterrows():
            year = date_idx.year
            month = date_idx.month
            if month in [1, 2, 3]: q_num = 1
            elif month in [4, 5, 6]: q_num = 2
            elif month in [7, 8, 9]: q_num = 3
            else: q_num = 4
            
            quarter_label = f"Q{q_num} {year}"
            end_date_str = quarter_end_dates_map.get(date_idx)
            price = price_by_quarter.get(end_date_str)

            def get_val(df, keys):
                if df is None or df.empty or date_idx not in df.index:
                    return None
                r = df.loc[date_idx]
                for k in keys:
                    if k in r and pd.notna(r[k]):
                        return r[k]
                return None

            rev_v = get_val(inc_T, ["Total Revenue", "Operating Revenue", "Revenue"])
            ebit_v = get_val(inc_T, ["Operating Income", "EBIT", "Operating Income Loss"])
            ni_v = get_val(inc_T, ["Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"])
            eps_v = get_val(inc_T, ["Diluted EPS", "Basic EPS"])
            gp_v = get_val(inc_T, ["Gross Profit"])
            
            ocf_v = get_val(cf_T, ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities", "Net Cash Provided By Used In Operating Activities"])
            
            fcf_v = get_val(cf_T, ["Free Cash Flow"])
            if fcf_v is None:
                capex_v = get_val(cf_T, ["Capital Expenditure", "Capital Expenditures", "Purchase Of Property Plant And Equipment"])
                if ocf_v is not None and capex_v is not None:
                    fcf_v = ocf_v + capex_v if capex_v < 0 else ocf_v - capex_v
                else:
                    fcf_v = ocf_v

            lt_debt = get_val(bal_T, ["Long Term Debt", "LongTermDebtNoncurrent"])
            st_debt = get_val(bal_T, ["Current Debt", "Short Term Debt", "Current Debt And Capital Lease Obligation", "ShortTermBorrowings"])
            total_debt_v = None
            if lt_debt is not None or st_debt is not None:
                total_debt_v = (lt_debt or 0) + (st_debt or 0)
            else:
                tot_alt = get_val(bal_T, ["Total Debt"])
                if tot_alt is not None:
                    total_debt_v = tot_alt

            ttm_eps = ttm_eps_series.loc[date_idx] if ttm_eps_series is not None and date_idx in ttm_eps_series.index else None
            pe = round(price / ttm_eps, 2) if (price and ttm_eps and ttm_eps != 0) else "N/A"

            gross_margin = round((gp_v / rev_v) * 100, 1) if (gp_v and rev_v) else None
            op_margin = round((ebit_v / rev_v) * 100, 1) if (ebit_v and rev_v) else None

            op_leverage = None
            if prev_ebit and prev_rev and ebit_v and rev_v and prev_rev != 0:
                ebit_growth = (ebit_v - prev_ebit) / abs(prev_ebit)
                rev_growth = (rev_v - prev_rev) / abs(prev_rev)
                if rev_growth != 0:
                    op_leverage = round(ebit_growth / rev_growth, 2)

            rows.append({
                "Quarter": quarter_label,
                "Price ($)": round(price, 2) if price else "N/A",
                "P/E": pe,
                "Revenue ($B)": round(rev_v / 1e9, 2) if rev_v else None,
                "EBIT ($B)": round(ebit_v / 1e9, 2) if ebit_v else None,
                "Net Income ($B)": round(ni_v / 1e9, 2) if ni_v else None,
                "EPS ($)": round(eps_v, 2) if eps_v else None,
                "OCF ($B)": round(ocf_v / 1e9, 2) if ocf_v else "N/A",
                "FCF ($B)": round(fcf_v / 1e9, 2) if fcf_v else "N/A",
                "Total Debt ($B)": round(total_debt_v / 1e9, 2) if total_debt_v else "N/A",
                "Gross Margin (%)": gross_margin if gross_margin else "N/A",
                "Op. Margin (%)": op_margin if op_margin else "N/A",
                "Op. Leverage": op_leverage if op_leverage else "N/A",
            })
            if ebit_v and rev_v:
                prev_ebit, prev_rev = ebit_v, rev_v

        df_result = pd.DataFrame(rows)
        return df_result, f"✅ [Yahoo Finance] โหลดงบการเงินรายไตรมาส 4 ไตรมาสล่าสุดพร้อมคำนวณ P/E สำเร็จ"
    except Exception as e:
        return pd.DataFrame(), f"⚠️ เกิดข้อผิดพลาด Yahoo Finance Quarterly: {e}"

# ================= ฟังก์ชันแปลและสรุปข่าวผ่าน Gemini (ใช้ SDK google-genai) =================
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_and_translate_news(ticker, api_key):
    try:
        stock = yf.Ticker(ticker)
        news_list = stock.news
        if not news_list:
            return []
        
        valid_news = []
        for item in news_list[:4]:
            content = item.get('content', {})
            if isinstance(content, dict):
                title = content.get('title') or item.get('title') or ''
                summary = content.get('summary') or ''
                publisher = content.get('provider', {}).get('displayName') or item.get('publisher') or 'Yahoo Finance'
                link_obj = content.get('clickThroughUrl') or content.get('canonicalUrl') or item.get('link')
                link = link_obj.get('url') if isinstance(link_obj, dict) else (link_obj or '#')
            else:
                title = item.get('title', '')
                summary = ''
                publisher = item.get('publisher', 'Yahoo Finance')
                link = item.get('link', '#')
            
            if title and title != 'No Title':
                valid_news.append({"title": title, "summary": summary, "publisher": publisher, "link": link})
        
        if not valid_news:
            return []

        client = genai.Client(api_key=api_key)
        
        news_payload = ""
        for i, n in enumerate(valid_news):
            news_payload += f"[{i+1}] Title: {n['title']}\nSummary: {n['summary']}\n\n"
        
        prompt = f"""
        คุณคือนักวิเคราะห์การลงทุน จงแปลและสรุปใจความสำคัญของข่าวหุ้นเหล่านี้เป็นภาษาไทยสำหรับนักลงทุน โดยให้สรุปข่าวละ 1 ย่อหน้าสั้นๆ (เน้นประเด็นสำคัญและผลกระทบต่อธุรกิจ)
        ตอบกลับในรูปแบบ JSON Array ของ Object โดยแต่ละ Object ต้องมีฟิลด์ดังนี้:
        - id: เลขลำดับข่าว (1, 2, 3, 4)
        - thai_title: หัวข้อข่าวแปลเป็นไทยที่กระชับและน่าสนใจ
        - thai_summary: เนื้อหาสรุปย่อของข่าวเป็นภาษาไทย (1-2 ประโยค)
        
        ข้อมูลข่าว:
        {news_payload}
        
        ตอบกลับเฉพาะ JSON Array เท่านั้น ห้ามมีข้อความเกริ่นหรือข้อความปิดท้ายใดๆ ทั้งสิ้น
        """
        response = client.models.generate_content(
            model='gemini-3.6-flash',
            contents=prompt
        )
        text_res = response.text.strip()
        
        json_match = re.search(r"\[.*\]", text_res, re.DOTALL)
        if json_match:
            text_res = json_match.group(0)
            
        translated_data = json.loads(text_res)
        
        for item in translated_data:
            idx = item.get('id', 1) - 1
            if 0 <= idx < len(valid_news):
                item['publisher'] = valid_news[idx]['publisher']
                item['link'] = valid_news[idx]['link']
        return translated_data
    except Exception:
        return [{"thai_title": n['title'], "thai_summary": n['summary'] or "ไม่มีรายละเอียดเนื้อหาเพิ่มเติม", "publisher": n['publisher'], "link": n['link']} for n in valid_news]

# ================= แถบเมนูด้านข้าง (Sidebar) =================
st.sidebar.header("🔍 ตั้งค่าการวิเคราะห์")
ticker_symbol = st.sidebar.text_input("ใส่ชื่อย่อหุ้น (เช่น TSM, NVDA, AVGO, AAPL):", "TSM").upper()

if "last_ticker" not in st.session_state or st.session_state.last_ticker != ticker_symbol:
    st.session_state.ai_results = None
    st.session_state.last_ticker = ticker_symbol

# ================= แสดงผลหน้าจอ Dashboard =================
with st.spinner(f'กำลังประมวลผลข้อมูล {ticker_symbol}...'):
    try:
        stock = yf.Ticker(ticker_symbol)
        info = stock.info

        company_name = info.get('shortName', ticker_symbol)
        current_price = info.get('currentPrice', info.get('previousClose', 0))
        
        pe = info.get('trailingPE')
        if pe is None or pe == 'N/A':
            eps = info.get('trailingEps')
            if current_price and eps and eps != 0:
                pe = round(current_price / eps, 2)
            else:
                pe = 'N/A'

        fwd_pe = info.get('forwardPE', 'N/A')
        peg = info.get('pegRatio', 'N/A')
        gross_margin = info.get('grossMargins', 0) * 100 if info.get('grossMargins') else 'N/A'
        op_margin = info.get('operatingMargins', 0) * 100 if info.get('operatingMargins') else 'N/A'
        roe = info.get('returnOnEquity', 0) * 100 if info.get('returnOnEquity') else 'N/A'
        total_cash = info.get('totalCash', 0) / 1e9 if info.get('totalCash') else 'N/A'
        total_debt = info.get('totalDebt', 0) / 1e9 if info.get('totalDebt') else 'N/A'
        current_ratio = info.get('currentRatio', 'N/A')

        logo_url = f"https://financialmodelingprep.com/image-stock/{ticker_symbol}.png"
        st.markdown(f"""
        <div style='display: flex; justify-content: flex-end; align-items: center; gap: 15px; margin-bottom: 0;'>
            <img src='{logo_url}' width='50' style='border-radius: 8px; background-color: #ffffff; padding: 3px;' onerror="this.style.display='none'">
            <h1 style='font-size: 4rem; margin: 0;'>{ticker_symbol}</h1>
        </div>
        """, unsafe_allow_html=True)
        st.markdown(f"<p style='text-align: right; color: #888; font-size: 1.2rem; margin-top: 5px;'>{company_name}</p>", unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        # ================= PART 1: Price Cycle =================
        st.markdown("<div class='part-title'>PART 1: 10-Year Price Cycle</div>", unsafe_allow_html=True)
        hist = stock.history(period="10y")
        if not hist.empty:
            fig = go.Figure(data=[go.Candlestick(x=hist.index, open=hist['Open'], high=hist['High'], low=hist['Low'], close=hist['Close'], name="Price")])
            fig.update_layout(xaxis_rangeslider_visible=False, height=450, template="plotly_dark", margin=dict(l=0, r=0, t=10, b=0), paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)')
            st.plotly_chart(fig, use_container_width=True)

        # ================= PART 2: Current Market Data =================
        st.markdown("<div class='part-title'>PART 2: Current Market Data & Key Metrics</div>", unsafe_allow_html=True)

        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("<div class='section-head'>VALUATION MULTIPLES</div>", unsafe_allow_html=True)
            display_row("Current Price", safe_format(current_price, "${:.2f}"))
            display_row("P/E Ratio", safe_format(pe, "{:.2f}x"))
            display_row("Forward P/E", safe_format(fwd_pe, "{:.2f}x"))
            display_row("PEG Ratio", safe_format(peg, "{:.2f}"))
        with c2:
            st.markdown("<div class='section-head'>PROFITABILITY MARGINS</div>", unsafe_allow_html=True)
            display_row("Gross Margin", safe_format(gross_margin, "{:.1f}%"))
            display_row("Operating Margin", safe_format(op_margin, "{:.1f}%"))
            display_row("ROE", safe_format(roe, "{:.1f}%"))
        with c3:
            st.markdown("<div class='section-head'>BALANCE SHEET & HEALTH</div>", unsafe_allow_html=True)
            display_row("Total Debt", safe_format(total_debt, "${:.2f} B"))
            display_row("Cash & Equivalents", safe_format(total_cash, "${:.2f} B"))
            display_row("Current Ratio", safe_format(current_ratio, "{:.2f}"))

        # ================= PART 3: ข้อมูลรายปีจาก SEC EDGAR =================
        st.markdown("<div class='part-title'>PART 3: ข้อมูลรายปีจาก SEC EDGAR (งบการเงินมาตรฐานย้อนหลังสูงสุด 10 ปี พร้อม FCF)</div>", unsafe_allow_html=True)
        df_sec, sec_status = get_standardized_sec_edgar_table(ticker_symbol)
        if df_sec.empty:
            st.error(sec_status)
        else:
            if "Auto-Fallback" in sec_status:
                st.info(sec_status)
            else:
                st.success(sec_status)
            st.dataframe(df_sec, use_container_width=True, hide_index=True)

        # ================= PART 4: ข้อมูลรายไตรมาสจาก Yahoo Finance =================
        st.markdown("<div class='part-title'>PART 4: ข้อมูลรายไตรมาสจาก Yahoo Finance (4 ไตรมาสล่าสุด พร้อม FCF & P/E)</div>", unsafe_allow_html=True)
        df_yf_q, q_status = get_standardized_yahoo_quarterly_table(ticker_symbol)
        if df_yf_q.empty:
            st.error(q_status)
        else:
            st.success(q_status)
            st.dataframe(df_yf_q, use_container_width=True, hide_index=True)

        # ================= PART 5 & 6: 6-Step Fundamental FV & AI Valuation Engine =================
        st.markdown("<div class='part-title'>PART 5: 6-Step Fundamental Fair Value & Valuation Engine</div>", unsafe_allow_html=True)

        col_s1, col_s2, col_s3 = st.columns(3)
        with col_s1:
            st.markdown("<div class='step-box'><div class='step-title'>Step 1: Data Sources</div>ดึงข้อมืองบ (รวม OCF & FCF) จาก Part 3 และ 4 ครบถ้วน</div>", unsafe_allow_html=True)
            st.markdown("<div class='step-box'><div class='step-title'>Step 4: Scenario Design</div>กำหนด Bear / Base / Bull Growth จากแนวโน้มจริง</div>", unsafe_allow_html=True)
        with col_s2:
            st.markdown("<div class='step-box'><div class='step-title'>Step 2: Financial Flow</div>ติดตามกระแส: Revenue ➔ EBIT ➔ Net Income ➔ EPS ➔ FCF</div>", unsafe_allow_html=True)
            st.markdown("<div class='step-box'><div class='step-title'>Step 5: Terminal Value</div>ประเมิน Terminal / Normalized P/E Multiple</div>", unsafe_allow_html=True)
        with col_s3:
            st.markdown("<div class='step-box'><div class='step-title'>Step 3: Forecast Horizon</div>สร้างโมเดลคาดการณ์งบการเงินล่วงหน้า 3–5 ปี</div>", unsafe_allow_html=True)
            st.markdown("<div class='step-box'><div class='step-title'>Step 6: Fundamental FV</div>คำนวณราคา Target Price ➔ คิดลด PV ด้วย WACC 9% (Discount Factor = 0.7722)</div>", unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        
        if "ai_results" not in st.session_state:
            st.session_state.ai_results = None

        # ปุ่มคำนวณด้วย Gemini ฟรี
        if st.sidebar.button("🚀 วิเคราะห์ Fundamental FV ด้วย Gemini (Part 6)", type="primary"):
            with st.spinner("🤖 Gemini (Free Tier) กำลังประเมินมูลค่าตามหลักการ Target Price & PV Discount..."):
                try:
                    client = genai.Client(api_key=BACKEND_GEMINI_API_KEY)
                    
                    prompt = f"""
                    คุณคือนักวิเคราะห์การลงทุนระดับเซียน (Senior Equity Research & Quantitative Analyst) จงรันโมเดลประเมินมูลค่าหุ้น {ticker_symbol} 
                    โดยอิงจากงบการเงินจริงใน Part 3 (รายปี) และ Part 4 (รายไตรมาส) ตามกรอบกระบวนการ **6-Step Fundamental Valuation** 
                    โดยเฉพาะ **Step 6** ให้ใช้หลักการคำนวณเชิงปริมาณแบบเข้มงวด:
                    - คำนวณมูลค่าในอนาคต (Target Price 3 ปีข้างหน้า) จาก EPS เติบโตคูณด้วย Terminal Multiple ที่เหมาะสม
                    - คิดลดกลับเป็นมูลค่าปัจจุบัน (Present Value - PV) ด้วยอัตราคิดลด WACC ที่ 9.0% (Discount Factor สำหรับ 3 ปี = 0.7722)
                    
                    - ข้อมูลตลาดปัจจุบัน: ราคาปัจจุบัน = ${current_price}, P/E ปัจจุบัน = {pe}
                    - ข้อมูลรายปี (Part 3):
                    {df_sec.to_string()}
                    - ข้อมูลรายไตรมาส (Part 4):
                    {df_yf_q.to_string()}
                    
                    ตอบกลับโดยขึ้นต้นรูปแบบ 3 บรรทัดแรกให้ตรงเป๊ะเพื่อนำไปแสดงผล (ระบุเฉพาะตัวเลขทศนิยม):
                    BEAR_FV: [ตัวเลขราคา PV เช่น 42.50]
                    BASE_FV: [ตัวเลขราคา PV เช่น 98.20]
                    BULL_FV: [ตัวเลขราคา PV เช่น 155.40]
                    
                    ตามด้วยรายงานบทวิเคราะห์เชิงปริมาณภาษาไทย แสดงขั้นตอนการคำนวณ Target Price และ PV ตามรูปแบบมาตรฐานสถาบันการเงิน:
                    ### 6-STEP FUNDAMENTAL VALUATION RATIONALE & AI THESIS
                    [แสดงรายละเอียดสมมติฐาน EPS, Target Multiple, Target Price และการคิดลด PV ด้วย Discount Factor 0.7722 สำหรับกรณี Bear, Base และ Bull อย่างละเอียด พร้อมเทียบกับราคาปัจจุบัน]
                    """
                    
                    response = client.models.generate_content(
                        model='gemini-3.6-flash',
                        contents=prompt
                    )
                    
                    st.session_state.ai_results = response.text
                    st.success("✅ วิเคราะห์ Fundamental FV สำเร็จโดย Gemini!")
                except Exception as e:
                    st.error(f"เกิดข้อผิดพลาดในการเชื่อมต่อ Gemini API: {e}")

        # ดึงค่า Fair Value ที่คำนวณได้มาแสดงผล
        bear_display, base_display, bull_display = "รอรันโมเดล...", "รอรันโมเดล...", "รอรันโมเดล..."
        bear_sub, base_sub, bull_sub = "Downside", "Upside", "Upside"
        base_val_num = current_price

        if st.session_state.ai_results:
            text = st.session_state.ai_results
            bear_match = re.search(r"BEAR_FV:\s*\$?([\d\.]+)", text)
            base_match = re.search(r"BASE_FV:\s*\$?([\d\.]+)", text)
            bull_match = re.search(r"BULL_FV:\s*\$?([\d\.]+)", text)
            
            if bear_match:
                b_val = float(bear_match.group(1))
                bear_display = f"${b_val:.2f}"
                diff = ((b_val - current_price) / current_price) * 100
                bear_sub = f"{diff:+.1f}% vs Price"
            if base_match:
                bs_val = float(base_match.group(1))
                base_val_num = bs_val
                base_display = f"${bs_val:.2f}"
                diff = ((bs_val - current_price) / current_price) * 100
                base_sub = f"{diff:+.1f}% vs Price"
            if bull_match:
                bl_val = float(bull_match.group(1))
                bull_display = f"${bl_val:.2f}"
                diff = ((bl_val - current_price) / current_price) * 100
                bull_sub = f"{diff:+.1f}% vs Price"

        st.markdown("<div class='section-head'>STEP 6 OUTCOMES: FUNDAMENTAL FAIR VALUE</div>", unsafe_allow_html=True)
        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("Current Price", safe_format(current_price, "${:.2f}"))
        sc2.metric("🐻 Bear FV", bear_display, bear_sub)
        sc3.metric("🎯 Base FV", base_display, base_sub)
        sc4.metric("🚀 Bull FV", bull_display, bull_sub)

        if st.session_state.ai_results:
            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("<div class='section-head'>GEMINI 6-STEP VALUATION RATIONALE & AI THESIS</div>", unsafe_allow_html=True)
            st.markdown(st.session_state.ai_results)

            # ================= Valuation Sensitivity Matrix =================
            st.markdown("<div class='part-title'>VALUATION SENSITIVITY MATRIX (WACC vs Terminal Growth)</div>", unsafe_allow_html=True)
            st.caption("ตารางจำลองความอ่อนไหวของราคา Base Fair Value ตามการเปลี่ยนแปลงของอัตราคิดลด (WACC) และอัตราเติบโตระยะยาว")
            
            wacc_rates = [0.08, 0.09, 0.10, 0.11, 0.12]
            tg_rates = [0.02, 0.025, 0.03, 0.035]
            
            matrix_data = []
            for tg in tg_rates:
                row = []
                for w in wacc_rates:
                    if w > tg:
                        factor = (0.10 - 0.03) / (w - tg)
                        val = round(base_val_num * factor, 2)
                        row.append(f"${val}")
                    else:
                        row.append("N/A")
                matrix_data.append(row)
            
            df_sens = pd.DataFrame(matrix_data, index=[f"TG: {int(tg*100)}%" for tg in tg_rates], columns=[f"WACC: {int(w*100)}%" for w in wacc_rates])
            st.dataframe(df_sens, use_container_width=True)

        # ================= PART 7: Qualitative News & Earnings Insights =================
        st.markdown("<div class='part-title'>PART 7: Qualitative News & Earnings Insights (แปลและสรุปภาษาไทยโดย Gemini)</div>", unsafe_allow_html=True)
        st.caption("สรุปเนื้อหาและประเด็นสำคัญจากข่าวสารล่าสุด 4 ข่าว เรียงจากข่าวใหม่ไปเก่า")
        
        with st.spinner("🤖 กำลังดึงข่าวและแปลสรุปเนื้อหาเป็นภาษาไทย..."):
            translated_news = fetch_and_translate_news(ticker_symbol, BACKEND_GEMINI_API_KEY)
            
            if translated_news and isinstance(translated_news, list):
                for news in translated_news:
                    title = news.get('thai_title', 'ไม่มีหัวข้อ')
                    summary = news.get('thai_summary', 'ไม่มีเนื้อหาสรุป')
                    publisher = news.get('publisher', 'Yahoo Finance')
                    link = news.get('link', '#')
                    
                    st.markdown(f"""
                    <div class='news-card'>
                        <b style='color: #4fc3f7;'>[{publisher}]</b> <span style='font-size: 1.05rem; font-weight: bold;'>{title}</span><br>
                        <p style='color: #d1d5db; margin-top: 5px; margin-bottom: 5px; font-size: 0.95rem;'>{summary}</p>
                        <a href='{link}' target='_blank' style='color: #ffeb3b; font-size: 0.85rem; text-decoration: none;'>🔗 อ่านข่าวต้นฉบับเต็ม</a>
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.info("ไม่พบข้อมูลข่าวสารล่าสุดสำหรับหุ้นตัวนี้ในขณะนี้")

    except Exception as e:
        st.error(f"เกิดข้อผิดพลาดในการประมวลผลระบบ: {e}")