# -*- coding: utf-8 -*-
"""
Costly — OpenAI API利用料金ダッシュボード（履歴対応版）
- 期間選択: 今月 / 任意の期間（開始月〜終了月）
- 表示モード: 日別明細（デフォルト） / 月別サマリ（ラジオで切替）
- USDグラフ(縦軸ラベルつき), cost_jpy列, CSVダウンロード
"""

import os
import time
import math
import json
from pathlib import Path
from datetime import datetime, date, timezone
from dateutil import tz, relativedelta
from typing import Optional, Tuple, List, Dict

import requests
import pandas as pd
import streamlit as st
import altair as alt

# ==============================
# 定数・設定
# ==============================
APP_NAME = "Costly"
PAGE_TITLE = f"{APP_NAME} | OpenAI API利用料金"
PAGE_ICON = "💸"

OPENAI_ADMIN_KEY = st.secrets.get("OPENAI_ADMIN_KEY") or os.getenv("OPENAI_ADMIN_KEY")
COSTS_URL = "https://api.openai.com/v1/organization/costs"  # Costs API
TOKYO = tz.gettz("Asia/Tokyo")
RATE_CACHE_PATH = Path(".rate_cache.json")

HEADERS = {
    "Authorization": f"Bearer {OPENAI_ADMIN_KEY}",
    "Content-Type": "application/json",
}

# ==============================
# 為替レート（フォールバック + キャッシュ）
# ==============================
def _save_rate_cache(rate: float) -> None:
    try:
        RATE_CACHE_PATH.write_text(json.dumps(
            {"rate": float(rate), "saved_at_utc": datetime.utcnow().isoformat()},
            ensure_ascii=False
        ))
    except Exception:
        pass

def _load_rate_cache() -> Tuple[Optional[float], Optional[str]]:
    try:
        obj = json.loads(RATE_CACHE_PATH.read_text())
        return float(obj.get("rate")), obj.get("saved_at_utc")
    except Exception:
        return None, None

@st.cache_data(ttl=60 * 10)
def fetch_usd_jpy_rate() -> Tuple[Optional[float], str]:
    providers = [
        ("exchangerate.host", "https://api.exchangerate.host/latest", {"base": "USD", "symbols": "JPY"},
         lambda j: j["rates"]["JPY"]),
        ("frankfurter.app", "https://api.frankfurter.app/latest", {"from": "USD", "to": "JPY"},
         lambda j: j["rates"]["JPY"]),
        ("open.er-api.com", "https://open.er-api.com/v6/latest/USD", {}, lambda j: j["rates"]["JPY"]),
    ]
    for _, url, params, pick in providers:
        try:
            r = requests.get(url, params=params, timeout=8)
            r.raise_for_status()
            rate = float(pick(r.json()))
            _save_rate_cache(rate)
            return rate, "live"
        except Exception:
            continue
    cached, _ = _load_rate_cache()
    if cached:
        return cached, "cache"
    return None, "none"

# ==============================
# 期間ユーティリティ
# ==============================
def first_day(dt: date) -> datetime:
    return datetime(dt.year, dt.month, 1, 0, 0, 0, tzinfo=TOKYO)

def next_month_start(dt: date) -> datetime:
    start = first_day(dt)
    return (start + relativedelta.relativedelta(months=1))

def to_unix_utc(dt_tokyo: datetime) -> int:
    return int(dt_tokyo.astimezone(timezone.utc).timestamp())

def month_range_current() -> Tuple[int, int]:
    now_tokyo = datetime.now(TOKYO)
    start_tokyo = first_day(now_tokyo.date())
    end_tokyo = next_month_start(now_tokyo.date())
    return to_unix_utc(start_tokyo), to_unix_utc(end_tokyo)

def month_range_from_months(start_month: date, end_month: date) -> Tuple[int, int]:
    """開始月はその月初、終了月は『翌月初』までを範囲にする"""
    start_tokyo = first_day(start_month)
    end_tokyo = next_month_start(end_month)
    return to_unix_utc(start_tokyo), to_unix_utc(end_tokyo)

# ==============================
# Costs API
# ==============================
@st.cache_data(ttl=60 * 5)
def call_costs_api(start_ts: int, end_ts: Optional[int] = None, limit: int = 90) -> List[Dict]:
    """日次バケットでページング取得（Python側で月次集計する設計）"""
    params = {"start_time": start_ts, "bucket_width": "1d", "limit": limit}
    if end_ts:
        params["end_time"] = end_ts

    all_buckets: List[Dict] = []
    page: Optional[str] = None
    while True:
        p = params.copy()
        if page:
            p["page"] = page
        resp = requests.get(COSTS_URL, headers=HEADERS, params=p, timeout=30)
        if resp.status_code in (401, 403):
            raise PermissionError("Costs API へのアクセスが拒否されました。Admin Key（読み取り専用）が必要です。")
        resp.raise_for_status()
        payload = resp.json()
        all_buckets.extend(payload.get("data", []))
        if not (payload.get("has_more") and payload.get("next_page")):
            break
        page = payload.get("next_page")
        time.sleep(0.5)
    return all_buckets

def to_daily_df(buckets: List[Dict]) -> pd.DataFrame:
    rows = []
    for b in buckets:
        start_dt_utc = datetime.fromtimestamp(b["start_time"], tz=timezone.utc)
        date_jst = start_dt_utc.astimezone(TOKYO).date()
        usd = 0.0
        for r in b.get("results", []):
            amt = r.get("amount", {}).get("value")
            curr = (r.get("amount", {}).get("currency") or "usd").lower()
            if amt is not None and curr == "usd":
                usd += float(amt)
        rows.append({"date": pd.to_datetime(date_jst), "cost_usd": round(usd, 6)})
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return df

def to_monthly_df(df_daily: pd.DataFrame) -> pd.DataFrame:
    """日別DFを月別サマリ DataFrame に変換（必ず DataFrame を返す）"""
    if df_daily.empty:
        return pd.DataFrame({"month": pd.Series(dtype="string"),
                             "cost_usd": pd.Series(dtype="float")})
    df = df_daily.copy()
    df["month"] = df["date"].dt.to_period("M").astype(str)  # 'YYYY-MM'
    dfm = df.groupby("month", as_index=False, sort=True).agg(cost_usd=("cost_usd", "sum"))
    return dfm

# ==============================
# UI
# ==============================
st.set_page_config(page_title=PAGE_TITLE, page_icon=PAGE_ICON, layout="centered")
st.title(APP_NAME)
st.caption("OpenAI API利用料金ダッシュボード（今月＋履歴）")

with st.expander("設定 / オプション", expanded=False):
    period_mode = st.radio("集計範囲", ["今月", "任意の期間"], index=0, horizontal=True)
    manual_rate = st.number_input(
        "円換算レート（未入力=自動/フォールバック/キャッシュ）",
        min_value=0.0, value=0.0, step=0.01,
        help="自動取得に失敗時はキャッシュ→警告。必要ならここで手動入力。"
    )
    # ✅ 単一のラジオで表示モード切替（デフォルトは「日別明細」）
    display_mode = st.radio("表示モード", ["日別明細", "月別サマリ"], index=0, horizontal=True)

if not OPENAI_ADMIN_KEY:
    st.error("OPENAI_ADMIN_KEY が設定されていません（.streamlit/secrets.toml 推奨）。")
    st.stop()

# 期間決定
if period_mode == "今月":
    start_ts, end_ts = month_range_current()
    range_label = "今月"
    start_month_disp = pd.to_datetime(datetime.fromtimestamp(start_ts, tz=timezone.utc)).tz_convert(TOKYO).strftime("%Y-%m")
    end_month_disp_dt = pd.to_datetime(datetime.fromtimestamp(end_ts, tz=timezone.utc)).tz_convert(TOKYO) - pd.offsets.Day(1)
    end_month_disp = end_month_disp_dt.strftime("%Y-%m")
else:
    today = datetime.now(TOKYO).date()
    default_start = (today.replace(day=1) - relativedelta.relativedelta(months=5))
    default_end = today.replace(day=1)
    c1, c2 = st.columns(2)
    start_month_pick = c1.date_input("開始月", value=default_start, help="月初で扱います。日付は自動的に1日に補正。")
    end_month_pick = c2.date_input("終了月", value=default_end, help="この月の末日まで含めます（内部的には翌月初の手前まで）。")
    # 安全補正：日付の月初に寄せる
    start_month_pick = date(start_month_pick.year, start_month_pick.month, 1)
    end_month_pick = date(end_month_pick.year, end_month_pick.month, 1)
    if start_month_pick > end_month_pick:
        st.error("開始月が終了月より後になっています。")
        st.stop()
    start_ts, end_ts = month_range_from_months(start_month_pick, end_month_pick)
    range_label = f"{start_month_pick.strftime('%Y-%m')} 〜 {end_month_pick.strftime('%Y-%m')}"
    start_month_disp = start_month_pick.strftime("%Y-%m")
    end_month_disp = end_month_pick.strftime("%Y-%m")

# データ取得
try:
    buckets = call_costs_api(start_ts=start_ts, end_ts=end_ts)
    df_daily = to_daily_df(buckets)
    df_month = to_monthly_df(df_daily)
except PermissionError as e:
    st.error(str(e))
    st.stop()
except requests.HTTPError as e:
    st.error(f"Costs API 呼び出しエラー: {e}")
    st.stop()
except Exception as e:
    st.error(f"予期せぬエラー: {e}")
    st.stop()

# 為替
rate_note = ""
if manual_rate and manual_rate > 0:
    rate, source = manual_rate, "manual"
else:
    rate, source = fetch_usd_jpy_rate()

if source == "live":
    rate_note = "（ライブ取得）"
elif source == "cache":
    _, saved_at = _load_rate_cache()
    jst_text = ""
    if saved_at:
        try:
            jst_text = datetime.fromisoformat(saved_at).astimezone(TOKYO).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    st.info(f"為替レートはキャッシュを使用中{('（'+jst_text+' JST 保存）') if jst_text else ''}。ネット到達性に問題がある可能性があります。")
elif source == "none":
    st.warning("為替レートの取得に失敗（キャッシュなし）。必要に応じて手動レートを入力してください。")

# 集計値
total_usd = float(df_daily["cost_usd"].sum()) if not df_daily.empty else 0.0
total_jpy = (math.floor(total_usd * rate) if rate else None)

st.subheader(f"集計期間：{range_label}")
m1, m2, m3 = st.columns(3)
m1.metric("合計 (USD)", f"${total_usd:,.2f}")
m2.metric("合計 (JPY)", f"¥{total_jpy:,.0f}" if rate else "—", help=(f"レート: {rate:.3f} JPY/USD {rate_note}" if rate else "手動入力可"))
avg_day = (total_usd / max(len(df_daily), 1)) if not df_daily.empty else 0.0
m3.metric("日平均 (USD)", f"${avg_day:,.2f}")

st.caption("※ 東京時間の月初/翌月初で範囲を切り、Costs APIの日次バケットをPython側で集計しています。")

# 共通: JPY列を付与
def add_jpy(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if rate:
        out["cost_jpy"] = (out["cost_usd"] * rate).apply(lambda x: math.floor(x))
    else:
        out["cost_jpy"] = None
    return out

# ==============================
# 表示モード：日別 or 月別
# ==============================
if display_mode == "月別サマリ":
    dfm_disp = add_jpy(df_month)
    st.markdown("#### 月別サマリ")
    if not dfm_disp.empty:
        chart_m = (
            alt.Chart(dfm_disp)
            .mark_line(point=True)
            .encode(
                x=alt.X("month:N", title="Month"),
                y=alt.Y("cost_usd:Q", title="USD ($)"),
                tooltip=[alt.Tooltip("month:N", title="Month"),
                         alt.Tooltip("cost_usd:Q", title="Cost (USD)", format="$.2f")]
            ).properties(height=220)
        )
        st.altair_chart(chart_m, use_container_width=True)

        try:
            st.dataframe(
                dfm_disp,
                use_container_width=True,
                column_config={
                    "month": st.column_config.TextColumn("month"),
                    "cost_usd": st.column_config.NumberColumn("cost_usd ($)", format="$%.2f"),
                    "cost_jpy": st.column_config.NumberColumn("cost_jpy (¥)", format="¥%d"),
                },
            )
        except Exception:
            st.dataframe(dfm_disp, use_container_width=True)

        csv_m = dfm_disp.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "月別CSVをダウンロード",
            data=csv_m,
            file_name=f"costly_monthly_{start_month_disp}_to_{end_month_disp}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("この期間にはデータがありません。")

else:
    dfd_disp = add_jpy(df_daily)
    st.markdown("#### 日別明細")
    if not dfd_disp.empty:
        chart_d = (
            alt.Chart(dfd_disp)
            .mark_line(point=True)
            .encode(
                x=alt.X("date:T", title="Date (JST)"),
                y=alt.Y("cost_usd:Q", title="USD ($)"),
                tooltip=[alt.Tooltip("date:T", title="Date"),
                         alt.Tooltip("cost_usd:Q", title="Cost (USD)", format="$.2f")]
            ).properties(height=220)
        )
        st.altair_chart(chart_d, use_container_width=True)

        try:
            st.dataframe(
                dfd_disp,
                use_container_width=True,
                column_config={
                    "date": st.column_config.DatetimeColumn("date"),
                    "cost_usd": st.column_config.NumberColumn("cost_usd ($)", format="$%.2f"),
                    "cost_jpy": st.column_config.NumberColumn("cost_jpy (¥)", format="¥%d"),
                },
            )
        except Exception:
            st.dataframe(dfd_disp, use_container_width=True)

        csv_d = dfd_disp.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "日別CSVをダウンロード",
            data=csv_d,
            file_name=f"costly_daily_{start_month_disp}_to_{end_month_disp}.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        st.info("この期間にはデータがありません。")

with st.expander("デバッグ情報", expanded=False):
    st.json({
        "range_start_ts": start_ts, "range_end_ts": end_ts,
        "daily_rows": len(df_daily), "monthly_rows": len(df_month),
    })
