import os
from datetime import date, datetime, timedelta
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# ==============================================================================
# CONFIGURATION
# ==============================================================================
st.set_page_config(
    page_title="Architectural Fabrication | Welder Metrics",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Match FY_START_MONTH in your Apps Script (1 = Jan, 4 = Apr, 7 = Jul, 10 = Oct)
FY_START_MONTH = 1

# Current Booth Roster (Update here or override via a "Booths" tab in Google Sheets)
DEFAULT_BOOTH_ASSIGNMENTS = {
    "Booth #1": "Felipe Moreno",
    "Booth #2": "David Fain",
    "Booth #3": "Xavier Butler",
    "Booth #4": "Roy Ordanes",
    "Booth #5": "Rafael Perez",
    "Booth #6": "Jacob Riyanto",
    "Booth #7": "N/A",
    "Booth #8": "N/A",
}

# Custom Dark Styling
st.markdown(
    """
    <style>
        .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
        div[data-testid="stMetric"] {
            background-color: #1a1d24;
            border: 1px solid #2d323e;
            border-left: 4px solid #d32f2f;
            padding: 14px 18px;
            border-radius: 8px;
        }
        div[data-testid="stMetricLabel"] { color: #9aa0a6; font-size: 0.85rem; }
        div[data-testid="stMetricValue"] { color: #f8f9fa; font-size: 1.75rem; font-weight: 700; }
        .brand-banner {
            background: linear-gradient(90deg, #1a1d24 0%, #12141a 100%);
            border: 1px solid #2d323e;
            border-left: 5px solid #d32f2f;
            padding: 16px 22px;
            border-radius: 8px;
            margin-bottom: 18px;
        }
        .brand-title { font-size: 1.6rem; font-weight: 800; color: #ffffff; margin: 0; letter-spacing: 0.5px; }
        .brand-sub { font-size: 0.9rem; color: #ef5350; font-weight: 600; margin: 2px 0 0 0; }
        section[data-testid="stSidebar"] { background-color: #161920; border-right: 1px solid #2d323e; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ==============================================================================
# DATA LOADING & FISCAL HELPERS
# ==============================================================================
def get_iso_week_str(dt: date) -> str:
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def get_fy_quarter_str(dt: pd.Timestamp) -> str:
    if pd.isna(dt):
        return "FY-Unknown"
    month = dt.month
    year = dt.year
    start_offset = (FY_START_MONTH - 1 + 12) % 12
    adjusted_month = (month - 1 - start_offset + 12) % 12
    quarter = (adjusted_month // 3) + 1
    fy_year = year + 1 if (FY_START_MONTH > 1 and month >= FY_START_MONTH) else year
    return f"FY-Q{quarter}-{fy_year}"


@st.cache_data(ttl=30, show_spinner="Syncing live shop-floor metrics...")
def load_dashboard_data() -> tuple[pd.DataFrame, dict]:
    """
    Loads Dashboard_Data (and optional Booths tab) from Google Sheets.
    Supports either a private Service Account in st.secrets["gcp_service_account"]
    or a link-shared spreadsheet URL in st.secrets["sheet_url"].
    """
    sheet_url = st.secrets.get("sheet_url", "")
    if not sheet_url:
        st.error("Missing `sheet_url` in Streamlit Secrets. See setup instructions.")
        st.stop()

    if "gcp_service_account" in st.secrets:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_info(
            st.secrets["gcp_service_account"], scopes=scopes
        )
        client = gspread.authorize(creds)
        sh = client.open_by_url(sheet_url)
        ws = sh.worksheet("Dashboard_Data")
        records = ws.get_all_records()
        df = pd.DataFrame(records)

        # Optional dynamic Booths tab (Columns: Booth, Welder Name)
        booth_map = DEFAULT_BOOTH_ASSIGNMENTS.copy()
        try:
            booth_ws = sh.worksheet("Booths")
            b_records = booth_ws.get_all_records()
            if b_records:
                booth_map = {
                    str(r["Booth"]).strip(): str(r["Welder Name"]).strip() or "N/A"
                    for r in b_records
                    if r.get("Booth")
                }
        except gspread.WorksheetNotFound:
            pass
    else:
        # Fallback for link-shared Google Sheet (no service account required)
        sheet_id = sheet_url.split("/d/")[1].split("/")[0]
        csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet=Dashboard_Data"
        df = pd.read_csv(csv_url)
        booth_map = DEFAULT_BOOTH_ASSIGNMENTS.copy()

    if df.empty:
        return df, booth_map

    # Clean types
    df["Calendar Week"] = df["Calendar Week"].astype(str).str.strip()
    df["Welder Name"] = df["Welder Name"].astype(str).str.strip()
    df["WO Item Name"] = df["WO Item Name"].astype(str).str.strip()
    df["Fab Due Date"] = pd.to_datetime(df["Fab Due Date"], errors="coerce")
    df["FY Quarter"] = df["Fab Due Date"].apply(get_fy_quarter_str)

    num_cols = [
        "Units Completed",
        "Std Hrs/Unit",
        "Welder Scheduled Hrs",
        "Welder Actual Hrs",
        "Welder Over/Under",
    ]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    return df, booth_map


# ==============================================================================
# CHART & TABLE BUILDERS
# ==============================================================================
def style_over_under(val):
    if pd.isna(val) or not isinstance(val, (int, float)):
        return ""
    if val > 0.005:
        return "color: #ff6b6b; font-weight: 700;"  # Over budget (Red)
    if val < -0.005:
        return "color: #51cf66; font-weight: 700;"  # Under budget (Green)
    return "color: #adb5bd;"


def build_combo_chart(agg_df: pd.DataFrame, x_col: str, title: str) -> go.Figure:
    """
    Recreates and upgrades the Data Studio combo chart:
      - Orange bars: Scheduled Hrs
      - Purple bars: Over/Under Hrs (above or below 0)
      - Blue line + markers: Actual Hrs
    """
    fig = go.Figure()

    fig.add_trace(
        go.Bar(
            x=agg_df[x_col],
            y=agg_df["Welder Scheduled Hrs"],
            name="Welder Scheduled Hrs",
            marker_color="#f59f45",
            hovertemplate="<b>%{x}</b><br>Scheduled Hrs: %{y:.2f}<extra></extra>",
        )
    )

    fig.add_trace(
        go.Bar(
            x=agg_df[x_col],
            y=agg_df["Welder Over/Under"],
            name="Welder Over/Under",
            marker_color="#9775fa",
            hovertemplate="<b>%{x}</b><br>Over/Under: %{y:+.2f} hrs<extra></extra>",
        )
    )

    fig.add_trace(
        go.Scatter(
            x=agg_df[x_col],
            y=agg_df["Welder Actual Hrs"],
            name="Welder Actual Hrs",
            mode="lines+markers",
            line=dict(color="#4dabf7", width=3),
            marker=dict(size=9, color="#4dabf7", line=dict(width=1.5, color="#0e1117")),
            hovertemplate="<b>%{x}</b><br>Actual Hrs: %{y:.2f}<extra></extra>",
        )
    )

    fig.update_layout(
        title=dict(text=title, font=dict(size=16, color="#f0f2f6")),
        barmode="relative",
        template="plotly_dark",
        paper_bgcolor="#1a1d24",
        plot_bgcolor="#1a1d24",
        height=400,
        margin=dict(l=40, r=20, t=55, b=40),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(size=13),
        ),
        xaxis=dict(title="", tickfont=dict(size=12)),
        yaxis=dict(
            title="Hours",
            gridcolor="#2d323e",
            zeroline=True,
            zerolinecolor="#6c757d",
            zerolinewidth=1.5,
        ),
        hovermode="x unified",
    )
    return fig


def render_kpi_strip(df_view: pd.DataFrame):
    actual = df_view["Welder Actual Hrs"].sum()
    sched = df_view["Welder Scheduled Hrs"].sum()
    ou = df_view["Welder Over/Under"].sum()
    units = df_view["Units Completed"].sum()
    eff = (sched / actual * 100.0) if actual > 0 else 0.0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Actual Hours", f"{actual:,.2f}")
    c2.metric("Scheduled Hours", f"{sched:,.2f}")
    c3.metric(
        "Net Over / Under",
        f"{ou:+,.2f} hrs",
        delta="Under Budget" if ou <= 0 else "Over Budget",
        delta_color="inverse",
    )
    c4.metric("Efficiency", f"{eff:,.1f}%")
    c5.metric("Units Completed", f"{units:,.1f}")


def render_overview_page(df_view: pd.DataFrame, period_label: str):
    if df_view.empty:
        st.info(f"No fabrication labor or completed units recorded for **{period_label}**.")
        return

    render_kpi_strip(df_view)
    st.markdown("---")

    # 1. Cross-Tab Pivot Table (Welder Name x WO Item Name)
    st.subheader(f"Product & Welder Matrix — {period_label}")

    pivot = pd.pivot_table(
        df_view,
        index="Welder Name",
        columns="WO Item Name",
        values=["Welder Actual Hrs", "Welder Scheduled Hrs", "Welder Over/Under"],
        aggfunc="sum",
        fill_value=0.0,
    )

    # Reorder levels so WO Item Name is on top and metrics sit underneath (matching Data Studio)
    pivot = pivot.swaplevel(0, 1, axis=1)
    metric_order = ["Welder Actual Hrs", "Welder Scheduled Hrs", "Welder Over/Under"]

    # Sort product columns by total Actual Hrs descending
    item_totals = (
        df_view.groupby("WO Item Name")["Welder Actual Hrs"]
        .sum()
        .sort_values(ascending=False)
        .index.tolist()
    )
    ordered_cols = [
        (item, m) for item in item_totals for m in metric_order if (item, m) in pivot.columns
    ]
    pivot = pivot[ordered_cols]

    # Sort welder rows by total Actual Hrs descending and append Grand total row
    welder_order = (
        df_view.groupby("Welder Name")["Welder Actual Hrs"]
        .sum()
        .sort_values(ascending=False)
        .index.tolist()
    )
    pivot = pivot.reindex(welder_order)
    pivot.loc["Grand total"] = pivot.sum(axis=0)

    ou_cols = [c for c in pivot.columns if c[1] == "Welder Over/Under"]
    styled_pivot = (
        pivot.style.format("{:,.2f}")
        .map(style_over_under, subset=ou_cols)
        .set_properties(**{"text-align": "right"})
    )
    st.dataframe(styled_pivot, use_container_width=True)

    # 2. Welder Combo Chart (Scheduled, Over/Under, Actual)
    welder_agg = (
        df_view.groupby("Welder Name", as_index=False)[
            ["Welder Actual Hrs", "Welder Scheduled Hrs", "Welder Over/Under"]
        ]
        .sum()
        .sort_values("Welder Actual Hrs", ascending=False)
    )
    fig = build_combo_chart(
        welder_agg, "Welder Name", f"Welder Performance Breakdown — {period_label}"
    )
    st.plotly_chart(fig, use_container_width=True)


def render_booth_page(df_all: pd.DataFrame, booth_label: str, welder_name: str, quarter_filter: str):
    if not welder_name or welder_name.upper() == "N/A":
        st.warning(
            f"**{booth_label}** is currently unassigned (`N/A`). "
            "Assign a welder in `DEFAULT_BOOTH_ASSIGNMENTS` (or your `Booths` sheet tab) to activate this view."
        )
        return

    df_welder = df_all[
        (df_all["Welder Name"].str.lower() == welder_name.lower())
        & (df_all["FY Quarter"] == quarter_filter)
    ].copy()

    if df_welder.empty:
        st.info(f"No data logged for **{welder_name}** in **{quarter_filter}**.")
        return

    render_kpi_strip(df_welder)
    st.markdown("---")

    # Flag items with Scheduled Hrs > 0 but 0 Actual Hrs (e.g., labor booked outside FAB DRAW)
    zero_labor = df_welder[
        (df_welder["Welder Scheduled Hrs"] > 0) & (df_welder["Welder Actual Hrs"] == 0)
    ]
    if not zero_labor.empty:
        missing_items = ", ".join(sorted(zero_labor["WO Item Name"].unique()))
        st.warning(
            f"**Data Check:** `{missing_items}` has scheduled hours from completed units "
            "but **0.00 Actual Hrs** booked under `FAB DRAW` in Rootstock."
        )

    # 1. Product Summary Table for this Booth/Welder
    st.subheader(f"{booth_label} — Product Breakdown ({quarter_filter})")
    prod_table = (
        df_welder.groupby("WO Item Name", as_index=False)[
            [
                "Units Completed",
                "Welder Actual Hrs",
                "Welder Scheduled Hrs",
                "Welder Over/Under",
            ]
        ]
        .sum()
        .sort_values("Welder Actual Hrs", ascending=False)
    )

    grand_row = pd.DataFrame(
        [
            {
                "WO Item Name": "Grand total",
                "Units Completed": prod_table["Units Completed"].sum(),
                "Welder Actual Hrs": prod_table["Welder Actual Hrs"].sum(),
                "Welder Scheduled Hrs": prod_table["Welder Scheduled Hrs"].sum(),
                "Welder Over/Under": prod_table["Welder Over/Under"].sum(),
            }
        ]
    )
    prod_display = pd.concat([prod_table, grand_row], ignore_index=True)

    styled_prod = (
        prod_display.style.format(
            {
                "Units Completed": "{:,.1f}",
                "Welder Actual Hrs": "{:,.2f}",
                "Welder Scheduled Hrs": "{:,.2f}",
                "Welder Over/Under": "{:+,.2f}",
            }
        )
        .map(style_over_under, subset=["Welder Over/Under"])
    )
    st.dataframe(styled_prod, use_container_width=True, hide_index=True)

    # 2. Product Combo Chart for this Booth/Welder
    fig = build_combo_chart(
        prod_table, "WO Item Name", f"{welder_name} — Hours by Product Line ({quarter_filter})"
    )
    st.plotly_chart(fig, use_container_width=True)

    # 3. Expandable Work Order Drill-Down
    with st.expander("View Individual Work Orders & Releases"):
        wo_cols = [
            "Calendar Week",
            "Fab Due Date",
            "Order Number",
            "Production Release",
            "WO Item Name",
            "Units Completed",
            "Welder Actual Hrs",
            "Welder Scheduled Hrs",
            "Welder Over/Under",
        ]
        wo_detail = df_welder[wo_cols].sort_values("Fab Due Date", ascending=False).copy()
        wo_detail["Fab Due Date"] = wo_detail["Fab Due Date"].dt.strftime("%Y-%m-%d")
        st.dataframe(
            wo_detail.style.format(
                {
                    "Units Completed": "{:,.1f}",
                    "Welder Actual Hrs": "{:,.2f}",
                    "Welder Scheduled Hrs": "{:,.2f}",
                    "Welder Over/Under": "{:+,.2f}",
                }
            ).map(style_over_under, subset=["Welder Over/Under"]),
            use_container_width=True,
            hide_index=True,
        )


# ==============================================================================
# MAIN APPLICATION SHELL
# ==============================================================================
def main():
    df, booth_map = load_dashboard_data()

    today = date.today()
    this_week_str = get_iso_week_str(today)
    last_week_str = get_iso_week_str(today - timedelta(days=7))
    this_quarter_str = get_fy_quarter_str(pd.Timestamp(today))

    # Build Left Navigation Menu matching your Data Studio sidebar
    nav_items = ["Last Week", "This Week", "This Quarter"]
    booth_lookup = {}
    for booth, welder in booth_map.items():
        label = f"{booth} ({welder})"
        nav_items.append(label)
        booth_lookup[label] = (booth, welder)

    with st.sidebar:
        if os.path.exists("logo.png"):
            st.image("logo.png", use_container_width=True)
        else:
            st.markdown("### ARCHITECTURAL FABRICATION")
        st.caption("Welder Metrics 2026")
        st.markdown("---")

        selected_page = st.radio("Navigation", nav_items, label_visibility="collapsed")

        st.markdown("---")
        available_quarters = sorted(
            [q for q in df["FY Quarter"].unique() if q != "FY-Unknown"], reverse=True
        )
        if this_quarter_str not in available_quarters:
            available_quarters.insert(0, this_quarter_str)

        selected_quarter = st.selectbox(
            "Fiscal Quarter (Booth Views)",
            options=available_quarters,
            index=0,
        )

        if st.button("🔄 Refresh Live Data", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

        st.caption(f"Synced: {datetime.now().strftime('%I:%M:%S %p')}")

    # Header Banner
    col_logo, col_title = st.columns([1.4, 3.6])
    with col_logo:
        if os.path.exists("logo.png"):
            st.image("logo.png", use_container_width=True)
    with col_title:
        st.markdown(
            f"""
            <div class="brand-banner">
                <p class="brand-title">ARCHITECTURAL FABRICATION — WELDER METRICS</p>
                <p class="brand-sub">Active View: {selected_page}</p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Route Selected View
    if selected_page == "Last Week":
        df_filtered = df[df["Calendar Week"] == last_week_str]
        render_overview_page(df_filtered, f"Last Week ({last_week_str})")

    elif selected_page == "This Week":
        df_filtered = df[df["Calendar Week"] == this_week_str]
        render_overview_page(df_filtered, f"This Week ({this_week_str})")

    elif selected_page == "This Quarter":
        df_filtered = df[df["FY Quarter"] == this_quarter_str]
        render_overview_page(df_filtered, f"This Quarter ({this_quarter_str})")

    else:
        booth_name, welder_name = booth_lookup[selected_page]
        render_booth_page(df, booth_name, welder_name, selected_quarter)


if __name__ == "__main__":
    main()
