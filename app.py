import streamlit as st
import gspread
import pandas as pd
import yfinance as yf
from datetime import datetime
import time

# --- Configuration ---
# 確保 secrets.toml 裡有 sheet_id 和 gcp_service_account
if "sheet_id" in st.secrets:
    SHEET_ID = st.secrets["sheet_id"]
else:
    st.error("Missing 'sheet_id' in secrets.toml")
    st.stop()

st.set_page_config(page_title="Stock Option Tracker", layout="wide")

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
        # 移除了 Premium
        if not worksheet.get_all_values():
            header = ['Symbol', 'Type', 'Strike', 'Expiry', 'Quantity', 'EntryDate']
            worksheet.append_row(header)
    except Exception as e:
        st.error(f"Error initializing sheet: {e}")

# --- Data Handling ---
def load_data(worksheet):
    try:
        data = worksheet.get_all_records()
        # 移除了 Premium
        if not data:
            return pd.DataFrame(columns=['Symbol', 'Type', 'Strike', 'Expiry', 'Quantity', 'EntryDate'])
        df = pd.DataFrame(data)
        
        # 確保數據類型正確
        df['Strike'] = pd.to_numeric(df['Strike'], errors='coerce')
        df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return pd.DataFrame()

def add_position(worksheet, symbol, type_, strike, expiry, quantity):
    try:
        entry_date = datetime.now().strftime("%Y-%m-%d")
        # 移除了 Premium
        row = [symbol, type_, strike, expiry, quantity, entry_date]
        worksheet.append_row(row)
        st.toast(f"✅ Added: {symbol} {type_} {strike}")
        time.sleep(1)
    except Exception as e:
        st.error(f"Error adding position: {e}")

def delete_position(worksheet, index_in_df):
    try:
        worksheet.delete_rows(index_in_df + 2)
        st.toast("🗑️ Position deleted.")
        time.sleep(1)
        st.rerun()
    except Exception as e:
        st.error(f"Error deleting position: {e}")

# --- Market Data & Calculations ---
@st.cache_data(ttl=60)
def get_current_prices(symbols):
    """
    修改為使用 yf.Ticker().fast_info['last_price']
    這比 download 更適合抓取單一當前股價，且較不會因為 DataFrame 格式問題報錯。
    """
    if not symbols:
        return {}
    prices = {}
    unique_symbols = list(set(symbols))
    
    for symbol in unique_symbols:
        try:
            ticker = yf.Ticker(symbol)
            # fast_info 提供更即時的價格數據，且結構簡單
            last_price = ticker.fast_info.get('last_price', None)
            
            # 如果 last_price 抓不到，嘗試用 regularMarketPrice (有時因休市狀態不同)
            if last_price is None:
                 last_price = ticker.fast_info.get('regularMarketPrice', 0.0)
            
            prices[symbol] = last_price
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")
            prices[symbol] = 0.0
    return prices

def process_market_data(df):
    if df.empty:
        return df
    
    symbols = df['Symbol'].unique().tolist()
    price_map = get_current_prices(symbols)
    
    df['Current Price'] = df['Symbol'].map(price_map).fillna(0.0)
    
    # 計算安全網 (股價距離履約價多遠)
    def calculate_distance(row):
        current = row['Current Price']
        strike = row['Strike']
        if current <= 0: return 0.0
        
        # 計算百分比距離
        return (current - strike) / current

    df['Distance %'] = df.apply(calculate_distance, axis=1)
    
    return df

# --- UI ---
st.title("📈 Stock Option Tracker (No Premium)")

worksheet = get_sheet()

if worksheet:
    init_sheet(worksheet)
    
    # Sidebar
    st.sidebar.header("📝 Add New Position")
    with st.sidebar.form("add_position_form", clear_on_submit=True):
        symbol = st.text_input("Symbol").upper()
        col_type, col_action = st.columns(2)
        with col_type:
            type_ = st.selectbox("Type", ["Put", "Call"])
        with col_action:
            side = st.selectbox("Action", ["Sell (Short)", "Buy (Long)"])
            
        strike = st.number_input("Strike Price", min_value=0.0, step=0.5)
        expiry = st.date_input("Expiry Date")
        # Premium 輸入欄位已移除
        
        qty_input = st.number_input("Quantity", min_value=1, step=1, value=1)
        quantity = -qty_input if "Sell" in side else qty_input
        
        submitted = st.form_submit_button("Add Position")
        if submitted:
            if symbol:
                add_position(worksheet, symbol, type_, strike, str(expiry), quantity)
                st.rerun()
            else:
                st.sidebar.error("Please enter Symbol")

    # Main Dashboard
    df = load_data(worksheet)
    
    if not df.empty:
        df = process_market_data(df)
        
        # Styling Logic
        def highlight_status(row):
            styles = [''] * len(row)
            # 簡單的 ITM (價內) / OTM (價外) 顏色標記
            # 如果是 Put: 現價 < 履約價 = ITM (通常對賣方不利) -> 紅色
            # 如果是 Call: 現價 > 履約價 = ITM -> 紅色 (假設主要是賣方策略)
            
            # 這裡假設你是做賣方 (Selling Options)，ITM 為危險
            is_itm = False
            if row['Type'] == 'Put' and row['Current Price'] < row['Strike']:
                is_itm = True
            elif row['Type'] == 'Call' and row['Current Price'] > row['Strike']:
                is_itm = True
            
            if is_itm:
                return ['background-color: #ffcdd2; color: #b71c1c'] * len(row) # Red
            else:
                return ['background-color: #c8e6c9; color: #1b5e20'] * len(row) # Green
            
            return styles

        st.subheader("📊 Portfolio Overview")
        
        st.dataframe(
            df.style.apply(highlight_status, axis=1),
            use_container_width=True,
            column_config={
                "Strike": st.column_config.NumberColumn("Strike", format="$%.2f"),
                "Current Price": st.column_config.NumberColumn("Current Price", format="$%.2f"),
                "Distance %": st.column_config.ProgressColumn(
                    "Distance from Strike", 
                    format="%.1f%%", 
                    min_value=-0.5, 
                    max_value=0.5,
                    help="Positive: Price > Strike, Negative: Price < Strike"
                ),
                "EntryDate": st.column_config.DateColumn("Entry Date", format="YYYY-MM-DD"),
            },
            hide_index=True
        )
        
        st.divider()
        
        # Delete Functionality
        st.subheader("🗑️ Manage Positions")
        
        options = [
            f"{i}: {row['Symbol']} {row['Type']} ${row['Strike']} ({row['Expiry']})" 
            for i, row in df.iterrows()
        ]
        
        col1, col2 = st.columns([3, 1])
        with col1:
            selected_option = st.selectbox("Select Position to Delete", options=options)
            if selected_option:
                selected_index = int(selected_option.split(":")[0])
            
        with col2:
            st.write("") 
            st.write("") 
            if st.button("Delete Position", type="primary"):
                delete_position(worksheet, selected_index)
    else:
        st.info("No positions found. Add one from the sidebar!")
else:
    st.error("Could not connect to Google Sheets. Check your secrets.toml.")
