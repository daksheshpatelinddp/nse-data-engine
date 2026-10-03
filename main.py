import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

app = FastAPI(title="AmiBroker Data Engine Hosting", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Frontend files live in frontend/ (see docs/repo_structure.md) - main.py itself
# stays at the repo root so Render's existing start command is unaffected by
# this reorganization. Only the paths below changed.
FRONTEND_DIR = "frontend"

@app.get("/")
def serve_index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "AmiBroker Frontend Host Live."}

# PWA support files - these must be served from the ROOT URL PATH (not the
# root FILE location) for the browser's "Add to Home Screen" install prompt
# and offline service worker to work. The URL routes below stay at "/", the
# underlying files just now live in frontend/.
@app.get("/manifest.json")
def serve_manifest():
    return FileResponse(os.path.join(FRONTEND_DIR, "manifest.json"), media_type="application/manifest+json")

@app.get("/sw.js")
def serve_service_worker():
    # Service workers must be served with this exact scope in mind - serving it
    # from root (not e.g. /static/sw.js) is what allows it to control the whole
    # app rather than just a subfolder.
    return FileResponse(os.path.join(FRONTEND_DIR, "sw.js"), media_type="application/javascript")

@app.get("/icon-192.png")
def serve_icon_192():
    return FileResponse(os.path.join(FRONTEND_DIR, "icon-192.png"), media_type="image/png")

@app.get("/icon-512.png")
def serve_icon_512():
    return FileResponse(os.path.join(FRONTEND_DIR, "icon-512.png"), media_type="image/png")
