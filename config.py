import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _clob_key() -> str:
    """POLYMARKET_API_KEY takes priority over the legacy CLOB_API_KEY."""
    return os.getenv("POLYMARKET_API_KEY") or os.getenv("CLOB_API_KEY", "")


def _clob_secret() -> str:
    return os.getenv("POLYMARKET_API_SECRET") or os.getenv("CLOB_SECRET", "")


def _clob_passphrase() -> str:
    return os.getenv("POLYMARKET_API_PASSPHRASE") or os.getenv("CLOB_PASS_PHRASE", "")


@dataclass
class Config:
    # ── Wallet / auth ──────────────────────────────────────────────────────────
    private_key: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))

    # Gnosis Safe proxy wallet address (holds USDC); blank = plain EOA flow
    proxy_wallet: str = field(default_factory=lambda: os.getenv("PROXY_WALLET", ""))

    # CLOB API credentials (POLYMARKET_API_* preferred; CLOB_* accepted as fallback)
    clob_api_key: str = field(default_factory=_clob_key)
    clob_secret: str = field(default_factory=_clob_secret)
    clob_pass_phrase: str = field(default_factory=_clob_passphrase)

    # ── Network ────────────────────────────────────────────────────────────────
    polygon_rpc_url: str = field(
        default_factory=lambda: os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    )

    # ── Winning-position auto-claim ────────────────────────────────────────────
    parsec_api_key: str = field(default_factory=lambda: os.getenv("PARSEC_API_KEY", ""))

    # ── Strategy ──────────────────────────────────────────────────────────────
    min_edge: float = field(default_factory=lambda: float(os.getenv("MIN_EDGE", "0.05")))
    max_trade_usdc: float = field(default_factory=lambda: float(os.getenv("MAX_TRADE_USDC", "50.0")))
    kelly_fraction: float = field(default_factory=lambda: float(os.getenv("KELLY_FRACTION", "0.25")))
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")

    # Maximum bid-ask spread to accept (wider = worse fill price)
    max_spread: float = field(default_factory=lambda: float(os.getenv("MAX_SPREAD", "0.10")))

    # Maximum edge to accept — suspiciously high edge usually means near-certain
    # markets (exact_c type) that escaped other filters; >30% edge had 50% WR
    max_edge: float = field(default_factory=lambda: float(os.getenv("MAX_EDGE", "0.30")))

    # Extra edge required for high-variability cities (Wellington, Chicago, etc.)
    high_variability_extra_edge: float = field(
        default_factory=lambda: float(os.getenv("HIGH_VARIABILITY_EXTRA_EDGE", "0.05"))
    )

    # ── Timing ────────────────────────────────────────────────────────────────
    scan_interval: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL", "300")))

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # ── Open-Meteo API key (optional — register free at open-meteo.com for higher limits) ──
    open_meteo_api_key: str = field(default_factory=lambda: os.getenv("OPEN_METEO_API_KEY", ""))

    # ── API endpoints (hardcoded) ──────────────────────────────────────────────
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_api: str = "https://clob.polymarket.com"
    ensemble_api: str = "https://ensemble-api.open-meteo.com/v1/ensemble"

    # ── Forecast model ────────────────────────────────────────────────────────
    ensemble_model: str = "ecmwf_ifs025"  # 50 members, best quality

    def has_trading_credentials(self) -> bool:
        return bool(self.private_key and self.private_key not in ("", "0xyour_private_key_here"))

    @property
    def signature_type(self) -> int:
        """
        0 = plain EOA
        2 = Gnosis Safe (proxy_wallet holds funds, EOA key signs)
        """
        return 2 if self.proxy_wallet else 0

    @property
    def funder_address(self) -> str | None:
        """The address that holds USDC — proxy wallet if set, else None (defaults to EOA)."""
        return self.proxy_wallet or None


cfg = Config()
