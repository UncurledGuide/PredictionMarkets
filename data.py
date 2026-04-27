import os
import ssl
from urllib.error import URLError

import certifi
from fredapi import Fred
import numpy as np
import pandas as pd
import yfinance as yf


os.environ["SSL_CERT_FILE"] = certifi.where()
ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=certifi.where())

FRED_API_KEY = os.getenv("FRED_API_KEY", "930f0c5f1f1e84c6a65e19eb04fbc680")
START_DATE = "2010-01-01"
END_DATE = "2025-12-31"
TRADING_DAYS = 252

fred = Fred(api_key=FRED_API_KEY)


def get_fred_frame(series_id, value_col):
    try:
        s = fred.get_series(series_id, observation_start=START_DATE)
    except (URLError, ssl.SSLError, ValueError) as err:
        print(f"FRED download failed for {series_id}: {err}")
        return pd.DataFrame(columns=["Date", value_col])
    frame = s.rename(value_col).reset_index()
    frame.columns = ["Date", value_col]
    return frame


def get_spx_frame():
    spx = yf.download("^GSPC", start=START_DATE, end=END_DATE, interval="1d")
    if spx.empty:
        return pd.DataFrame(columns=["Date", "Close"])
    #flatten columns
    if isinstance(spx.columns, pd.MultiIndex):
        spx.columns = spx.columns.get_level_values(0)
    return spx.reset_index()


def compute_annualized_return(close):
    daily_ret = close.pct_change()
    return (1.0 + daily_ret).rolling(TRADING_DAYS).apply(np.prod, raw=True) - 1.0


def build_daily_df(vix_df, epu_df, spx_df):
    # Keep one aligned daily index across all series.
    daily_df = pd.DataFrame()
    if not vix_df.empty:
        daily_df = vix_df.set_index("Date")[["VIX"]]
    if not epu_df.empty:
        epu_idx = epu_df.set_index("Date")[["EPU"]]
        daily_df = daily_df.join(epu_idx, how="outer") if not daily_df.empty else epu_idx
    if not spx_df.empty:
        spx_idx = spx_df.set_index("Date")[["SPX_Close"]]
        daily_df = daily_df.join(spx_idx, how="outer") if not daily_df.empty else spx_idx

    daily_df = daily_df.sort_index()
    if daily_df.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], name="Date"))
    daily_df.index = pd.to_datetime(daily_df.index)
    return daily_df


def build_monthly_df(daily_df):
    # Monthly snapshot at month-end.
    monthly_df = daily_df.resample("ME").last()
    for col in ["VIX", "EPU", "SPX_Close"]:
        if col not in monthly_df.columns:
            monthly_df[col] = np.nan
    monthly_df["VIX_diff"] = monthly_df["VIX"].diff()
    monthly_df["EPU_diff"] = monthly_df["EPU"].diff()
    monthly_df["SPX_ret"] = monthly_df["SPX_Close"].ffill().pct_change()
    monthly_df["SPX_ret_abs"] = monthly_df["SPX_ret"].abs()
    return monthly_df


def build_annualized_df(spx_df, monthly_index):
    # Keep annualized return series separate from monthly features.
    if not spx_df.empty and "SPX_Close" in spx_df.columns:
        spx_ann = spx_df[["Date", "SPX_Close"]].copy()
        spx_ann["Date"] = pd.to_datetime(spx_ann["Date"])
        spx_ann = spx_ann.set_index("Date").sort_index()
        annualized_df = pd.DataFrame(index=spx_ann.index)
        annualized_df["SPX_ann_return"] = compute_annualized_return(spx_ann["SPX_Close"])
        return annualized_df.resample("ME").last()
    annualized_df = pd.DataFrame(index=monthly_index)
    annualized_df["SPX_ann_return"] = np.nan
    return annualized_df


vix_df = get_fred_frame("VIXCLS", "VIX")
epu_df = get_fred_frame("USEPUINDXD", "EPU")
spx_df = get_spx_frame().rename(columns={"Close": "SPX_Close"})

daily_df = build_daily_df(vix_df, epu_df, spx_df)
monthly_df = build_monthly_df(daily_df)
annualized_df = build_annualized_df(spx_df, monthly_df.index)

 