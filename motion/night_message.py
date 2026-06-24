"""Daily digest: AI-summary + plots pushed to Discord via gRPC."""

from pathlib import Path

from motion.day_summary import main as day_summary_main
from ai.ai import ai_summary
from discord_grpc import send_text, send_image

PLOTS_DIR = Path(__file__).resolve().parent / "plots"


def main():
    stats = day_summary_main()
    message = ai_summary(stats)
    send_text(message, timeout=60.0)

    for file in PLOTS_DIR.iterdir():
        if file.is_file() and file.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            try:
                resp = send_image(file, timeout=120.0)
                if not resp.success:
                    print(f"[DISCORD] failed to send {file.name}: {resp.error}")
            except Exception as exc:
                print(f"[DISCORD] error sending {file.name}: {exc}")


if __name__ == "__main__":
    main()