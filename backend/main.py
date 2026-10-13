import asyncio
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Read a local .env file (if present) before any backend module reads env vars.
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from . import jobs, scheduler_api
from .database import init_schedule_db

logger = logging.getLogger("freeroomfinder")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_schedule_db()

    # Schedule-builder background jobs. Each is safe to skip/fail and guarded
    # against double execution by a per-job lock inside jobs.py.
    bg_tasks: list[asyncio.Task] = []
    if not jobs.background_jobs_disabled():
        bg_tasks.append(asyncio.create_task(jobs.scraper_loop(scheduler_api.get_active_term)))
        bg_tasks.append(asyncio.create_task(jobs.rmp_loop()))

        # Don't block startup on a full scrape: if the sections table is
        # empty, kick off a one-off scrape and start serving "loading".
        if not await scheduler_api.sections_exist():
            logger.info("sections table empty; starting background scrape")
            bg_tasks.append(asyncio.create_task(
                jobs.run_section_scrape(await scheduler_api.get_active_term())
            ))

    yield
    for task in bg_tasks:
        task.cancel()


app = FastAPI(title="BYU Schedule Builder", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(scheduler_api.router)
app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/", response_class=FileResponse)
async def serve_scheduler():
    return FileResponse("frontend/scheduler.html")
