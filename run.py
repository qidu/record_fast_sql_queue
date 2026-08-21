"""Entry point for the recording service.

Run from the project root:
    python run.py
or:
    uvicorn src.main:app --reload
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
