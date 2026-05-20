import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Database
    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL", "postgresql://m365user:m365pass@db:5432/m365logs"
    )

    # Flask
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    DEBUG: bool = os.environ.get("FLASK_ENV", "production") == "development"

    # Azure AD / Graph
    TENANT_ID: str = os.environ.get("TENANT_ID", "")
    CLIENT_ID: str = os.environ.get("CLIENT_ID", "")
    CLIENT_SECRET: str = os.environ.get("CLIENT_SECRET", "")
    GRAPH_SCOPE: str = os.environ.get(
        "GRAPH_SCOPE", "https://graph.microsoft.com/.default"
    )

    # Sync behaviour
    SYNC_INTERVAL_MINUTES: int = int(os.environ.get("SYNC_INTERVAL_MINUTES", "60"))
    # Overlap to catch out-of-order events
    SYNC_LOOKBACK_MINUTES: int = int(os.environ.get("SYNC_LOOKBACK_MINUTES", "5"))
    # How many records to fetch per Graph page
    GRAPH_PAGE_SIZE: int = int(os.environ.get("GRAPH_PAGE_SIZE", "500"))

    # Microsoft SSO (OAuth 2.0 authorization-code flow)
    OAUTH_ENABLED: bool = os.environ.get("OAUTH_ENABLED", "false").lower() == "true"
    # Must match a Web redirect URI registered in the Azure AD app registration
    OAUTH_REDIRECT_URI: str = os.environ.get(
        "OAUTH_REDIRECT_URI", "http://localhost:8080/auth/callback"
    )

    # Optional basic auth (alternative to OAuth; disabled if username is blank)
    BASIC_AUTH_USERNAME: str = os.environ.get("BASIC_AUTH_USERNAME", "")
    BASIC_AUTH_PASSWORD: str = os.environ.get("BASIC_AUTH_PASSWORD", "")

    # Cloudflare R2 archive (offload sign_in_events older than ARCHIVE_HOT_DAYS).
    # Bucket-blank or ARCHIVE_ENABLED=false disables archiving.
    R2_ACCOUNT_ID: str = os.environ.get("R2_ACCOUNT_ID", "")
    R2_ACCESS_KEY_ID: str = os.environ.get("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY: str = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET: str = os.environ.get("R2_BUCKET", "")
    R2_ENDPOINT: str = os.environ.get("R2_ENDPOINT", "")
    ARCHIVE_ENABLED: bool = os.environ.get("ARCHIVE_ENABLED", "false").lower() == "true"
    ARCHIVE_HOT_DAYS: int = int(os.environ.get("ARCHIVE_HOT_DAYS", "35"))

    @property
    def graph_configured(self) -> bool:
        return bool(self.TENANT_ID and self.CLIENT_ID and self.CLIENT_SECRET)

    @property
    def archive_configured(self) -> bool:
        return bool(
            self.ARCHIVE_ENABLED
            and self.R2_BUCKET
            and self.R2_ENDPOINT
            and self.R2_ACCESS_KEY_ID
            and self.R2_SECRET_ACCESS_KEY
        )
