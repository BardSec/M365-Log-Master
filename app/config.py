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

    # Optional basic auth
    BASIC_AUTH_USERNAME: str = os.environ.get("BASIC_AUTH_USERNAME", "")
    BASIC_AUTH_PASSWORD: str = os.environ.get("BASIC_AUTH_PASSWORD", "")

    @property
    def graph_configured(self) -> bool:
        return bool(self.TENANT_ID and self.CLIENT_ID and self.CLIENT_SECRET)
