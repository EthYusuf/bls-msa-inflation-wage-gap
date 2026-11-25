"""Configuration module for the BLS MSA inflation vs. wage gap analysis."""

from datetime import datetime
from pathlib import Path
from typing import Dict

# BLS Public Data API v2 endpoint
BLS_API_URL: str = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# Local SQLite cache settings
BASE_DIR: Path = Path(__file__).resolve().parent
CACHE_DB_PATH: Path = BASE_DIR / "bls_cache.db"
CACHE_TTL_HOURS: int = 24

# Default 5-year analysis window
CURRENT_YEAR: int = datetime.now().year
MIN_SUPPORTED_YEAR: int = 2010
MAX_SUPPORTED_YEAR: int = CURRENT_YEAR
DEFAULT_START_YEAR: int = CURRENT_YEAR - 5
DEFAULT_END_YEAR: int = CURRENT_YEAR

# BLS API v2 rate-limit constraints
API_LIMITS: Dict[str, Dict[str, int]] = {
    "unregistered": {
        "max_series_per_request": 25,
        "max_years_per_request": 10,
        "daily_query_limit": 25,
    },
    "registered": {
        "max_series_per_request": 50,
        "max_years_per_request": 20,
        "daily_query_limit": 500,
    },
}

REQUEST_TIMEOUT_SECONDS: int = 30
MAX_RETRIES: int = 3
RETRY_BACKOFF_SECONDS: float = 1.5

# BLS Series ID templates
# CPI Food: CUURS[MSA_CPI_CODE]SAF1
# CPI Energy: CUURS[MSA_CPI_CODE]SA0E
# Average Hourly Earnings (Total Private, SAE): SMU[STATE_FIPS][MSA_SAE_CODE]0500000003
SERIES_TEMPLATES: Dict[str, str] = {
    "food_cpi": "CUURS{cpi_code}SAF1",
    "energy_cpi": "CUURS{cpi_code}SA0E",
    "hourly_earnings": "SMU{state_fips}{sae_msa_code}0500000003",
}

# Target Metropolitan Statistical Areas (MSAs)
MSA_METADATA: Dict[str, Dict[str, str]] = {
    "New York-Newark-Jersey City, NY-NJ-PA": {
        "short_name": "New York",
        "cpi_code": "12A",
        "state_fips": "36",
        "sae_msa_code": "35620",
        "cpi_frequency": "monthly",
    },
    "Los Angeles-Long Beach-Anaheim, CA": {
        "short_name": "Los Angeles",
        "cpi_code": "49A",
        "state_fips": "06",
        "sae_msa_code": "31080",
        "cpi_frequency": "monthly",
    },
    "Chicago-Naperville-Elgin, IL-IN-WI": {
        "short_name": "Chicago",
        "cpi_code": "23A",
        "state_fips": "17",
        "sae_msa_code": "16980",
        "cpi_frequency": "monthly",
    },
    "Dallas-Fort Worth-Arlington, TX": {
        "short_name": "Dallas-Fort Worth",
        "cpi_code": "37A",
        "state_fips": "48",
        "sae_msa_code": "19100",
        "cpi_frequency": "bimonthly",
    },
    "Miami-Fort Lauderdale-West Palm Beach, FL": {
        "short_name": "Miami",
        "cpi_code": "35B",
        "state_fips": "12",
        "sae_msa_code": "33100",
        "cpi_frequency": "bimonthly",
    },
}


def build_msa_series_map() -> Dict[str, Dict[str, str]]:
    """Build concrete BLS Series IDs for every configured MSA."""
    series_map: Dict[str, Dict[str, str]] = {}
    for msa_name, meta in MSA_METADATA.items():
        series_map[msa_name] = {
            metric: template.format(
                cpi_code=meta["cpi_code"],
                state_fips=meta["state_fips"],
                sae_msa_code=meta["sae_msa_code"],
            )
            for metric, template in SERIES_TEMPLATES.items()
        }
    return series_map


MSA_SERIES_MAP: Dict[str, Dict[str, str]] = build_msa_series_map()

# Reverse lookup: series_id -> (msa_name, metric_key)
SERIES_TO_MSA_METRIC: Dict[str, tuple[str, str]] = {
    series_id: (msa_name, metric_key)
    for msa_name, metrics in MSA_SERIES_MAP.items()
    for metric_key, series_id in metrics.items()
}
