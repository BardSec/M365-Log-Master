from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

_engine = None
_Session = None


class Base(DeclarativeBase):
    pass


def init_db(database_url: str):
    global _engine, _Session
    _engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        echo=False,
    )
    _Session = sessionmaker(bind=_engine, autocommit=False, autoflush=False)


def get_engine():
    return _engine


def get_session():
    """Return a new SQLAlchemy session. Caller is responsible for close()."""
    if _Session is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _Session()
