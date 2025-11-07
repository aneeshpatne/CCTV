from pathlib import Path
import logging

logging.basicConfig(level=logging.INFO)


logging.info("Program started")
directory = Path("data/") 


if directory.exists() and directory.is_dir():
    for file in directory.iterdir():
        if (file.is_file()):
            try:
                logging.info(f"[DELETE] Program started {file}")
                file.unlink()
            except Exception as e:
                logging.error(f"[DELETE] Failed to delete {file}: {e}")
else:
    logging.info("[DELETE] Directory Does not exist")