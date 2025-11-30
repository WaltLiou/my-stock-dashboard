import streamlit as st
import gspread
import pandas as pd
import yfinance as yf
from datetime import datetime
import time

# --- Configuration ---
SHEET_ID = st.secrets["sheet_id"]

st.set_page_config(page_title="Stock Option Tracker", layout="wide")

# --- Google Sheets Connection ---
@st.cache_resource
def get_gspread_client():
    try:
        # 直接將 st.secrets 轉換為 dict 即可，無需手動對應欄位
        # 確保 secrets.toml 結構為 [gcp_service_account] 下方直接放 json 內容
        creds_dict = dict(st.secrets["gcp_service_account"])
        gc = gspread.service_account_from_dict(creds_dict)
        return gc
    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
        return None

def get_sheet():
    gc = get_gspread_client()
    if not gc:
        return None
    try:
        sh = gc.open_by_key(SHEET_ID)
        return sh.sheet1
    except Exception as e:
        st.error(f"Failed to open sheet: {e}")
        return None

def init_sheet(worksheet):
    try:
        if not worksheet.get_all_values():
            header = ['Symbol', 'Type', 'Strike', 'Expiry', 'Premium', 'Quantity', 'EntryDate']
            worksheet.append_row(header)
    except Exception as e:
        st.error(f"Error initializing sheet: {e}")

# --- Data Handling ---
def load_data(worksheet):
    try:
        data = worksheet.get_all_records()
        if not data:
            return pd.DataFrame(columns=['Symbol', 'Type', 'Strike', 'Expiry', 'Premium', 'Quantity', 'EntryDate'])
        df = pd.DataFrame(data)
        # 確保數據類型正確，避免格式錯誤
        df['Strike'] = pd.to_numeric(df['Strike'], errors='coerce')
        df['Premium'] = pd.to_numeric(df['Premium'], errors='coerce')
        df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return pd.DataFrame()

def add_position(worksheet, symbol, type_, strike, expiry, premium, quantity):
    try:
        entry_date = datetime.now().strftime("%Y-%m-%d")
        row = [symbol, type_, strike, expiry, premium, quantity, entry_date]
        worksheet.append_row(row)
        st.toast(f"✅ Added: {symbol} {type_} {strike}") # 使用 toast 取代 success，介面更乾淨
        time.sleep(1) # 讓使用者看到提示
    except Exception as e:
        st.error(f"Error adding position: {e}")

def delete_position(worksheet, index_in_df):
    try:
        # 注意：這裡假設 Dataframe 沒有被排序過。
        # Google Sheets 是 1-based，且有 header (佔 1 行)，所以是 index + 2
        worksheet.delete_rows(index_in_df + 2)
        st.toast("🗑️ Position deleted.")
        time.sleep(1)
        st.rerun()
    except Exception as e:
        st.error(f"Error deleting position: {e}")

# --- Market Data & Calculations ---
# 加入快取，TTL 設定為 60 秒，避免頻繁呼叫 API
@st.cache_data(ttl=60)
def get_current_prices(symbols):
    if not symbols:
        return {}
    prices = {}
    try:
        # 使用批量下載，比迴圈快
        unique_symbols = list(set(symbols))
        # period='1d' 足夠，group_by='ticker' 方便處理
        tickers = yf.download(unique_symbols, period="1d", group_by='ticker', progress=False)
        
        for symbol in unique_symbols:
            try:
                if len(unique_symbols) == 1:
                    # yfinance 單一股票結構不同，直接取 Close
                    price = tickers['Close'].iloc[-1].item()
                else:
                    price = tickers[symbol]['Close'].iloc[-1].item()
                prices[symbol] = price
            except Exception:
                prices[symbol] = 0.0
    except Exception as e:
        st.warning(f"Market data fetch warning: {e}")
    return prices

def process_market_data(df):
    if df.empty:
        return df
    
    symbols = df['Symbol'].unique().tolist()
    price_map = get_current_prices(symbols)
    
    df['Current Price'] = df['Symbol'].map(price_map).fillna(0.0)
    
    # 邏輯運算
    def calculate_safety_net(row):
        if row['Type'] == 'Put' and row['Current Price'] > 0:
            return (row['Current Price'] - row['Strike']) / row['Current Price']
        return 0.0

    df['Safety Net %'] = df.apply(calculate_safety_net, axis=1)
    
    # P&L Display Logic (顯示已收權利金總額)
    # 賣出選擇權 (Quantity < 0)，Premium 是正的現金流
    df['Total Premium'] = df['Premium'] * df['Quantity'].abs() * 100
    
    return df

# --- UI ---
st.title("📈 Stock Option Tracker")

worksheet = get_sheet()

if worksheet:
    init_sheet(worksheet)
    
    # Sidebar
    st.sidebar.header("📝 Add New Position")
    with st.sidebar.form("add_position_form", clear_on_submit=True): # clear_on_submit 自動清空
        symbol = st.text_input("Symbol").upper()
        col_type, col_action = st.columns(2)
        with col_type:
            type_ = st.selectbox("Type", ["Put", "Call"])
        with col_action:
            side = st.selectbox("Action", ["Sell (Short)", "Buy (Long)"])
            
        strike = st.number_input("Strike Price", min_value=0.0, step=0.5)
        expiry = st.date_input("Expiry Date")
        premium = st.number_input("Premium Price", min_value=0.0, step=0.01)
        qty_input = st.number_input("Quantity", min_value=1, step=1, value=1)
        
        # 自動處理正負號
        quantity = -qty_input if "Sell" in side else qty_input
        
        submitted = st.form_submit_button("Add Position")
        if submitted:
            if symbol:
                add_position(worksheet, symbol, type_, strike, str(expiry), premium, quantity)
                st.rerun()
            else:
                st.sidebar.error("Please enter Symbol")

    # Main Dashboard
    df = load_data(worksheet)
    
    if not df.empty:
        df = process_market_data(df)
        
        # Styling
        def highlight_risk(row):
            styles = [''] * len(row)
            # 安全網邏輯：如果是 Put 且 現價 < 履約價 (ITM for Short Put)，標示紅色
            # 如果是 Sell Put 且 現價 > 履約價，標示綠色
            
            if row['Type'] == 'Put' and row['Quantity'] < 0:
                if row['Current Price'] < row['Strike']:
                    # 危險：跌破履約價 (ITM)
                    return ['background-color: #ffcdd2; color: #b71c1c'] * len(row)
                else:
                    # 安全：價格在履約價之上 (OTM)
                    return ['background-color: #c8e6c9; color: #1b5e20'] * len(row)
            return styles

        st.subheader("📊 Portfolio Overview")
        
        # 使用 st.dataframe 的 column_config 進行更漂亮的格式化
        st.dataframe(
            df.style.apply(highlight_risk, axis=1),
            use_container_width=True,
            column_config={
                "Strike": st.column_config.NumberColumn("Strike", format="$%.2f"),
                "Premium": st.column_config.NumberColumn("Premium", format="$%.2f"),
                "Current Price": st.column_config.NumberColumn("Current Price", format="$%.2f"),
                "Safety Net %": st.column_config.ProgressColumn(
                    "Safety Net", 
                    format="%.1f%%", 
                    min_value=-0.5, 
                    max_value=0.5,
                    help="Distance from Strike Price"
                ),
                "Total Premium": st.column_config.NumberColumn("Total Premium", format="$%.2f"),
                "EntryDate": st.column_config.DateColumn("Entry Date", format="YYYY-MM-DD"),
            },
            hide_index=True # 隱藏 Pandas Index，介面更乾淨
        )
        
        st.divider()
        
        # Delete Functionality
        st.subheader("🗑️ Manage Positions")
        
        # 建立一個下拉選單用的標籤列表
        options = [
            f"{i}: {row['Symbol']} {row['Type']} ${row['Strike']} ({row['Expiry']})" 
            for i, row in df.iterrows()
        ]
        
        col1, col2 = st.columns([3, 1])
        with col1:
            selected_option = st.selectbox("Select Position to Delete", options=options)
            # 解析出 index
            selected_index = int(selected_option.split(":")[0])
            
        with col2:
            st.write("") # Spacer
            st.write("") # Spacer
            if st.button("Delete Position", type="primary"):
                delete_position(worksheet, selected_index)
    else:
        st.info("No positions found. Add one from the sidebar!")
else:
    st.error("Could not connect to Google Sheets. Check your secrets.toml.")
