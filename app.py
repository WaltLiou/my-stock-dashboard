import streamlit as st
import gspread
import pandas as pd
import yfinance as yf
from datetime import datetime
import time

# --- Configuration ---
if "sheet_id" in st.secrets:
    SHEET_ID = st.secrets["sheet_id"]
else:
    st.error("Missing 'sheet_id' in secrets.toml")
    st.stop()

st.set_page_config(page_title="Stock Option Safety Net", layout="wide")

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
            return pd.DataFrame(columns=['Symbol', 'Type', 'Strike', 'Expiry', 'Quantity', 'EntryDate'])
        df = pd.DataFrame(data)
        
        # 1. 強制轉換數值
        df['Strike'] = pd.to_numeric(df['Strike'], errors='coerce')
        df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
        
        # 2. 強制轉換日期並排序 (解決問題3)
        df['Expiry'] = pd.to_datetime(df['Expiry'])
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
        time.sleep(1)
    except Exception as e:
        st.error(f"Error adding position: {e}")

def delete_position(worksheet, index_in_df):
    try:
        # sheet row index start from 1, header is 1, so data starts at 2.
        # But index_in_df is from dataframe which might be filtered or sorted.
        # This simple deletion relies on the original order. 
        # For safety in production, finding by ID is better, but here we assume direct mapping
        worksheet.delete_rows(index_in_df + 2)
        st.toast("🗑️ Position deleted.")
        time.sleep(1)
        st.rerun()
    except Exception as e:
        st.error(f"Error deleting position: {e}")

# --- Market Data & Calculations ---
@st.cache_data(ttl=60)
def get_current_prices(symbols):
    if not symbols: return {}
    prices = {}
    unique_symbols = list(set(symbols))
    
    for symbol in unique_symbols:
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1d")
            if not hist.empty:
                prices[symbol] = hist['Close'].iloc[-1]
            else:
                prices[symbol] = 0.0
        except:
            prices[symbol] = 0.0
    return prices

def process_market_data(df):
    if df.empty: return df
    
    symbols = df['Symbol'].unique().tolist()
    price_map = get_current_prices(symbols)
    
    df['Current Price'] = df['Symbol'].map(price_map).fillna(0.0)
    
    # 計算 Notional Value (名目本金) = Strike * Qty * 100
    df['Notional'] = df['Strike'] * df['Quantity'].abs() * 100
    
    # 解決問題 1 & 2: 正確計算 Put/Call 的安全距離
    def calculate_safety_gap(row):
        current = row['Current Price']
        strike = row['Strike']
        if current <= 0: return 0.0
        
        val = 0.0
        if row['Type'] == 'Put':
            val = (current - strike) / current
        else:
            val = (strike - current) / current
            
        return val * 100  # <--- 關鍵修改：這裡乘了 100

    df['Safety %'] = df.apply(calculate_safety_gap, axis=1)
    
    # 為分布圖建立 Bucket 標籤
    def get_bucket(val):
        if val < 0: return '<0%'
        elif val < 5: return '0-5%'    # 原本是 0.05
        elif val < 10: return '5-10%'  # 原本是 0.10
        elif val < 15: return '10-15%' # 原本是 0.15
        elif val < 20: return '15-20%' # 原本是 0.20
        else: return '>20%'
        
    df['Bucket'] = df['Safety %'].apply(get_bucket)
    df['ExpiryMonth'] = df['Expiry'].dt.strftime('%Y-%m')
    
    return df

# --- UI Components ---
def display_safety_matrix(df):
    """建立類似截圖的分布矩陣"""
    if df.empty: return

    st.subheader("🕸️ 安全網分布 (Notional Value)")

    # 解決問題 4: 切換 Put / Call
    view_type = st.radio("顯示類型", ["Put", "Call"], horizontal=True)
    
    # 篩選數據
    filtered_df = df[df['Type'] == view_type].copy()
    
    if filtered_df.empty:
        st.info(f"目前沒有 {view_type} 部位")
        return

    # 建立 Pivot Table
    # Index: 到期月份, Columns: 安全區間, Values: Notional 加總
    pivot = filtered_df.pivot_table(
        index='ExpiryMonth', 
        columns='Bucket', 
        values='Notional', 
        aggfunc='sum',
        fill_value=0
    )
    
    # 確保 Columns 順序正確 (解決問題 3 & 排版)
    col_order = ['<0%', '0-5%', '5-10%', '10-15%', '15-20%', '>20%']
    # 只保留資料中存在的欄位，並補齊缺失的欄位為 0
    pivot = pivot.reindex(columns=col_order, fill_value=0)
    
    # 增加「總計」欄位
    pivot['總計'] = pivot.sum(axis=1)

    # 格式化顯示：千分位
    st.dataframe(
        pivot.style.format("{:,.0f}"), 
        use_container_width=True
    )

# --- Main App ---
st.title("📈 Stock Option Tracker")

# -------- 新增這段 CSS 代碼來隱藏介面元素 --------
hide_streamlit_style = """
            <style>
            /* 隱藏右上角的漢堡選單 (☰) */
            #MainMenu {visibility: hidden;}
            
            /* 隱藏頁尾 (Made with Streamlit) */
            footer {visibility: hidden;}
            
            /* 隱藏上方的彩條 header (如果不需要留白) */
            header {visibility: hidden;}
            </style>
            """
st.markdown(hide_streamlit_style, unsafe_allow_html=True)

worksheet = get_sheet()

if worksheet:
    init_sheet(worksheet)
    
    # Sidebar: Add Position
    with st.sidebar:
        st.header("📝 Add New Position")
        with st.form("add_position_form", clear_on_submit=True):
            symbol = st.text_input("Symbol").upper()
            col_type, col_action = st.columns(2)
            with col_type:
                type_ = st.selectbox("Type", ["Put", "Call"])
            with col_action:
                side = st.selectbox("Action", ["Sell (Short)", "Buy (Long)"])
                
            strike = st.number_input("Strike Price", min_value=0.0, step=0.5)
            expiry = st.date_input("Expiry Date")
            qty_input = st.number_input("Quantity", min_value=1, step=1, value=1)
            quantity = -qty_input if "Sell" in side else qty_input
            
            if st.form_submit_button("Add Position"):
                if symbol:
                    add_position(worksheet, symbol, type_, strike, str(expiry), quantity)
                    st.rerun()
                else:
                    st.error("Please enter Symbol")

    # Load & Process Data
    df = load_data(worksheet)
    
    if not df.empty:
        df = process_market_data(df)
        
        # 1. 顯示安全網分布矩陣 (你最需要的功能)
        display_safety_matrix(df)
        
        st.divider()

        # 2. 詳細持倉列表
        st.subheader("📋 詳細持倉 (Portfolio)")
        
        # 格式化一下顯示的 DataFrame
        display_df = df[['Expiry', 'Symbol', 'Type', 'Strike', 'Current Price', 'Safety %', 'Quantity', 'Notional']].copy()
        
        # 設定顏色樣式
        def highlight_row(row):
            if row['Safety %'] < 0:
                return ['background-color: #ffebee; color: #c62828'] * len(row)
            elif row['Safety %'] < 5:  # <--- 這裡改成 5 (代表 5%)
                return ['background-color: #fffde7; color: #f57f17'] * len(row)
            return [''] * len(row)

        st.dataframe(
            display_df.style.apply(highlight_row, axis=1),
            use_container_width=True,
            column_config={
                "Expiry": st.column_config.DateColumn("Expiry", format="YYYY-MM-DD"),
                "Strike": st.column_config.NumberColumn("Strike", format="$%.1f"),
                "Current Price": st.column_config.NumberColumn("Price", format="$%.1f"),
                "Notional": st.column_config.NumberColumn("Notional", format="$%,.0f"),
                # 修正 3: 進度條設定調整
                "Safety %": st.column_config.ProgressColumn(
                    "Safety Net", 
                    format="%.1f%%",   # 這樣 10.5 就會顯示 10.5%
                    min_value=-20,     # 設定為 -20%
                    max_value=50,      # 設定為 50%
                    help="正數 = 價外(安全)距離 %; 負數 = 價內(已跌破/漲破)"
                ),
            },
            hide_index=True
        )

        # Delete Section
        st.subheader("🗑️ Delete Position")
        # 生成刪除選項時，加上索引以便查找
        delete_options = [
            f"{idx}: {row['Expiry'].strftime('%Y-%m')} | {row['Symbol']} {row['Type']} ${row['Strike']}" 
            for idx, row in df.iterrows()
        ]
        
        col1, col2 = st.columns([3, 1])
        with col1:
            selected_option = st.selectbox("Select to delete", options=delete_options)
        with col2:
            st.write("")
            st.write("")
            if st.button("Delete", type="primary"):
                if selected_option:
                    idx_to_del = int(selected_option.split(":")[0])
                    delete_position(worksheet, idx_to_del)
    else:
        st.info("目前沒有持倉數據，請從左側新增。")
else:
    st.error("無法連接 Google Sheets，請檢查設定。")
