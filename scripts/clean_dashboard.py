"""Back up SQLite, remove rejected records, and audit visible postings once."""
import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
import os

from dotenv import load_dotenv
from backend.database import initialize_database
from backend.job_store import list_jobs, purge_rejected_jobs, mark_job_closed_by_url, mark_job_availability_checked
from agent.posting_extractor import PostingExtractor, PostingExtractionError, ClosedJobPostingError
from agent.search_providers import SearchResult


async def main():
    load_dotenv()
    path = Path(os.getenv('DATABASE_PATH', 'data/jobradar.db'))
    if not path.is_file():
        raise FileNotFoundError(f'Existing JobRadar database not found: {path}')
    backup = Path('private/backups') / f'jobradar-before-cleanup-{datetime.now():%Y%m%d-%H%M%S}.db'
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    await initialize_database(path)
    removed = await purge_rejected_jobs(database_path=path)
    jobs = await list_jobs(us_only=True, database_path=path)
    extractor = PostingExtractor()
    semaphore = asyncio.Semaphore(5)
    counts = {'deleted_rejections': removed, 'checked': 0, 'confirmed_closed': 0, 'unverifiable': 0}
    print(json.dumps({'backup': str(backup), 'visible_to_check': len(jobs), **counts}), flush=True)
    async def check(job):
        async with semaphore:
            try:
                await extractor.fetch(SearchResult(title=job.title, url=job.url, source=job.source))
            except ClosedJobPostingError as error:
                await mark_job_closed_by_url(job.url, str(error), database_path=path)
                counts['confirmed_closed'] += 1
            except PostingExtractionError:
                counts['unverifiable'] += 1
            finally:
                await mark_job_availability_checked(job.id, database_path=path)
                counts['checked'] += 1
                if counts['checked'] % 25 == 0:
                    print(json.dumps(counts), flush=True)
    await asyncio.gather(*(check(job) for job in jobs))
    print(json.dumps(counts), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
