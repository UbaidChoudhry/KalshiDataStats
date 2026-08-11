import uvicorn

from .config import Settings

if __name__ == "__main__":
    settings = Settings.from_environment()
    uvicorn.run("backend.app.main:app", host=settings.host, port=settings.port)
