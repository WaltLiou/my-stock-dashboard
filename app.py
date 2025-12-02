import streamlit as st
import gspread
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, date
import calendar
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
            .block-container {padding-top: 1rem;}
            [data-testid="stMetricValue"] {
                font-size: 1.4rem;
                font-weight: 600;
            }
            [data-testid="stMetricLabel"] {
                font-size: 1rem;
                color: #555;
            }
            [data-testid="stMetricDelta"] {
                font-size: 0.85rem;
                color: #666 !important;
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

# --- Helper Functions ---
def get_next_third_friday():
    today = date.today()
    if today.month == 12:
        next_month = 1
        year = today.year + 1
    else:
        next_month = today.month + 1
        year = today.year
    
    c = calendar.Calendar(firstweekday=calendar.SUNDAY)
    month_cal = c.monthdatescalendar(year, next_month)
    fridays = [day for week in month_cal for day in week if day.weekday() == 4 and day.month == next_month]
    
    if len(fridays) >= 3:
        return fridays[2]
    return today + timedelta(days=30)

# --- Data Handling ---
def load_data(worksheet):
    try:
        data = worksheet.get_all_records()
        if not data:
            return pd.DataFrame(columns=['Symbol', 'Type', 'Strike', 'Expiry', 'Quantity', 'EntryDate', '_row_index'])
        
        df = pd.DataFrame(data)
        df['_row_index'] = df.index + 2 
        df['Strike'] = pd.to_numeric(df['Strike'], errors='coerce')
        df['Quantity'] = pd.to_numeric(df['Quantity'], errors='coerce')
        df['Expiry'] = pd.to_datetime(df['Expiry'], format='mixed', errors='coerce')
        
        df = df.dropna(subset=['Expiry'])
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
        time.sleep(1)
        st.rerun()
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
    
    today = pd.Timestamp.now().normalize()
    df['Days Left'] = (df['Expiry'] - today).dt.days
    
    df['Expiry Display'] = df['Expiry'].dt.strftime('%Y/%m/%d') + " (" + df['Days Left'].astype(str) + "d)"
    
    def calculate_metrics(row):
        current = row['Current Price']
        strike = row['Strike']
        type_ = row['Type']
        
        if current <= 0: return pd.Series([0.0, '', 0.0])
        
        safety_val = 0.0
        if type_ == 'Put':
            safety_val = (current - strike) / current
        else:
            safety_val = (strike - current) / current
        safety_pct = safety_val * 100
        
        status = "🟢"
        if type_ == 'Put' and current < strike:
            status = "🔴"
        elif type_ == 'Call' and current > strike:
            status = "🔴"
            
        risk_score = max(0, 5 - safety_pct)
            
        return pd.Series([safety_pct, status, risk_score])

    df[['Safety %', 'Status', 'Risk Score']] = df.apply(calculate_metrics, axis=1)
    
    def get_bucket(val):
        if val < 0: return '<0%'
        elif val < 5: return '0-5%'
        elif val < 10: return '5-10%'
        else: return '>10%'
        
    df['Bucket'] = df['Safety %'].apply(get_bucket)
    df['ExpiryMonth'] = df['Expiry'].dt.strftime('%Y-%m')
    
    return df

# --- UI Components ---

def display_kpi(df):
    def get_metrics(subset):
        p = subset[subset['Type'] == 'Put']
        c = subset[subset['Type'] == 'Call']
        return len(p), len(c), p['Notional'].sum(), c['Notional'].sum()

    total_p, total_c, notional_p, notional_c = get_metrics(df)
    total_count = len(df)
    total_notional = df['Notional'].sum()

    high_risk_df = df[df['Safety %'] < 5]
    risk_p, risk_c, _, _ = get_metrics(high_risk_df)
    total_risk = len(high_risk_df)

    today = pd.Timestamp.now().normalize()
    next_week = today + pd.Timedelta(days=7)
    exp_df = df[df['Expiry'] <= next_week]
    exp_p, exp_c, _, _ = get_metrics(exp_df)
    total_exp = len(exp_df)

    c1, c2, c3, c4 = st.columns(4)
    
    with c1:
        st.metric("總持倉數 (Total Count)", f"{total_count}", delta=f"P: {total_p} | C: {total_c}", delta_color="off")
    with c2:
        st.metric("總曝險 (Total Notional)", f"${total_notional/1000:,.1f} K", delta=f"P: ${notional_p/1000:,.1f}K | C: ${notional_c/1000:,.1f}K", delta_color="off")
    with c3:
        st.metric("⚠️ 高風險 (<5%)", f"{total_risk}", delta=f"P: {risk_p} | C: {risk_c}", delta_color="inverse" if total_risk > 0 else "off")
    with c4:
        st.metric("⏳ 本週到期", f"{total_exp}", delta=f"P: {exp_p} | C: {exp_c}", delta_color="off")

def display_input_form(worksheet):
    with st.expander("📝 新增持倉 (Add Position) - 智慧輸入", expanded=False):
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1: symbol_in = st.text_input("Symbol", placeholder="e.g. TSLA").upper().strip()
        with c2: type_in = st.selectbox("Type", ["Put", "Call"])
        with c3: side_in = st.selectbox("Action", ["Sell", "Buy"])
            
        c4, c5, c6 = st.columns([1, 1, 1])
        with c4: strike_in = st.number_input("Strike", min_value=0.0, step=0.5)
        with c5:
            default_date = get_next_third_friday()
            expiry_in = st.date_input("Expiry", value=default_date)
        with c6: qty_in = st.number_input("Qty (Abs)", min_value=1, step=1, value=1)

        feedback_text = ""
        feedback_color = "blue"
        if symbol_in and strike_in > 0:
            if side_in == "Sell" and type_in == "Put":
                feedback_text = f"⚠️ 承諾以 **${strike_in} 賣出** {symbol_in} (Sell Put)"
                feedback_color = "orange"
            elif side_in == "Sell" and type_in == "Call":
                feedback_text = f"⚠️ 承諾以 **${strike_in} 賣出** {symbol_in} (Sell Call)"
                feedback_color = "orange"
            elif side_in == "Buy":
                feedback_text = f"ℹ️ 支付權利金看{'漲' if type_in=='Call' else '跌'} {symbol_in}"
                feedback_color = "blue"
            st.markdown(f":{feedback_color}[{feedback_text}]")

        if st.button("Add Position", type="primary", use_container_width=True):
            if symbol_in and strike_in > 0:
                final_qty = -qty_in if "Sell" in side_in else qty_in
                add_position(worksheet, symbol_in, type_in, strike_in, str(expiry_in), final_qty)
            else:
                st.warning("請填寫完整的 Symbol 和 Strike")

def display_alerts(df):
    st.subheader("🚨 風險與到期監控 (Action Center)")
    
    today = pd.Timestamp.now().normalize()
    next_week = today + pd.Timedelta(days=7)
    
    expiring_soon = df[df['Expiry'] <= next_week].copy()
    high_risk = df[df['Safety %'] < 5].copy()
    
    c1, c2 = st.columns(2)
    
    # [修改區塊] 欄位順序調整與 Current Price 加回
    common_cfg = {
        "Status": st.column_config.TextColumn("狀態", width="small"),
        "Expiry Display": st.column_config.TextColumn("Expiry (Days)", width="medium"),
        "Symbol": st.column_config.TextColumn("Symbol", width="small"),
        "Safety %": st.column_config.NumberColumn("Safety", format="%.1f%%"),
        "Type": st.column_config.TextColumn("Type", width="small"),
        "Strike": st.column_config.NumberColumn("Strike", format="%.1f"),
        "Current Price": st.column_config.NumberColumn("Price", format="$%.1f"), # 加回 Current Price
        "Notional": st.column_config.NumberColumn("Notional", format="$%,.0f"),
    }
    
    # 新的欄位順序: Symbol -> Safety -> Type -> Strike -> Price -> Notional (最右邊)
    show_cols = ['Status', 'Expiry Display', 'Symbol', 'Safety %', 'Type', 'Strike', 'Current Price', 'Notional']

    with c1:
        if not expiring_soon.empty:
            st.error(f"⏳ 7 天內到期 ({len(expiring_soon)})")
            st.dataframe(expiring_soon[show_cols], use_container_width=True, hide_index=True, column_config=common_cfg)
        else:
            st.success("✅ 本週無到期部位")

    with c2:
        if not high_risk.empty:
            st.warning(f"⚠️ 高風險 Safety < 5% ({len(high_risk)})")
            st.dataframe(high_risk[show_cols], use_container_width=True, hide_index=True, column_config=common_cfg)
        else:
            st.success("✅ 所有部位 Safety > 5%")

def display_safety_matrix(df):
    if df.empty: return
    st.subheader("🕸️ 曝險分佈 (Notional Value)")
    
    col_radio, _ = st.columns([1, 5])
    with col_radio:
        view_type = st.radio("顯示類型", ["Put", "Call"], horizontal=True, label_visibility="collapsed")
    
    filtered_df = df[df['Type'] == view_type].copy()
    if filtered_df.empty:
        st.info(f"無 {view_type} 部位")
        return

    pivot = filtered_df.pivot_table(
        index='ExpiryMonth', 
        columns='Bucket', 
        values='Notional', 
        aggfunc='sum',
        fill_value=0,
        margins=True,       
        margins_name='Total'
    )
    
    desired_order = ['<0%', '0-5%', '5-10%', '>10%']
    existing_cols = [c for c in desired_order if c in pivot.columns]
    
    if 'Total' in pivot.columns:
        existing_cols.append('Total')
        
    pivot = pivot[existing_cols]

    styled_pivot = pivot.style.background_gradient(cmap='Blues', axis=None).format("${:,.0f}")
    
    st.dataframe(styled_pivot, use_container_width=True)

def display_full_list(worksheet, df):
    st.subheader("📋 持倉管理 (Full List)")
    
    all_symbols = sorted(df['Symbol'].unique())
    selected_syms = st.multiselect("🔍 Filter Symbol", all_symbols)
    
    df_view = df.copy()
    if selected_syms:
        df_view = df_view[df_view['Symbol'].isin(selected_syms)]

    df_view['Delete'] = False 
    
    cols_to_show = ['Expiry', 'Expiry Display', 'Symbol', 'Type', 'Strike', 'Current Price', 'Safety %', 'Risk Score', 'Notional', 'Delete', '_row_index']
    df_view = df_view[cols_to_show]

    edited_df = st.data_editor(
        df_view,
        column_config={
            "Delete": st.column_config.CheckboxColumn("Del", width="small"),
            "_row_index": None,
            "Expiry": st.column_config.DateColumn("Edit Date", format="YYYY-MM-DD", width="small"),
            "Expiry Display": st.column_config.TextColumn("Expiry (Days)", width="medium", disabled=True),
            "Strike": st.column_config.NumberColumn("Strike", format="%.1f"),
            "Current Price": st.column_config.NumberColumn("Price", format="%.1f"),
            "Notional": st.column_config.NumberColumn("Notional", format="$%,.0f"),
            "Safety %": st.column_config.NumberColumn("Safety %", format="%.1f%%"),
            "Risk Score": st.column_config.ProgressColumn(
                "Risk Monitor",
                help="Bar越長越危險",
                format=" ",
                min_value=0,
                max_value=30,
            ),
        },
        disabled=['Expiry Display', 'Symbol', 'Type', 'Strike', 'Current Price', 'Safety %', 'Risk Score', 'Notional', '_row_index'],
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

# --- Main App ---
st.title("📈 Stock Option Safety Net")

worksheet = get_sheet()

if worksheet:
    init_sheet(worksheet)
    
    df = load_data(worksheet)
    if not df.empty:
        with st.spinner('Updating market data...'):
            df = process_market_data(df)
            st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')}")
            
        display_kpi(df)
    
    st.divider()
    
    display_input_form(worksheet)
    
    if not df.empty:
        st.divider()
        display_alerts(df)
        st.divider()
        display_safety_matrix(df)
        st.divider()
        display_full_list(worksheet, df)
    else:
        st.info("目前沒有持倉數據，請使用上方表單新增。")
else:
    st.error("無法連接 Google Sheets，請檢查 secrets.toml。")
