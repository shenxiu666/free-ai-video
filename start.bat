@echo off
rem Quick start: double-click to run backend + frontend. Windows stay open on error.
cd /d %~dp0
start "free-ai-video-backend" cmd /k python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
start "free-ai-video-frontend" cmd /k npm run dev --prefix frontend
timeout /t 6 /nobreak >nul
start http://localhost:5173
