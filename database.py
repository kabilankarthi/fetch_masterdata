from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import os

DB1_URL = "postgresql://pvx_rouser:z3O9^K19mgINDzQd@20.29.20.104:5432/pvx_prod"  # postgres://user:pass@host:5432/db1
DB2_URL = "postgresql://postgres:postgres@172.17.0.1:5432/sedona"  # postgres://user:pass@host:5432/db2

engine_db1 = create_engine(DB1_URL, pool_pre_ping=True)
engine_db2 = create_engine(DB2_URL, pool_pre_ping=True)

SessionDB1 = sessionmaker(bind=engine_db1)
SessionDB2 = sessionmaker(bind=engine_db2)

