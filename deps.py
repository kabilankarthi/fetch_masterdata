from database import SessionDB1
from database import SessionDB2

def get_db1():
    db = SessionDB1()
    try:
        yield db
    finally:
        db.close()

def get_db2():
    db = SessionDB2()
    try:
        yield db
    finally:
        db.close()
