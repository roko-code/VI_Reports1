import json
from pathlib import Path
from datetime import datetime


class TechnicalLogger:
    """Append-only technical log for pipeline stages."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_header_if_new()

    def _write_header_if_new(self):
        if not self.log_path.exists():
            self.log_path.write_text(
                f"# Technical Log\n\n"
                f"Generated: {datetime.now().isoformat()}\n\n"
                f"---\n\n",
                encoding="utf-8",
            )

    def stage_start(self, stage_name: str, details: dict = None):
        self._append(
            f"## {stage_name}\n"
            f"**Started:** {datetime.now().isoformat()}\n"
        )
        if details:
            for key, value in details.items():
                self._append(f"- **{key}:** {value}")
            self._append("")

    def stage_end(self, stage_name: str, success: bool, summary: str = ""):
        status = "SUCCESS" if success else "FAILED"
        self._append(
            f"**Finished:** {datetime.now().isoformat()}  "
            f"**Status:** {status}\n"
        )
        if summary:
            for line in summary.splitlines():
                self._append(f"  {line}")
            self._append("")
        self._append("---\n\n")

    def warning(self, message: str):
        self._append(f"[WARN] {message}")

    def error(self, message: str):
        self._append(f"[ERROR] {message}")

    def info(self, message: str):
        self._append(f"[INFO] {message}")

    def _append(self, text: str):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def write_supporting_json(supporting_dir: Path, filename: str, data) -> Path:
    """Write a JSON file to the stage's supporting_data directory."""
    supporting_dir.mkdir(parents=True, exist_ok=True)
    file_path = supporting_dir / filename
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    return file_path


def write_supporting_text(supporting_dir: Path, filename: str, text: str) -> Path:
    """Write a text file to the stage's supporting_data directory."""
    supporting_dir.mkdir(parents=True, exist_ok=True)
    file_path = supporting_dir / filename
    file_path.write_text(text, encoding="utf-8")
    return file_path
