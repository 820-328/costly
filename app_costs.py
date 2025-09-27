# -*- coding: utf-8 -*-
"""
Costly — OpenAI 今月のAPI利用料金ダッシュボード（Streamlit）改訂版
- ① グラフ縦軸に USD($) ラベル
- ② テーブルに cost_jpy 列
- ③ 為替レート取得のフォールバック & キャッシュ（.rate_cache.json）
"""

import os
import time
import math
import json
from pathlib import Path
from datetime import datetime, timezone
from dateutil import tz

import requests
import pandas as pd
import streamlit as st
import altair as alt

# ==============================
# 定数・設定
# ==============================
APP_NAME = "Costly"
PAGE_TITLE = f"{APP_NAME} | OpenAI 今月のAPI利用料金"
PAGE_ICON = "💸"

OPENAI_ADMIN_KEY = st.secrets.get("OPENAI_ADMIN_KEY") or os.getenv("OPENAI_ADMIN_KEY")
COSTS_URL = "https://api.openai.com/v1/organization/costs"  # Costs API エンドポイント
TOKYO = tz.gettz("Asia/Tokyo")
RATE_CACHE_PATH = Path(".rate_cache.json")

HEADERS = {
    "Authorization": f"Bearer {OPENAI_ADMIN_KEY}",
    "Content-Type": "application/json",
}

# ==============================
# 為替レート周り（フォールバック + キャッシュ）
# ==============================
def _save_rate_cache(rate: float):
    try:
        RATE_CACHE_PATH.write_text(json.dumps({
            "rate": float(rate),
            "saved_at_utc": datetime.utcnow().isoformat()
        }, ensure_ascii=False))
    except Exception:
        pass

def _load_rate_cache() -> tuple[float | None, str | None]:
    try:
        obj = json.loads(RATE_CACHE_PATH.read_text())
        return float(obj.get("rate")), obj.get("saved_at_utc")
    except Exception:
        return None, None

@st.cache_data(ttl=60 * 10)
def fetch_usd_jpy_rate() -> tuple[float | None, str]:
    """
    為替レートを複数プロバイダで順番に取得。
    成功: (rate, "live")
    失敗: (キャッシュorNone, "cache" / "none")
    """
    providers = [
        ("exchangerate.host", "https://api.exchangerate.host/latest", {"base": "USD", "symbols": "JPY"},
         lambda j: j["rates"]["JPY"]),
        ("frankfurter.app", "https://api.frankfurter.app/latest", {"from": "USD", "to": "JPY"},
         lambda j: j["rates"]["JPY"]),
        ("open.er-api.com", "https://open.er-api.com/v6/latest/USD", {}, lambda j: j["rates"]["JPY"]),
    ]
    for name, url, params, pick in providers:
        try:
            r = requests.get(url, params=params, timeout=8)
            r.raise_for_status()
            rate = float(pick(r.json()))
            _save_rate_cache(rate)
            return rate, "live"
        except Exception:
            continue

    # ここまで失敗 → キャッシュを試す
    cached, saved_at = _load_rate_cache()
    if cached:
        return cached, "cache"
    return None, "none"

# ==============================
# 期間ユーティリティ
# ==============================
def month_range_tokyo() -> tuple[int, int]:
    """東京時刻の月初00:00と翌月初00:00をUTCのUNIX秒で返す"""
    now_tokyo = datetime.now(TOKYO)
    start_tokyo = now_tokyo.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (
        start_tokyo.replace(year=start_tokyo.year + 1, month=1)
        if start_tokyo.month == 12
        else start_tokyo.replace(month=start_tokyo.month + 1)
    )
    start_utc = start_tokyo.astimezone(timezone.utc)
    end_utc = next_month.astimezone(timezone.utc)
    return int(start_utc.timestamp()), int(end_utc.timestamp())

def call_costs_api(start_ts: int, end_ts: int | None = None, limit: int = 45) -> list[dict]:
    """Costs APIをページングしつつ取得（日次バケット）"""
    params = {"start_time": start_ts, "bucket_width": "1d", "limit": limit}
    if end_ts:
        params["end_time"] = end_ts

    all_buckets, page_cursor = [], None
    while True:
        p = params.copy()
        if page_cursor:
            p["page"] = page_cursor
        resp = requests.get(COSTS_URL, headers=HEADERS, params=p, timeout=30)
        if resp.status_code in (401, 403):
            raise PermissionError("Costs API へのアクセスが拒否されました。Admin Key（読み取り専用）が必要です。")
        resp.raise_for_status()
        payload = resp.json()
        all_buckets.extend(payload.get("data", []))
        next_page = payload.get("next_page")
        if not (payload.get("has_more") and next_page):
            break
        page_cursor = next_page
        time.sleep(0.5)  # レートリミット配慮
    return all_buckets

def to_dataframe(buckets: list[dict]) -> pd.DataFrame:
    """バケット配列 → 日別USD合計のDataFrame"""
    rows = []
    for b in buckets:
        start_dt_utc = datetime.fromtimestamp(b["start_time"], tz=timezone.utc)
        date_jst = start_dt_utc.astimezone(TOKYO).date().isoformat()
        usd = 0.0
        for r in b.get("results", []):
            amt = r.get("amount", {}).get("value")
            curr = (r.get("amount", {}).get("currency") or "usd").lower()
            if amt is not None and curr == "usd":
                usd += float(amt)
        rows.append({"date": date_jst, "cost_usd": round(usd, 6)})
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)

# ==============================
# UI
# ==============================
st.set_page_config(page_title=PAGE_TITLE, page_icon=PAGE_ICON, layout="centered")
st.title(APP_NAME)
st.caption("OpenAI 今月のAPI利用料金ダッシュボード")

with st.expander("設定 / オプション", expanded=False):
    manual_rate = st.number_input(
        "円換算レート（空欄=自動取得・フォールバック/キャッシュ）",
        min_value=0.0, value=0.0, step=0.01,
        help="未入力または0なら自動で複数API→キャッシュの順に取得します。"
    )
    show_table = st.checkbox("日別明細テーブルを表示", value=True)
    show_chart = st.checkbox("日別推移チャートを表示", value=True)

# Key チェック
if not OPENAI_ADMIN_KEY:
    st.error("OPENAI_ADMIN_KEY が設定されていません。.streamlit/secrets.toml または環境変数に Admin Key（読み取り専用）を設定してください。")
    st.stop()

# データ取得
try:
    start_ts, end_ts = month_range_tokyo()
    buckets = call_costs_api(start_ts=start_ts, end_ts=end_ts)
    df = to_dataframe(buckets)
    month_total_usd = float(df["cost_usd"].sum()) if not df.empty else 0.0
except PermissionError as e:
    st.error(str(e))
    st.info("Admin Key の発行方法を確認してください。")
    st.stop()
except requests.HTTPError as e:
    st.error(f"Costs API 呼び出しでエラーが発生しました: {e}")
    st.stop()
except Exception as e:
    st.error(f"予期せぬエラー: {e}")
    st.stop()

# 為替レート
rate_source_note = ""
if manual_rate and manual_rate > 0:
    rate, source = manual_rate, "manual"
else:
    rate, source = fetch_usd_jpy_rate()

if source == "live":
    rate_source_note = "（ライブ取得）"
elif source == "cache":
    _, saved_at = _load_rate_cache()
    jst_time = ""
    if saved_at:
        try:
            jst_time = datetime.fromisoformat(saved_at).astimezone(TOKYO).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    st.info(f"為替レートは直近キャッシュを使用中{f'（{jst_time} JST 保存）' if jst_time else ''}。ネット到達性に問題がある可能性があります。")
elif source == "none":
    st.warning("為替レートの取得に失敗しました（キャッシュも無し）。必要に応じて手動レートを入力してください。")

# JPY換算
yen_total = math.floor(month_total_usd * rate) if (rate and month_total_usd) else None

# 見出し・メトリクス
today_jst = datetime.now(TOKYO)
st.subheader(f"{today_jst.strftime('%Y-%m')} の累計")
c1, c2, c3 = st.columns(3)
c1.metric("今月合計 (USD)", f"${month_total_usd:,.2f}")
if rate:
    c2.metric("今月合計 (JPY)", f"¥{yen_total:,.0f}", help=f"レート: {rate:.3f} JPY/USD {rate_source_note}")
else:
    c2.metric("今月合計 (JPY)", "—", help="レート未取得（手動入力可）")
avg_per_day = (month_total_usd / max(len(df), 1)) if not df.empty else 0.0
c3.metric("1日平均 (USD)", f"${avg_per_day:,.2f}")

st.caption("※ 東京時間の月初〜翌月初(未満)で集計。Costs API は日次バケットで返却されます。")

# cost_jpy 列を追加（レートがあれば）
df_display = df.copy()
if rate:
    df_display["cost_jpy"] = (df_display["cost_usd"] * rate).apply(lambda x: math.floor(x))
else:
    df_display["cost_jpy"] = None

# ① グラフ（Altairで縦軸ラベル）
if show_chart and not df.empty:
    df_chart = df.copy()
    df_chart["date"] = pd.to_datetime(df_chart["date"])
    chart = (
        alt.Chart(df_chart)
        .mark_line(point=True)
        .encode(
            x=alt.X("date:T", title="Date (JST)"),
            y=alt.Y("cost_usd:Q", title="USD ($)"),
            tooltip=[
                alt.Tooltip("date:T", title="Date"),
                alt.Tooltip("cost_usd:Q", title="Cost (USD)", format="$.2f"),
            ],
        )
        .properties(height=220)
    )
    st.altair_chart(chart, use_container_width=True)

# ② テーブル（USD/JPY 両方表示）
if show_table:
    try:
        st.dataframe(
            df_display,
            use_container_width=True,
            column_config={
                "date": st.column_config.TextColumn("date"),
                "cost_usd": st.column_config.NumberColumn("cost_usd ($)", format="$%.2f"),
                "cost_jpy": st.column_config.NumberColumn("cost_jpy (¥)", format="¥%d"),
            },
        )
    except Exception:
        # 古いStreamlitで column_config が無い場合
        st.dataframe(df_display, use_container_width=True)

with st.expander("APIレスポンス / 状態（デバッグ）", expanded=False):
    st.json({
        "start_time_utc": start_ts, "end_time_utc": end_ts,
        "buckets_count": len(buckets),
        "fx_rate": rate, "fx_source": source
    })
