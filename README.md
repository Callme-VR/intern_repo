# Employee Attendance & Analytics API

FAST API for recording attendance and reporting workforce analytics. Built with **FastAPI** and **MongoDB**.

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-API-009688)
![MongoDB](https://img.shields.io/badge/MongoDB-6.0%2B-47A248)

## Overview

- Real-time punch-in / punch-out capture
- Manual regularizations with a full audit history
- Monthly and daily analytics computed **inside MongoDB** (aggregation pipelines, no in-memory loops)
- All timestamps anchored to **IST (UTC+05:30)**

## Table of Contents

1. [Business Rules](#business-rules)
2. [Concurrency Safety](#concurrency-safety)
3. [Analytics](#analytics)
4. [Quick Start](#quick-start)
5. [API Endpoints](#api-endpoints)
6. [Project Structure](#project-structure)
7. [Further Reading](#further-reading)

## Business Rules

| Rule | Behavior |
|------|----------|
| **R1** Time & calendar | IST only. Overnight shifts (e.g. 22:00-06:00) attribute early-morning punches to the previous day. Instants are truncated to whole seconds and serialized as epoch milliseconds. |
| **R2** Grace & late | 10-minute grace. Late minutes are measured from shift start once grace is exceeded. |
| **R3** Overtime | Counted only after a 30-minute threshold past shift end; handles next-day shift ends. |
| **R4** Work hours | Rounded half-up to 2 decimals using `Decimal`. |
| **R5** Half-day | Flagged when rounded hours are below 4.50. |
| **R6** Presence | `PRESENT`, `WFH` and `ON_DUTY` count as present. |
| **R7** Mid-month joiners | Working days are calculated from `joined_on`. |

## Concurrency Safety

| Scenario | Mechanism |
|----------|-----------|
| Concurrent punch-ins | Unique compound index on `(emp_code, date)`: one `201 Created`, the rest `409 Conflict` |
| Concurrent punch-outs | Atomic `find_one_and_update` on open records (`punch_out: None`) |
| Regularization conflicts | Optimistic lock on audit history length (`history: {$size: len}`) |

## Analytics

| Report | Technique |
|--------|-----------|
| Monthly employee summary | Aggregation with ISO day-of-week detection and join-date business days |
| Department summary | Cross-collection `$lookup`; headcount includes employees with zero records |
| Late leaderboard | `$setWindowFields` + `$rank` (standard competition ranking: 1, 2, 2, 4) |
| Daily department trend | `$densify` for full day sequence + 7-day rolling average |

**Query plans:** `GET /admin/explain/{endpoint}` runs with `executionStats`. All endpoints use `IXSCAN`, with no `COLLSCAN`.

## Quick Start

**Prerequisites:** Python 3.10+, MongoDB 6.0+ (tested on 7.0 and Atlas M0)

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # Linux / macOS

# 2. Install dependencies
pip install -r requirements.txt
```

**3. Configure environment** by creating a `.env` file in the project root:

```env
MONGO_URI="mongodb+srv://<user>:<password>@cluster.mongodb.net/?appName=Cluster0"
MONGO_DB="attendance_db"
```

```bash
# 4. (Optional) Seed sample data
python db/sample_seed.py

# 5. Run the server
uvicorn app.main:app --port 8000 --reload
```

Swagger UI: <http://localhost:8000/docs>

## API Endpoints

Full contract: [`openapi.yaml`](openapi.yaml)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health probe |
| GET | `/attendance` | Paginated attendance list (`page`, `page_size`, `date_from`, `date_to`) |
| GET | `/analytics/departments/summary` | Department summary for a month (`month=YYYY-MM`) |
| GET | `/analytics/leaderboard/late` | Late leaderboard (`month`, `limit`) |
| GET | `/analytics/departments/{dept}/trend` | Daily trend (`from`, `to`) |
| GET | `/admin/explain/{endpoint}` | Query plan with `executionStats` |

Punch, regularization and employee-summary routes are documented in `openapi.yaml`.

### Examples

```bash
curl http://localhost:8000/health
curl "http://localhost:8000/attendance?page=1&page_size=20"
curl "http://localhost:8000/analytics/departments/summary?month=2026-07"
curl "http://localhost:8000/analytics/leaderboard/late?month=2026-07&limit=10"
curl "http://localhost:8000/analytics/departments/Sales/trend?from=2026-07-06&to=2026-07-12"
curl "http://localhost:8000/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31"
```

## Project Structure

```text
.
├── app/
│   └── main.py           # Application, helpers, aggregation pipelines
├── db/
│   └── sample_seed.py    # Seeder for sample Extended JSON data
├── Sample_data/          # Sample MongoDB Extended JSON datasets
├── DECISIONS.md          # Design decisions, index rationale, race handling
├── REVIEW.md             # Bug fixes and starter-code review findings
├── openapi.yaml          # OpenAPI 3.0.3 contract
├── requirements.txt
└── README.md
```

## Further Reading

- [`DECISIONS.md`](DECISIONS.md): why the indexes and concurrency strategies were chosen
- [`REVIEW.md`](REVIEW.md): issues found and fixed in the starter code
