from pathlib import Path
import sys
from datetime import datetime, timedelta
from math import exp

# Allow running this file directly: `python motion/day_summary.py`
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from utilities.motion_db import (
    get_motion_events_by_date,
    get_motion_events_daytime,
    get_motion_event_hourly_avg_all_time,
)


def _gaussian_smooth(values, sigma: float):
    radius = max(1, int(3 * sigma))
    kernel = [exp(-(x * x) / (2 * sigma * sigma)) for x in range(-radius, radius + 1)]
    norm = sum(kernel)
    kernel = [k / norm for k in kernel]

    smoothed = []
    last_index = len(values) - 1
    for i in range(len(values)):
        weighted_sum = 0.0
        for offset, weight in zip(range(-radius, radius + 1), kernel):
            idx = i + offset
            idx = 0 if idx < 0 else (last_index if idx > last_index else idx)
            weighted_sum += values[idx] * weight
        smoothed.append(weighted_sum)
    return smoothed


def get_yesterday_hourly_counts() -> tuple[datetime, list[int]]:
    yesterday = datetime.now() - timedelta(days=1)
    events = get_motion_events_by_date(yesterday)
    counts = [0] * 24
    for event in events:
        counts[event.timestamp.hour] += 1
    return yesterday, counts


def generate_event_vs_time_plot(yesterday: datetime):
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    events = get_motion_events_daytime(yesterday)
    output_dir = Path(__file__).resolve().parent / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"events_vs_time.png"

    start_time = datetime.combine(yesterday.date(), datetime.min.time()).replace(hour=7)
    total_minutes = 16 * 60
    minute_times = [start_time + timedelta(minutes=i) for i in range(total_minutes)]
    minute_counts = [0] * total_minutes

    for event in events:
        delta = int((event.timestamp - start_time).total_seconds() // 60)
        if 0 <= delta < total_minutes:
            minute_counts[delta] += 1

    smooth_counts = _gaussian_smooth(minute_counts, sigma=14)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(minute_times, smooth_counts, linewidth=2.5, color="#0f766e")
    ax.fill_between(minute_times, smooth_counts, color="#14b8a6", alpha=0.18)
    ax.set_title(f"Motion Events vs Time ({yesterday.date()})")
    ax.set_xlabel("Time")
    ax.set_ylabel("Event count")
    ax.grid(alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(output_file, dpi=150)
    plt.close(fig)
    return output_file


def generate_hourly_compare_plot(yesterday: datetime, yesterday_counts: list[int], all_time_avg: list[dict]):
    import matplotlib.pyplot as plt

    output_dir = Path(__file__).resolve().parent / "plots"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"hourly_yesterday_vs_avg.png"

    hours = list(range(24))
    avg_values = [item["avg_per_day"] for item in all_time_avg]

    smooth_yesterday = _gaussian_smooth(yesterday_counts, sigma=1.2)
    smooth_avg = _gaussian_smooth(avg_values, sigma=1.2)

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(hours, smooth_yesterday, linewidth=2.6, color="#1d4ed8", label=f"{yesterday.date()} count")
    ax.plot(hours, smooth_avg, linewidth=2.2, color="#dc2626", label="All-time avg/day")
    ax.fill_between(hours, smooth_yesterday, color="#3b82f6", alpha=0.12)
    ax.fill_between(hours, smooth_avg, color="#ef4444", alpha=0.08)
    ax.set_title("Hourly Motion: Yesterday vs All-Time Avg/Day (Smoothed)")
    ax.set_xlabel("Hour")
    ax.set_ylabel("Event count")
    ax.set_xticks(hours[::2])
    ax.set_xticklabels([f"{h:02d}:00" for h in hours[::2]], rotation=45)
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(output_file, dpi=150)
    plt.close(fig)
    return output_file


def main():
    yesterday, yesterday_counts = get_yesterday_hourly_counts()
    all_time_avg = get_motion_event_hourly_avg_all_time()

    total_yesterday = sum(yesterday_counts)
    avg_total = sum(item["avg_per_day"] for item in all_time_avg)

    print(f"Hourly motion stats for {yesterday.date()} vs all-time daily average")
    print(f"Yesterday total: {total_yesterday}")
    print(f"All-time avg/day: {avg_total:.2f}")
    print("-" * 44)
    print(f"{'Hour':<8}{'Yesterday':>12}{'Avg/Day (All)':>18}")
    print("-" * 44)

    for hour in range(24):
        print(
            f"{hour:02d}:00"
            f"{yesterday_counts[hour]:>12}"
            f"{all_time_avg[hour]['avg_per_day']:>18.2f}"
        )

    events_plot = generate_event_vs_time_plot(yesterday)
    compare_plot = generate_hourly_compare_plot(yesterday, yesterday_counts, all_time_avg)
    print("-" * 44)
    print(f"Saved plot: {events_plot}")
    print(f"Saved plot: {compare_plot}")


if __name__ == "__main__":
    main()
