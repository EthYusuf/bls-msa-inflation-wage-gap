"""BLS API v2 client with rate-limit management, SQLite caching, and fallback support."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

from config import (
    API_LIMITS,
    BLS_API_URL,
    CACHE_DB_PATH,
    CACHE_TTL_HOURS,
    MAX_RETRIES,
    MSA_METADATA,
    MSA_SERIES_MAP,
    REQUEST_TIMEOUT_SECONDS,
    RETRY_BACKOFF_SECONDS,
    SERIES_TO_MSA_METRIC,
)

logger = logging.getLogger(__name__)


class BLSDataFetcher:
    """Fetches, caches, and manages BLS Public Data API v2 time series."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        db_path: Path = CACHE_DB_PATH,
        cache_ttl_hours: int = CACHE_TTL_HOURS,
    ) -> None:
        resolved_key = (api_key or os.getenv("BLS_API_KEY", "")).strip()
        self.api_key: Optional[str] = resolved_key if resolved_key else None
        self.db_path: Path = Path(db_path)
        self.cache_ttl_hours: int = cache_ttl_hours
        self.limits = API_LIMITS["registered" if self.api_key else "unregistered"]
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite tables for series observations and cache metadata."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bls_observations (
                        series_id TEXT NOT NULL,
                        year INTEGER NOT NULL,
                        period TEXT NOT NULL,
                        obs_date TEXT NOT NULL,
                        value REAL NOT NULL,
                        source TEXT NOT NULL DEFAULT 'api',
                        PRIMARY KEY (series_id, year, period)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS series_cache_meta (
                        series_id TEXT PRIMARY KEY,
                        start_year INTEGER NOT NULL,
                        end_year INTEGER NOT NULL,
                        fetched_at TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'api'
                    )
                    """
                )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("SQLite cache initialization failed: %s", exc)
            raise RuntimeError(f"Failed to initialize cache database: {exc}") from exc

    def _is_series_cache_valid(
        self, series_id: str, start_year: int, end_year: int
    ) -> bool:
        """Check whether cached observations cover the requested years and are fresh."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT start_year, end_year, fetched_at, source
                    FROM series_cache_meta
                    WHERE series_id = ?
                    """,
                    (series_id,),
                ).fetchone()
            if not row:
                return False

            cached_start, cached_end, fetched_at_str, source = row
            if source == "fallback":
                return False
            if cached_start > start_year or cached_end < end_year:
                return False

            fetched_at = datetime.fromisoformat(fetched_at_str)
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - fetched_at
            return age <= timedelta(hours=self.cache_ttl_hours)
        except (sqlite3.Error, ValueError) as exc:
            logger.warning("Cache validation error for %s: %s", series_id, exc)
            return False

    def _load_cached_series(
        self, series_ids: List[str], start_year: int, end_year: int
    ) -> pd.DataFrame:
        """Load cached observations for given series IDs within the year range."""
        if not series_ids:
            return pd.DataFrame()
        placeholders = ",".join("?" for _ in series_ids)
        query = f"""
            SELECT series_id, year, period, obs_date, value, source
            FROM bls_observations
            WHERE series_id IN ({placeholders})
              AND year BETWEEN ? AND ?
            ORDER BY series_id, obs_date
        """
        params: List[Any] = [*series_ids, start_year, end_year]
        try:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql_query(query, conn, params=params)
            return df
        except sqlite3.Error as exc:
            logger.error("Failed to read from SQLite cache: %s", exc)
            return pd.DataFrame()

    def _save_to_cache(
        self,
        records: List[Dict[str, Any]],
        fetched_series: List[str],
        start_year: int,
        end_year: int,
        source: str = "api",
    ) -> None:
        """Persist fetched observations and metadata into SQLite."""
        if not records:
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO bls_observations
                    (series_id, year, period, obs_date, value, source)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            r["series_id"],
                            int(r["year"]),
                            r["period"],
                            r["obs_date"],
                            float(r["value"]),
                            source,
                        )
                        for r in records
                    ],
                )
                for sid in fetched_series:
                    existing = conn.execute(
                        "SELECT start_year, end_year FROM series_cache_meta WHERE series_id = ?",
                        (sid,),
                    ).fetchone()
                    new_start = min(start_year, existing[0]) if existing else start_year
                    new_end = max(end_year, existing[1]) if existing else end_year
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO series_cache_meta
                        (series_id, start_year, end_year, fetched_at, source)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (sid, new_start, new_end, now_iso, source),
                    )
                conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to save observations to SQLite cache: %s", exc)

    def _chunk_year_ranges(
        self, start_year: int, end_year: int
    ) -> List[Tuple[int, int]]:
        """Split year range to comply with BLS API max-years-per-request limit."""
        max_years = self.limits["max_years_per_request"]
        ranges: List[Tuple[int, int]] = []
        cursor = start_year
        while cursor <= end_year:
            chunk_end = min(cursor + max_years - 1, end_year)
            ranges.append((cursor, chunk_end))
            cursor = chunk_end + 1
        return ranges

    def _chunk_series_ids(self, series_ids: List[str]) -> List[List[str]]:
        """Split series IDs to comply with BLS API max-series-per-request limit."""
        max_series = self.limits["max_series_per_request"]
        return [
            series_ids[i : i + max_series]
            for i in range(0, len(series_ids), max_series)
        ]

    def _post_bls_request(
        self, series_ids: List[str], start_year: int, end_year: int
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Execute a single BLS API v2 POST request with retry and backoff."""
        payload: Dict[str, Any] = {
            "seriesid": series_ids,
            "startyear": str(start_year),
            "endyear": str(end_year),
        }
        if self.api_key:
            payload["registrationkey"] = self.api_key

        headers = {"Content-type": "application/json"}
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = requests.post(
                    BLS_API_URL,
                    json=payload,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                if response.status_code == 429:
                    wait_time = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
                    logger.warning("BLS API rate limit (429). Retrying in %.1fs", wait_time)
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                body = response.json()
                status = body.get("status")
                messages = body.get("message", [])

                if status != "REQUEST_SUCCEEDED":
                    msg_text = "; ".join(messages) if messages else "Unknown BLS API error"
                    raise RuntimeError(f"BLS API status={status}: {msg_text}")

                parsed_records: List[Dict[str, Any]] = []
                series_list = body.get("Results", {}).get("series", [])
                for series_obj in series_list:
                    sid = series_obj.get("seriesID")
                    for item in series_obj.get("data", []):
                        period = str(item.get("period", ""))
                        if not period.startswith("M") or period == "M13":
                            continue
                        try:
                            year_val = int(item["year"])
                            month_val = int(period[1:])
                            numeric_val = float(item["value"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        obs_date = f"{year_val:04d}-{month_val:02d}-01"
                        parsed_records.append(
                            {
                                "series_id": sid,
                                "year": year_val,
                                "period": period,
                                "obs_date": obs_date,
                                "value": numeric_val,
                                "source": "api",
                            }
                        )
                return parsed_records, messages

            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    wait_time = RETRY_BACKOFF_SECONDS * attempt
                    logger.warning(
                        "BLS API attempt %d/%d failed (%s). Retrying in %.1fs",
                        attempt,
                        MAX_RETRIES,
                        exc,
                        wait_time,
                    )
                    time.sleep(wait_time)

        raise RuntimeError(f"BLS API request failed: {last_error}")

    @staticmethod
    def _generate_fallback_series(
        series_id: str, start_year: int, end_year: int
    ) -> List[Dict[str, Any]]:
        """Generate deterministic synthetic fallback data when API & cache are unavailable."""
        msa_name, metric = SERIES_TO_MSA_METRIC.get(series_id, ("Unknown", "food_cpi"))
        seed_bytes = hashlib.sha256(series_id.encode("utf-8")).digest()
        seed = int.from_bytes(seed_bytes[:4], "little")
        rng = np.random.default_rng(seed)

        is_bimonthly = (
            MSA_METADATA.get(msa_name, {}).get("cpi_frequency") == "bimonthly"
            and metric in {"food_cpi", "energy_cpi"}
        )

        now = datetime.now()
        dates = pd.date_range(
            start=f"{start_year}-01-01",
            end=min(
                datetime(end_year, 12, 1),
                datetime(now.year, now.month, 1),
            ),
            freq="MS",
        )

        if metric == "food_cpi":
            base = 265.0 + rng.uniform(-10, 20)
            monthly_drift = 0.0038 + rng.uniform(-0.0005, 0.0008)
            noise_std = 0.0035
        elif metric == "energy_cpi":
            base = 220.0 + rng.uniform(-15, 25)
            monthly_drift = 0.0045 + rng.uniform(-0.0008, 0.0012)
            noise_std = 0.022
        else:  # hourly_earnings
            base = 30.0 + rng.uniform(-2.0, 5.0)
            monthly_drift = 0.0031 + rng.uniform(-0.0004, 0.0005)
            noise_std = 0.0025

        records: List[Dict[str, Any]] = []
        val = base
        for idx, dt in enumerate(dates):
            seasonal = 0.0
            if metric == "energy_cpi":
                seasonal = 0.025 * np.sin(2 * np.pi * (dt.month - 4) / 12.0)
            shock = rng.normal(0.0, noise_std)
            if idx > 0:
                val = max(1.0, val * (1.0 + monthly_drift + shock + seasonal * 0.15))

            # Simulate bi-monthly CPI publication schedule (even months)
            if is_bimonthly and dt.month % 2 == 1:
                continue

            records.append(
                {
                    "series_id": series_id,
                    "year": dt.year,
                    "period": f"M{dt.month:02d}",
                    "obs_date": dt.strftime("%Y-%m-%d"),
                    "value": round(float(val), 2),
                    "source": "fallback",
                }
            )
        return records

    def fetch_msa_data(
        self,
        selected_msas: List[str],
        start_year: int,
        end_year: int,
        force_refresh: bool = False,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Fetch all 3 series for each selected MSA using cache, BLS API, or fallback."""
        if not selected_msas:
            raise ValueError("At least one Metropolitan Statistical Area (MSA) must be selected.")
        if start_year > end_year:
            raise ValueError("Start year cannot be greater than end year.")

        target_series: List[str] = []
        for msa in selected_msas:
            if msa not in MSA_SERIES_MAP:
                raise KeyError(f"Undefined MSA: {msa}")
            target_series.extend(MSA_SERIES_MAP[msa].values())

        series_to_fetch: List[str] = []
        cached_series: List[str] = []
        for sid in target_series:
            if not force_refresh and self._is_series_cache_valid(sid, start_year, end_year):
                cached_series.append(sid)
            else:
                series_to_fetch.append(sid)

        api_messages: List[str] = []
        api_records: List[Dict[str, Any]] = []
        fetched_from_api: set[str] = set()
        fallback_series: set[str] = set()

        if series_to_fetch:
            series_chunks = self._chunk_series_ids(series_to_fetch)
            year_chunks = self._chunk_year_ranges(start_year, end_year)

            for s_chunk in series_chunks:
                for y_start, y_end in year_chunks:
                    try:
                        chunk_records, msgs = self._post_bls_request(
                            s_chunk, y_start, y_end
                        )
                        api_records.extend(chunk_records)
                        api_messages.extend(msgs)
                        for rec in chunk_records:
                            fetched_from_api.add(rec["series_id"])
                        # Polite delay between consecutive chunks
                        time.sleep(0.25)
                    except Exception as exc:
                        logger.warning("API fetch error for chunk %s: %s", s_chunk, exc)
                        api_messages.append(str(exc))

            if api_records:
                self._save_to_cache(
                    api_records,
                    list(fetched_from_api),
                    start_year,
                    end_year,
                    source="api",
                )

        # Check for any series that failed to fetch from API
        missing_after_api = [sid for sid in series_to_fetch if sid not in fetched_from_api]
        stale_cache_df = pd.DataFrame()
        fallback_records: List[Dict[str, Any]] = []

        if missing_after_api:
            stale_cache_df = self._load_cached_series(
                missing_after_api, start_year, end_year
            )
            recovered_from_stale = (
                set(stale_cache_df["series_id"].unique())
                if not stale_cache_df.empty
                else set()
            )
            still_missing = [
                sid for sid in missing_after_api if sid not in recovered_from_stale
            ]
            for sid in still_missing:
                fallback_series.add(sid)
                fallback_records.extend(
                    self._generate_fallback_series(sid, start_year, end_year)
                )

        frames: List[pd.DataFrame] = []
        if cached_series:
            frames.append(self._load_cached_series(cached_series, start_year, end_year))
        if api_records:
            frames.append(pd.DataFrame(api_records))
        if not stale_cache_df.empty:
            frames.append(stale_cache_df)
        if fallback_records:
            frames.append(pd.DataFrame(fallback_records))

        if not frames:
            return pd.DataFrame(), {
                "cached_count": 0,
                "api_count": 0,
                "fallback_count": 0,
                "messages": api_messages,
                "used_fallback": False,
            }

        combined = pd.concat(frames, ignore_index=True)
        combined = combined.drop_duplicates(
            subset=["series_id", "year", "period"], keep="last"
        )
        combined["msa_name"] = combined["series_id"].map(
            lambda sid: SERIES_TO_MSA_METRIC.get(sid, ("Unknown", ""))[0]
        )
        combined["metric"] = combined["series_id"].map(
            lambda sid: SERIES_TO_MSA_METRIC.get(sid, ("", "unknown"))[1]
        )
        combined["date"] = pd.to_datetime(combined["obs_date"])
        combined = combined.sort_values(["msa_name", "metric", "date"]).reset_index(
            drop=True
        )

        meta = {
            "cached_count": len(cached_series),
            "api_count": len(fetched_from_api),
            "fallback_count": len(fallback_series),
            "used_fallback": len(fallback_series) > 0,
            "messages": api_messages,
        }
        return combined, meta
