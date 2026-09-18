"""Thin subprocess wrapper around headless Claude Code (`claude -p`).

Call shape:
    claude -p <prompt> --output-format json --allowedTools Read --max-turns N [--model M]
    cwd = cfg.paths.root; env inherits HOME/PATH so the CLI's login is found.
The CLI prints a JSON object; the model's text is in its "result" key. That text should itself be a
JSON object (prompts always demand this); strip ``` fences and surrounding prose before json.loads.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from kosaccounts.config import ClaudeConfig

logger = logging.getLogger(__name__)


class ClaudeError(RuntimeError):
    """Raised after all retries fail (non-zero exit, timeout, or unparsable JSON)."""


class ClaudeClient:
    def __init__(self, cfg: ClaudeConfig, cwd: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.cwd = cwd if cwd is not None else Path.cwd()

    def run_text(self, prompt: str, files: Iterable[Path] = ()) -> str:
        """Run once (with retries) and return the model's text result. `files` are absolute paths the
        prompt tells the model to Read; they are only appended to the prompt as a list, the CLI is
        never given them directly."""

        # Build full prompt
        files_list = list(files)
        full_prompt = prompt
        if files_list:
            full_prompt += "\n\nFiles to read (absolute paths):\n"
            full_prompt += "\n".join(str(f) for f in files_list)

        # Build argv
        argv = [
            self.cfg.command,
            "-p",
            full_prompt,
            "--output-format",
            "json",
            "--max-turns",
            str(self.cfg.max_turns),
            "--allowedTools",
            ",".join(self.cfg.allowed_tools),
        ]
        if self.cfg.model:
            argv.extend(["--model", self.cfg.model])

        # Retry loop
        last_error = None
        for attempt in range(1 + self.cfg.retries):
            logger.debug(f"Attempt {attempt + 1}/{1 + self.cfg.retries}: running {self.cfg.command}")

            try:
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=self.cfg.timeout_seconds,
                    cwd=str(self.cwd),
                )

                # Check exit code
                if result.returncode != 0:
                    last_error = (result.returncode, result.stderr)
                    logger.debug(f"Attempt {attempt + 1}: non-zero exit {result.returncode}")
                    continue

                # Parse JSON
                try:
                    response = json.loads(result.stdout)
                except json.JSONDecodeError as e:
                    last_error = (result.returncode, result.stderr)
                    logger.debug(f"Attempt {attempt + 1}: JSON decode error: {e}")
                    continue

                # Check for error flag
                if response.get("is_error"):
                    last_error = (result.returncode, result.stderr)
                    logger.debug(f"Attempt {attempt + 1}: is_error flag set")
                    continue

                # Check for result key
                if "result" not in response:
                    last_error = (result.returncode, result.stderr)
                    logger.debug(f"Attempt {attempt + 1}: no 'result' key in response")
                    continue

                # Success
                logger.debug(f"Attempt {attempt + 1}: success")
                return response["result"]

            except subprocess.TimeoutExpired:
                last_error = (None, "timeout")
                logger.debug(f"Attempt {attempt + 1}: timeout after {self.cfg.timeout_seconds}s")
                continue

        # All retries exhausted
        exit_code, stderr = last_error if last_error else (None, "")
        stderr_tail = stderr[-500:] if isinstance(stderr, str) else str(stderr)[-500:]
        msg = f"All {1 + self.cfg.retries} attempts failed"
        if exit_code is not None:
            msg += f" (exit code: {exit_code})"
        msg += f"; stderr: {stderr_tail}"
        raise ClaudeError(msg)

    def run_json(self, prompt: str, files: Iterable[Path] = ()) -> dict:
        """run_text + extract the first JSON object from the result. Raises ClaudeError if none."""

        files_list = list(files)

        for attempt in range(1 + self.cfg.retries):
            try:
                text = self.run_text(prompt, files_list)
                result = extract_json_object(text)
                return result
            except ValueError as e:
                logger.debug(f"Attempt {attempt + 1}: JSON extraction failed: {e}")
                if attempt == self.cfg.retries:
                    raise ClaudeError(f"Failed to extract JSON after {1 + self.cfg.retries} attempts: {e}")
            except ClaudeError:
                raise

        raise ClaudeError("Unexpected end of run_json")


def extract_json_object(text: str) -> dict:
    """Pure helper: find and parse the first top-level {...} in `text`, tolerating ``` fences and
    prose around it. Raises ValueError if nothing parses."""

    # Strip fences
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]

    text = text.strip()

    # Find first "{"
    first_brace = text.find("{")
    if first_brace == -1:
        raise ValueError("No '{' found in text")

    # Find last "}"
    last_brace = text.rfind("}")
    if last_brace == -1 or last_brace <= first_brace:
        raise ValueError("No matching '}' found")

    # Try simple json.loads first
    json_str = text[first_brace:last_brace + 1]
    try:
        result = json.loads(json_str)
        if not isinstance(result, dict):
            raise ValueError(f"JSON result is {type(result).__name__}, not dict")
        return result
    except json.JSONDecodeError:
        pass

    # Try raw_decode from each "{" position
    decoder = json.JSONDecoder()
    pos = first_brace
    while pos <= last_brace:
        brace_pos = text.find("{", pos)
        if brace_pos == -1 or brace_pos > last_brace:
            break
        try:
            result, end_idx = decoder.raw_decode(text, brace_pos)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
        pos = brace_pos + 1

    raise ValueError("No valid JSON object found")
