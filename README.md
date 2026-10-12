# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out, regularizations with an audit trail, and analytics aggregations. Contract: `openapi.yaml`. Data shapes: `DATA_MODEL.md`.

## Setup

Python 3.11+, MongoDB 6.0+ (local Docker or Atlas).

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and set `MONGO_URI` / `MONGO_DB`. You do **not** need MongoDB while writing code; you need it before you run the app.

Local MongoDB:

```bash
docker run -d --name mongo -p 27017:27017 mongo:7
```

Then:

```bash
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

`GET http://127.0.0.1:8000/health` should return `{"status":"ok"}`.

## Notes

- All application code is in `app/main.py`.
- Indexes are created at startup (and again on a successful health check).
- Do not commit `.env` or secrets.
