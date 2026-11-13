from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    bot_username: str = "MellokyVpnBot"
    admin_ids: str = ""

    shop_name: str = "Melloky VPN"
    support_url: str = "https://t.me/fuckmelloky"

    referral_percent: int = 15

    privacy_url: str = ""
    terms_url: str = ""

    db_url: str = "sqlite+aiosqlite:///data/melloky.db"

    server_name: str = "Melloky · EU"
    server_host: str = "de28.h1cloud.net"
    server_port: int = 443
    server_public_key: str = "REPLACE_WITH_REALITY_PUBLIC_KEY"
    server_sni: str = "cloudflare.com"
    server_sid: str = "6ba85179e30a1f01"
    server_flow: str = "xtls-rprx-vision"
    server_fp: str = "chrome"

    sub_server_host: str = "0.0.0.0"
    sub_server_port: int = 8080
    sub_base_url: str = ""

    panel_api_url: str = "https://s-25157.de28.h1clayd.shop/api"
    panel_api_url_direct: str = "http://direct-de28.h1cloud.net:25157/api"
    panel_api_token: str = ""

    extra_device_price: int = 49

    transfer_details: str = "+7 985 442-75-15"

    log_level: str = "INFO"
    proxy_url: str = ""

    @field_validator("db_url")
    @classmethod
    def _resolve_sqlite_path(cls, value: str) -> str:
        prefix = "sqlite+aiosqlite:///"
        if value.startswith(prefix) and not value.startswith(prefix + "/"):
            raw = value[len(prefix) :]
            path = Path(raw)
            if not path.is_absolute():
                path = BASE_DIR / path
                path.parent.mkdir(parents=True, exist_ok=True)
                return prefix + path.as_posix()
        return value

    @property
    def admins(self) -> set[int]:
        return {
            int(x)
            for x in self.admin_ids.replace(" ", "").split(",")
            if x.strip().isdigit()
        }

    @property
    def docs_links(self) -> dict[str, str]:
        return {"privacy": self.privacy_url, "terms": self.terms_url}

    @property
    def public_sub_base(self) -> str:
        if self.sub_base_url:
            return self.sub_base_url.rstrip("/")
        return f"http://{self.server_host}:{self.sub_server_port}"

    @property
    def panel_enabled(self) -> bool:
        return bool(self.panel_api_token.strip())


settings = Settings()
