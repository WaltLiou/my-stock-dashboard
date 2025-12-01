import streamlit as st
import gspread
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import time

# --- Configuration ---
if "sheet_id" in st.secrets:
    SHEET_ID = st.secrets["sheet_id"]
else:
    st.error("Missing 'sheet_id' in secrets.toml")
    st.stop()

st.set_page_config(page_title="Stock Option Safety Net", layout="wide")

# --- CSS Styling ---
hide_streamlit_style = """
            <style>
            header {visibility: hidden;}
            #MainMenu {visibility: hidden;}
            footer {visibility: hidden;}
            .stDeployButton {display:none;}
            [data-testid="stToolbar"] {visibility: hidden !important;}
            [data-testid="stDecoration"] {visibility: hidden;}
            [data-testid="stStatusWidget"] {visibility: hidden;}
            .block-container {padding-top: 1rem;}
            
            [data-testid="stMetricValue"] {
                font-size: 1.5rem;
            }
            </style>
            """
st.markdown(hide_streamlit_style, unsafe_allow_html=True)

# --- Google Sheets Connection ---
@st.cache_resource
def get_gspread_client():
    try:
        creds_dict = dict(st.secrets["gcp_service_account"])
        gc = gspread.service_account_from_dict(creds_dict)
        return gc
    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
        return None

def get_sheet():
    gc = get_gspread_client()
    if not gc: return None
    try:
        sh = gc.open_by_key(SHEET_ID)
        return sh.sheet1
    except Exception as e:
        st.error(f"Failed to open sheet: {e}")
        return None

def init_sheet(worksheet):
    try:
        if not worksheet.get_all_values():
            header = ['Symbol', 'Type', 'Strike', 'Expiry', 'Quantity', 'EntryDate']
            worksheet.append_row(header)
    except Exception as e:
        st.error(f"Error initializing sheet: {e}")

# --- Data Handling ---
def load_data(worksheet):
    try:
        data = worksheet.get_all_records()
        if not data:
            return pd.DataFrame(columns=['Symbol', 'Type', 'Strike', 'Expiry', 'Quantity', 'EntryDate', '_row_index'])
        
        df = pd.DataFrame(data)
        
        # 1. 記錄原始行號 (在過濾之前！)
        df['_row_index'] = df.index + 2 
        
        # 2. 轉換格式 (包含您之前的日期修復 mixed format)
        df['Strike'] = pd.to_numeric(df['Strike'], errors='coerce')
        df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
        df['Expiry'] = pd.to_datetime(df['Expiry'], format='mixed', errors='coerce')
        
        # 移除無效日期以避免錯誤
        df = df.dropna(subset=['Expiry'])
        
        # [修改點 1] 過濾掉過期很久的部位
        # 邏輯：只保留 (到期日 >= 昨天) 的部位
        yesterday = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
        df = df[df['Expiry'] >= yesterday]
        
        df = df.sort_values(by='Expiry')
        
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return pd.DataFrame()

def add_position(worksheet, symbol, type_, strike, expiry, quantity):
    try:
        entry_date = datetime.now().strftime("%Y-%m-%d")
        row = [symbol, type_, strike, expiry, quantity, entry_date]
        worksheet.append_row(row)
        st.toast(f"✅ Added: {symbol} {type_} {strike}")
    except Exception as e:
        st.error(f"Error adding position: {e}")

def delete_positions_batch(worksheet, row_indices):
    try:
        sorted_indices = sorted(row_indices, reverse=True)
        for idx in sorted_indices:
            worksheet.delete_rows(idx)
        st.toast(f"🗑️ Deleted {len(sorted_indices)} position(s).")
        time.sleep(0.5) 
        st.rerun()
    except Exception as e:
        st.error(f"Error deleting positions: {e}")

# --- Market Data & Calculations ---
@st.cache_data(ttl=60) 
def get_current_prices(symbols):
    if not symbols: return {}
    unique_symbols = list(set(symbols))
    prices = {}
    
    try:
        tickers_str = " ".join(unique_symbols)
        data = yf.download(tickers_str, period="1d", group_by='ticker', progress=False)
        
        if len(unique_symbols) == 1:
            sym = unique_symbols[0]
            if not data.empty:
                prices[sym] = data['Close'].iloc[-1]
            else:
                prices[sym] = 0.0
        else:
            for sym in unique_symbols:
                try:
                    if sym in data.columns.levels[0]:
                        val = data[sym]['Close'].iloc[-1]
                        prices[sym] = val
                    else:
                        prices[sym] = 0.0
                except:
                    prices[sym] = 0.0
    except:
        return {s: 0.0 for s in unique_symbols}
                
    return prices

def process_market_data(df):
    if df.empty: return df
    
    symbols = df['Symbol'].unique().tolist()
    price_map = get_current_prices(symbols)
    
    df['Current Price'] = df['Symbol'].map(price_map).fillna(0.0)
    df['Notional'] = df['Strike'] * df['Quantity'].abs() * 100
    
    def calculate_safety_gap(row):
        current = row['Current Price']
        strike = row['Strike']
        if current <= 0: return 0.0
        
        val = 0.0
        if row['Type'] == 'Put':
            val = (current - strike) / current
        else:
            val = (strike - current) / current
            
        return val * 100 

    df['Safety %'] = df.apply(calculate_safety_gap, axis=1)
    
    def get_bucket(val):
        if val < 0: return '<0%'
        elif val < 5: return '0-5%'
        elif val < 10: return '5-10%'
        elif val < 15: return '10-15%'
        elif val < 20: return '15-20%'
        else: return '>20%'
        
    df['Bucket'] = df['Safety %'].apply(get_bucket)
    df['ExpiryMonth'] = df['Expiry'].dt.strftime('%Y-%m')
    
    return df

# --- UI Components ---

def display_alerts(df):
    st.subheader("🚨 風險與到期監控")
    
    today = pd.Timestamp.now().normalize()
    next_week = today + pd.Timedelta(days=7)
    
    expiring_soon = df[df['Expiry'] <= next_week].copy()
    high_risk = df[df['Safety %'] < 5].copy()
    
    c1, c2 = st.columns(2)
    
    # [修改點 2] 左欄：即將到期 -> 加入 Current Price
    with c1:
        if not expiring_soon.empty:
            st.error(f"⏳ 7 天內到期 ({len(expiring_soon)})")
            st.dataframe(
                expiring_soon[['Expiry', 'Symbol', 'Type', 'Strike', 'Current Price', 'Safety %']].style.format({
                    'Safety %': '{:.1f}%',
                    'Strike': '{:.1f}',
                    'Current Price': '{:.1f}' # 格式化價格
                }),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Expiry": st.column_config.DateColumn("Exp", format="MM-DD", width="small"),
                    "Current Price": st.column_config.NumberColumn("Price", format="%.1f"), # 加入欄位設定
                    "Safety %": st.column_config.ProgressColumn("Safety", min_value=-20, max_value=50, format="%.1f%%")
                }
            )
        else:
            st.success("✅ 近 7 天無到期部位")

    # 右欄：高風險
    with c2:
        if not high_risk.empty:
            st.warning(f"⚠️ 高風險 Safety < 5% ({len(high_risk)})")
            st.dataframe(
                high_risk[['Expiry', 'Symbol', 'Type', 'Strike', 'Current Price', 'Safety %']].style.format({
                    'Safety %': '{:.1f}%',
                    'Strike': '{:.1f}',
                    'Current Price': '{:.1f}'
                }),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Expiry": st.column_config.DateColumn("Exp", format="MM-DD", width="small"),
                    "Current Price": st.column_config.NumberColumn("Price", format="%.1f"),
                    "Safety %": st.column_config.ProgressColumn("Safety", min_value=-20, max_value=50, format="%.1f%%")
                }
            )
        else:
            st.success("✅ 所有部位 Safety > 5%")

def display_safety_matrix(df):
    if df.empty: return
    st.subheader("🕸️ 整體分布 (Notional Value)")
    
    col_radio, _ = st.columns([1, 4])
    with col_radio:
        view_type = st.radio("顯示類型", ["Put", "Call"], horizontal=True, label_visibility="collapsed")
    
    filtered_df = df[df['Type'] == view_type].copy()
    if filtered_df.empty:
        st.info(f"目前沒有 {view_type} 部位")
        return

    pivot = filtered_df.pivot_table(
        index='ExpiryMonth', 
        columns='Bucket', 
        values='Notional', 
        aggfunc='sum',
        fill_value=0
    )
    col_order = ['<0%', '0-5%', '5-10%', '10-15%', '15-20%', '>20%']
    pivot = pivot.reindex(columns=col_order, fill_value=0)
    pivot['總計'] = pivot.sum(axis=1)

    st.dataframe(pivot.style.format("{:,.0f}"), use_container_width=True)

# --- Main App ---
st.title("📈 Stock Option Tracker")

worksheet = get_sheet()

if worksheet:
    init_sheet(worksheet)
    
    # 1. 輸入區塊
    with st.expander("📝 新增持倉 (Add New Position)", expanded=False):
        with st.form("add_position_form", clear_on_submit=True):
            c1, c2, c3 = st.columns([2, 1, 1])
            with c1: symbol = st.text_input("Symbol").upper().strip()
            with c2: type_ = st.selectbox("Type", ["Put", "Call"])
            with c3: side = st.selectbox("Action", ["Sell", "Buy"])
                
            c4, c5, c6 = st.columns([1, 1, 1])
            with c4: strike = st.number_input("Strike", min_value=0.0, step=0.5)
            with c5:
                default_date = datetime.now() + timedelta(days=30)
                expiry = st.date_input("Expiry", value=default_date)
            with c6: qty_input = st.number_input("Qty (Abs)", min_value=1, step=1, value=1)
            
            if st.form_submit_button("Add Position", type="primary"):
                if symbol:
                    quantity = -qty_input if "Sell" in side else qty_input
                    add_position(worksheet, symbol, type_, strike, str(expiry), quantity)
                    st.rerun()
                else:
                    st.warning("Please enter Symbol")

    # 2. 資料處理
    df = load_data(worksheet)
    
    if not df.empty:
        with st.spinner('Updating market data...'):
            df = process_market_data(df)
        
        # 3. 儀表板
        display_alerts(df)
        
        st.divider()

        # 4. 矩陣
        display_safety_matrix(df)
        
        st.divider()

        # 5. 詳細列表
        st.subheader("📋 所有持倉管理 (Full List)")
        
        df_editor = df.copy()
        df_editor['Delete'] = False 
        
        cols_to_show = ['Expiry', 'Symbol', 'Type', 'Strike', 'Current Price', 'Safety %', 'Quantity', 'Notional', 'Delete', '_row_index']
        df_editor = df_editor[cols_to_show]

        edited_df = st.data_editor(
            df_editor,
            column_config={
                "Delete": st.column_config.CheckboxColumn("Del", width="small", default=False),
                "_row_index": None,
                "Expiry": st.column_config.DateColumn("Expiry", format="YYYY-MM-DD", width="medium"),
                "Strike": st.column_config.NumberColumn("Strike", format="$%.1f"),
                "Current Price": st.column_config.NumberColumn("Price", format="$%.1f"),
                "Notional": st.column_config.NumberColumn("Notional", format="$%,.0f"),
                "Safety %": st.column_config.ProgressColumn(
                    "Safety Net", 
                    format="%.1f%%", 
                    min_value=-20, 
                    max_value=50,
                    width="medium"
                ),
            },
            disabled=['Expiry', 'Symbol', 'Type', 'Strike', 'Current Price', 'Safety %', 'Quantity', 'Notional', '_row_index'],
            hide_index=True,
            use_container_width=True,
            key="position_editor"
        )
        
        rows_to_delete = edited_df[edited_df["Delete"] == True]
        
        if not rows_to_delete.empty:
            count = len(rows_to_delete)
            if st.button(f"🗑️ 確認刪除 ({count})", type="primary"):
                indices_to_del = rows_to_delete['_row_index'].tolist()
                delete_positions_batch(worksheet, indices_to_del)

    else:
        st.info("目前沒有持倉數據，請點擊上方「新增持倉」展開表單。")
else:
    st.error("無法連接 Google Sheets，請檢查 secrets.toml。")

