import os
import importlib

# Remove env vars so the module has nothing to read
os.environ.pop('MONGO_URI', None)
os.environ.pop('MONGO_DB', None)

try:
    import app.main
    importlib.reload(app.main)
    print('FAIL: should have raised KeyError for missing MONGO_URI/MONGO_DB')
except KeyError as e:
    print(f'PASS: missing env var raises KeyError → {e}')
except Exception as e:
    # OperationFailure or ConnectionFailure from pymongo when MONGO_URI was previously set
    # can also happen if module was already imported; KeyError is the target
    print(f'Got {type(e).__name__}: {e}')
