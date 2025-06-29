#!/usr/bin/env python3
"""
Run FastAPI application
"""

import os

import uvicorn

if __name__ == "__main__":
    # Set default environment variables if not set
    os.environ.setdefault("ENVIRONMENT", "development")
    os.environ.setdefault("DATABASE_HOST", "postgresql://localhost:5432")
    os.environ.setdefault("DATABASE_NAME", "nolas")

    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True, log_level="info")
