# Employee Attendance & Analytics API

A FastAPI + MongoDB service for employee attendance tracking, punch-in/punch-out flows, manual regularizations with an audit trail, and MongoDB-based analytics.

## Project goal

This API supports:
- employee creation
- punch-in and punch-out workflows
- attendance regularization with audit history
- analytics for:
  - monthly employee summary
  - department summary
  - late-comer leaderboard
  - department attendance trend

## Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 6.0+
- python-dotenv

## Setup

### 1) Create a virtual environment

```bash
python -m venv .venv
```

On Windows:

```powershell
.venv\Scripts\Activate.ps1
```

### 2) Install dependencies

```bash
pip install -r requirements.txt
```

### 3) Start MongoDB locally

If using local MongoDB installed on Windows:

```powershell
& "C:\Program Files\MongoDB\Server\9.0\bin\mongod.exe" --dbpath "C:\data\db"
```

If using Docker:

```bash
docker run -d --name mongo -p 27017:27017 mongo:7
```

### 4) Configure environment

Create a local `.env` file with:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Do not commit this file to Git.

### 5) Seed sample data

```bash
python sample_seed.py
```

### 6) Run the app

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

## Health check

```bash
curl http://127.0.0.1:8000/health
```

Expected response:

```json
{"status":"ok"}
```

## Main API endpoints

- `POST /employees`
- `GET /employees`
- `POST /attendance/punch-in`
- `POST /attendance/punch-out`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /attendance`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`

## Notes

- Business rules are enforced in the API logic.
- Derived values such as `late_minutes`, `overtime_minutes`, `work_hours`, and `half_day` are computed by the server and stored as authoritative values.
- Manual corrections are recorded in the `history` array.
- Unique database indexes are created automatically at startup for correctness and performance.
- The root `/` route is not an app endpoint; a `404` there is expected.

## Repository notes

- Keep the repository public for evaluation.
- Do not commit `.env`, secrets, or Docker files.
- Ensure the final repo contains the required files for submission:
  - app/main.py
  - requirements.txt
  - REVIEW.md
  - DECISIONS.md
  - README.md
