"""Tests for claude_client.py: subprocess wrapper for claude -p"""

import json
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest

from kosaccounts.claude_client import ClaudeClient, ClaudeError, extract_json_object
from kosaccounts.config import ClaudeConfig


class TestExtractJsonObject:
    """Test extract_json_object helper function."""

    def test_plain_json(self):
        """Extract plain JSON object."""
        text = '{"key": "value", "number": 42}'
        result = extract_json_object(text)
        assert result == {"key": "value", "number": 42}

    def test_fenced_json(self):
        """Extract JSON from ```json fences."""
        text = '```json\n{"key": "value"}\n```'
        result = extract_json_object(text)
        assert result == {"key": "value"}

    def test_fenced_generic(self):
        """Extract JSON from generic ``` fences."""
        text = '```\n{"key": "value"}\n```'
        result = extract_json_object(text)
        assert result == {"key": "value"}

    def test_json_with_prose(self):
        """Extract JSON surrounded by prose."""
        text = 'Here is the JSON: {"key": "value"} And that is it.'
        result = extract_json_object(text)
        assert result == {"key": "value"}

    def test_nested_braces(self):
        """Extract JSON with nested objects."""
        text = '{"outer": {"inner": "value"}, "array": [1, 2, 3]}'
        result = extract_json_object(text)
        assert result == {"outer": {"inner": "value"}, "array": [1, 2, 3]}

    def test_complex_nested(self):
        """Extract complex nested JSON with multiple levels."""
        text = dedent('''\
            Result: ```json
            {
              "data": {
                "nested": {"deep": {"very_deep": "value"}},
                "list": [{"a": 1}, {"b": 2}]
              }
            }
            ```''')
        result = extract_json_object(text)
        assert result == {
            "data": {
                "nested": {"deep": {"very_deep": "value"}},
                "list": [{"a": 1}, {"b": 2}]
            }
        }

    def test_no_json(self):
        """Raise ValueError when no JSON found."""
        text = "Just some text without JSON"
        with pytest.raises(ValueError, match="No '{'"):
            extract_json_object(text)

    def test_no_closing_brace(self):
        """Raise ValueError when closing brace is missing."""
        text = '{"key": "value"'
        with pytest.raises(ValueError, match="No matching '}'"):
            extract_json_object(text)

    def test_json_array_not_object(self):
        """Raise ValueError when JSON is array, not object."""
        text = '[1, 2, 3]'
        with pytest.raises(ValueError):
            extract_json_object(text)

    def test_json_scalar_not_object(self):
        """Raise ValueError when JSON is scalar, not object."""
        text = '"just a string"'
        with pytest.raises(ValueError):
            extract_json_object(text)

    def test_multiple_objects_first_one(self):
        """Extract the first JSON object when multiple exist."""
        text = '{"first": 1} some text {"second": 2}'
        result = extract_json_object(text)
        assert result == {"first": 1}

    def test_with_whitespace(self):
        """Handle text with extra whitespace."""
        text = '  \n  {"key": "value"}  \n  '
        result = extract_json_object(text)
        assert result == {"key": "value"}


class TestClaudeClientFake:
    """Test ClaudeClient with fake command."""

    def _make_fake_command(self, tmp_path, name, python_code):
        """Create an executable Python script that acts as a fake claude command."""
        script = tmp_path / name
        # Write as Python script with shebang
        code = f"#!/usr/bin/env python3\n{dedent(python_code)}"
        script.write_text(code)
        script.chmod(0o755)
        return str(script)

    def test_basic_invocation(self, tmp_path):
        """Test that ClaudeClient builds correct argv and parses result."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            print(json.dumps({"result": "test output"}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="test-model",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read", "Write"]
        )

        client = ClaudeClient(cfg)
        result = client.run_text("test prompt")
        assert result == "test output"

    def test_argv_structure_with_model(self, tmp_path):
        """Test that argv includes model when set."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            import sys
            # Echo sys.argv for verification
            print(json.dumps({"result": json.dumps(sys.argv[1:])}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="test-model",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        result_text = client.run_text("test prompt")
        result_argv = json.loads(result_text)

        # Check that --model is present
        assert "--model" in result_argv
        model_idx = result_argv.index("--model")
        assert result_argv[model_idx + 1] == "test-model"

    def test_argv_no_model_when_empty(self, tmp_path):
        """Test that argv excludes model when cfg.model is empty."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            import sys
            print(json.dumps({"result": json.dumps(sys.argv[1:])}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",  # Empty model
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        result_text = client.run_text("test prompt")
        result_argv = json.loads(result_text)

        # Check that --model is NOT present
        assert "--model" not in result_argv

    def test_allowed_tools_joined(self, tmp_path):
        """Test that allowed_tools are comma-joined."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            import sys
            print(json.dumps({"result": json.dumps(sys.argv[1:])}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read", "Write", "Edit"]
        )

        client = ClaudeClient(cfg)
        result_text = client.run_text("test prompt")
        result_argv = json.loads(result_text)

        # Find --allowedTools
        assert "--allowedTools" in result_argv
        tools_idx = result_argv.index("--allowedTools")
        tools_str = result_argv[tools_idx + 1]
        assert tools_str == "Read,Write,Edit"

    def test_files_appended_to_prompt(self, tmp_path):
        """Test that files are appended to prompt."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            import sys
            # Extract prompt from argv (it's after -p)
            prompt = None
            for i, arg in enumerate(sys.argv[1:]):
                if arg == "-p" and i + 1 < len(sys.argv) - 1:
                    prompt = sys.argv[i + 2]
                    break
            print(json.dumps({"result": prompt if prompt else "no prompt"}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )

        file1 = tmp_path / "test1.txt"
        file2 = tmp_path / "test2.txt"
        file1.write_text("content1")
        file2.write_text("content2")

        client = ClaudeClient(cfg)
        result = client.run_text("original prompt", files=[file1, file2])

        # Check that files are in the result
        assert "Files to read (absolute paths):" in result
        assert str(file1) in result
        assert str(file2) in result

    def test_cwd_parameter(self, tmp_path):
        """Test that cwd parameter is respected."""

        working_dir = tmp_path / "work"
        working_dir.mkdir()

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            import os
            print(json.dumps({"result": os.getcwd()}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg, cwd=working_dir)
        result = client.run_text("test")

        assert str(working_dir) in result

    def test_cwd_default(self, tmp_path):
        """Test that cwd defaults to current directory."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            import os
            print(json.dumps({"result": os.getcwd()}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)  # No cwd specified
        result = client.run_text("test")

        # Should be some valid directory
        assert len(result) > 0


class TestClaudeClientRetry:
    """Test retry behavior."""

    def _make_fake_command(self, tmp_path, name, python_code):
        """Create an executable Python script that acts as a fake claude command."""
        script = tmp_path / name
        code = f"#!/usr/bin/env python3\n{dedent(python_code)}"
        script.write_text(code)
        script.chmod(0o755)
        return str(script)

    def test_single_retry_success_on_second(self, tmp_path):
        """Test that single retry works: fails first, succeeds second."""

        counter_file = tmp_path / "counter.txt"

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", f'''\
            import json
            import sys
            from pathlib import Path

            counter_file = Path({str(counter_file)!r})

            try:
                count = int(counter_file.read_text())
            except:
                count = 0

            count += 1
            counter_file.write_text(str(count))

            if count == 1:
                sys.exit(1)
            else:
                print(json.dumps({{"result": "success"}}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=1,  # One retry
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        result = client.run_text("test")

        assert result == "success"

    def test_retry_exhaustion_raises_error(self, tmp_path):
        """Test that exhausting retries raises ClaudeError."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import sys
            sys.exit(1)
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=0,  # No retries
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        with pytest.raises(ClaudeError):
            client.run_text("test")

    def test_timeout_raises_error(self, tmp_path):
        """Test that timeout raises ClaudeError."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import time
            time.sleep(10)
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=1,  # 1 second timeout
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        with pytest.raises(ClaudeError):
            client.run_text("test")

    def test_missing_result_key_retries(self, tmp_path):
        """Test that missing 'result' key in response triggers retry."""

        counter_file = tmp_path / "counter.txt"

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", f'''\
            import json
            import sys
            from pathlib import Path

            counter_file = Path({str(counter_file)!r})

            try:
                count = int(counter_file.read_text())
            except:
                count = 0

            count += 1
            counter_file.write_text(str(count))

            if count == 1:
                # First attempt: missing 'result' key
                print(json.dumps({{"other_key": "value"}}))
            else:
                # Second attempt: succeed
                print(json.dumps({{"result": "success"}}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=1,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        result = client.run_text("test")

        assert result == "success"

    def test_is_error_flag_retries(self, tmp_path):
        """Test that is_error flag triggers retry."""

        counter_file = tmp_path / "counter.txt"

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", f'''\
            import json
            import sys
            from pathlib import Path

            counter_file = Path({str(counter_file)!r})

            try:
                count = int(counter_file.read_text())
            except:
                count = 0

            count += 1
            counter_file.write_text(str(count))

            if count == 1:
                # First attempt: error flag
                print(json.dumps({{"result": "error", "is_error": True}}))
            else:
                # Second attempt: succeed
                print(json.dumps({{"result": "success"}}))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=1,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        result = client.run_text("test")

        assert result == "success"


class TestRunJson:
    """Test run_json method."""

    def _make_fake_command(self, tmp_path, name, python_code):
        """Create an executable Python script that acts as a fake claude command."""
        script = tmp_path / name
        code = f"#!/usr/bin/env python3\n{dedent(python_code)}"
        script.write_text(code)
        script.chmod(0o755)
        return str(script)

    def test_basic_json_extraction(self, tmp_path):
        """Test that run_json extracts JSON from result."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            result = {
                "result": '{"key": "value", "number": 42}'
            }
            print(json.dumps(result))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        result = client.run_json("test")

        assert result == {"key": "value", "number": 42}

    def test_json_with_fences(self, tmp_path):
        """Test that run_json handles fenced JSON."""
        
        fake_claude = self._make_fake_command(tmp_path, "fake_claude", """
import json
# Return JSON with fenced content - construct it carefully
inner_json = json.dumps({"key": "value"})
fenced = '```json' + chr(10) + inner_json + chr(10) + '```'
result = {
    "result": fenced
}
print(json.dumps(result))
""")
        
        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )
        
        client = ClaudeClient(cfg)
        result = client.run_json("test")
        
        assert result == {"key": "value"}


    def test_bad_json_raises_error(self, tmp_path):
        """Test that bad JSON raises ClaudeError."""

        fake_claude = self._make_fake_command(tmp_path, "fake_claude", '''\
            import json
            result = {
                "result": "This is not JSON {invalid}"
            }
            print(json.dumps(result))
            ''')

        cfg = ClaudeConfig(
            command=fake_claude,
            model="",
            timeout_seconds=5,
            max_turns=2,
            retries=0,
            allowed_tools=["Read"]
        )

        client = ClaudeClient(cfg)
        with pytest.raises(ClaudeError):
            client.run_json("test")
