# FlowOps AI API

Run locally:

```powershell
python -m uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8080
```

Fast demo path:

```text
POST /dev/reset
POST /jobs/demo/run
GET  /jobs/{job_id}/dashboard
GET  /jobs/{job_id}/events
GET  /jobs/{job_id}/report
```

