"""Dev entrypoint: python run.py (or: uvicorn sportbrobot.main:app --reload)."""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("sportbrobot.main:app", host="0.0.0.0", port=8000, reload=True)
