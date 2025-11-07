from pathlib import Path

directory = Path("data/") 


if directory.exists() and directory.is_dir():
    for file in directory.iterdir():
        if (file.is_file()):
            print(f"[DELETE] Deleting : {file}")
            file.unlink()
else:
    print("[DELETE] Failed Deletion.")