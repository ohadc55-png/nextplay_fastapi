web: alembic upgrade head && python scripts/build.py && uvicorn src.main:app --host 0.0.0.0 --port $PORT --workers 2 --timeout-keep-alive 300 --access-log
frontend: python scripts/build.py && uvicorn frontend_server:app --host 0.0.0.0 --port $PORT --workers 2 --access-log
