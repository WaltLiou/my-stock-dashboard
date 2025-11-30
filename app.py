import streamlit as st
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import yfinance as yf
from datetime import datetime
import toml

# --- Configuration ---
SHEET_ID = "1WxWAa7V8j_5rVt2MeoLbyEXbql9FiiOC1t7_CpDaYAA"
SCOPE = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

st.set_page_config(page_title="Stock Option Tracker", layout="wide")

# --- Google Sheets Connection ---
@st.cache_resource
def get_gspread_client():
    try:
        secrets = st.secrets["gcp_service_account"]
        # Convert toml object to dict for gspread
        creds_dict = {
            "type": secrets["type"],
            "project_id": secrets["project_id"],
            "private_key_id": secrets["private_key_id"],
            "private_key": secrets["private_key"],
            "client_email": secrets["client_email"],
            "client_id": secrets["client_id"],
            "auth_uri": secrets["auth_uri"],
            "token_uri": secrets["token_uri"],
            "auth_provider_x509_cert_url": secrets["auth_provider_x509_cert_url"],
            "client_x509_cert_url": secrets["client_x509_cert_url"],
            "universe_domain": secrets.get("universe_domain", "googleapis.com")
        }
        
        # gspread service_account_from_dict expects the dict to be exactly right
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
        worksheet = sh.sheet1
        return worksheet
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
        return df
    except Exception as e:
        st.error(f"Error loading data: {e}")
        return pd.DataFrame()

def add_position(worksheet, symbol, type_, strike, expiry, premium, quantity):
    try:
        entry_date = datetime.now().strftime("%Y-%m-%d")
        row = [symbol, type_, strike, expiry, premium, quantity, entry_date]
        worksheet.append_row(row)
        st.success(f"Added position: {symbol} {type_} {strike}")
    except Exception as e:
        st.error(f"Error adding position: {e}")

def delete_position(worksheet, index):
    try:
        # Index is 0-based from dataframe, but sheet is 1-based and has header.
        # So row to delete is index + 2
        worksheet.delete_rows(index + 2)
        st.success("Position deleted.")
        st.rerun()
    except Exception as e:
        st.error(f"Error deleting position: {e}")

# --- Market Data & Calculations ---
def fetch_market_data(df):
    if df.empty:
        return df
    
    symbols = df['Symbol'].unique().tolist()
    prices = {}
    
    # Batch fetch for efficiency
    if symbols:
        try:
            tickers = yf.Tickers(' '.join(symbols))
            for symbol in symbols:
                try:
                    # Handle single ticker vs multiple tickers return structure
                    if len(symbols) == 1:
                        ticker = tickers.tickers[symbol]
                    else:
                        ticker = tickers.tickers[symbol]
                    
                    # Fast way to get current price
                    history = ticker.history(period="1d")
                    if not history.empty:
                        prices[symbol] = history['Close'].iloc[-1]
                    else:
                        prices[symbol] = 0.0
                except Exception:
                    prices[symbol] = 0.0
        except Exception as e:
            st.error(f"Error fetching market data: {e}")
    
    df['Current Price'] = df['Symbol'].map(prices)
    
    # Calculations
    # Safety Net % = (Current Price - Strike) / Current Price
    # Only relevant for Puts? Usually Safety Net is for Cash Secured Puts.
    # Formula given: (Current Price - Strike) / Current Price
    
    def calculate_safety_net(row):
        if row['Type'] == 'Put' and row['Current Price'] > 0:
            return (row['Current Price'] - row['Strike']) / row['Current Price']
        return 0.0

    df['Safety Net %'] = df.apply(calculate_safety_net, axis=1)
    
    # Unrealized P&L
    # (Premium - Current Option Price) * Quantity * 100
    # Since we don't have real-time option prices easily from free yfinance, 
    # we will use the fallback: Premium * Quantity * 100 (assuming current option price is 0 or just tracking collected premium)
    # User prompt said: "if can't get option price, temporarily use (Premium) * Quantity * 100"
    # Actually, if we sold the option (Quantity < 0), P&L is (Premium - CurrentPrice) * -Quantity * 100?
    # Let's stick to the user's simplified formula: (Premium - 0) * Quantity * 100 for now as 'N/A' replacement
    # But wait, if Quantity is negative (sold), and we use Premium * Quantity, we get negative number.
    # Usually for selling options:
    # Max Profit = Premium received.
    # If we just want to show "Premium Collected" as P&L for now:
    
    df['Unrealized P&L'] = df['Premium'] * df['Quantity'] * 100
    
    return df

# --- UI ---
st.title("📈 Stock Option Tracker")

worksheet = get_sheet()
if worksheet:
    init_sheet(worksheet)
    
    # Sidebar
    st.sidebar.header("Add New Position")
    with st.sidebar.form("add_position_form"):
        symbol = st.text_input("Symbol").upper()
        type_ = st.selectbox("Type", ["Put", "Call"])
        strike = st.number_input("Strike Price", min_value=0.0, step=0.5)
        expiry = st.date_input("Expiry Date")
        premium = st.number_input("Premium", min_value=0.0, step=0.01)
        quantity = st.number_input("Quantity (Negative for Sell)", step=1)
        
        submitted = st.form_submit_button("Add Position")
        if submitted:
            if symbol and quantity != 0:
                add_position(worksheet, symbol, type_, strike, str(expiry), premium, quantity)
                st.rerun()
            else:
                st.sidebar.error("Please enter Symbol and Quantity")

    # Main Dashboard
    df = load_data(worksheet)
    
    if not df.empty:
        df = fetch_market_data(df)
        
        # Formatting for display
        display_df = df.copy()
        
        # Apply styling
        def highlight_risk(row):
            # Red if Put and Current Price < Strike
            if row['Type'] == 'Put' and row['Current Price'] < row['Strike']:
                return ['background-color: #FFCDD2; color: #B71C1C'] * len(row)
            # Green if Safe (e.g. Put and Current Price > Strike + buffer? or just default safe)
            # User said "Safety Net display green background". Let's just default to none or light green for safe?
            # User: "Safety Net display green background" -> maybe specifically that column?
            # Or "Safe orders display green background".
            # Let's apply light green to rows that are NOT risky Puts.
            return ['background-color: #C8E6C9; color: #1B5E20'] * len(row)

        st.dataframe(
            display_df.style.apply(highlight_risk, axis=1)
            .format({
                "Strike": "${:.2f}",
                "Premium": "${:.2f}",
                "Current Price": "${:.2f}",
                "Safety Net %": "{:.2%}",
                "Unrealized P&L": "${:.2f}"
            }),
            use_container_width=True
        )
        
        # Delete Functionality
        st.subheader("Manage Positions")
        col1, col2 = st.columns([3, 1])
        with col1:
            delete_index = st.selectbox("Select Position to Delete", options=df.index, format_func=lambda x: f"{df.iloc[x]['Symbol']} {df.iloc[x]['Type']} ${df.iloc[x]['Strike']} (Qty: {df.iloc[x]['Quantity']})")
        with col2:
            if st.button("Delete Selected"):
                delete_position(worksheet, delete_index)
    else:
        st.info("No positions found. Add one from the sidebar!")
else:
    st.error("Could not connect to Google Sheets.")
