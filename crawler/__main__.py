import logging
import sys


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)
    cmd = argv[1] if len(argv) > 1 else "crawl"
    if cmd == "crawl":
        from .run import crawl
        return crawl()
    if cmd == "export":
        from . import db
        from .export import export_jobs
        conn = db.connect()
        db.migrate(conn)
        print(f"exported {export_jobs(conn)} rows")
        return 0
    if cmd == "dedupe":
        from . import db
        from .dedupe import run_dedupe
        conn = db.connect()
        print(run_dedupe(conn))
        return 0
    print(f"unknown command {cmd!r}; use crawl | export | dedupe", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
