"""
MCP Server for Arduino CLI, File Management, and WireViz using FastMCP (v2.3 - Auto Open Files)

Provides tools for managing Arduino projects via the `arduino-cli`, generating
circuit diagrams using WireViz, and basic file operations.

Core Features:
- Sketch Management: Create, list, read, and write Arduino sketches.
  - **New:** Automatically opens the created `.ino` file in the default editor after `create_new_sketch`.
  - Writing `.ino` files within the designated sketch directory automatically
    triggers compilation using a specified or default FQBN.
- Code Verification: Compile sketches without uploading using `verify_code`.
- Uploading: Compile and upload sketches to connected boards.
- Library Management: Search the online index and local platform libraries,
  install libraries, and list examples from installed libraries.
- Board Management: Discover connected boards and search the online index.
- File Operations: Basic, restricted file renaming and removal within the user's
  home directory.
- WireViz Integration:
    - `get_wireviz_instructions`: Provides basic usage instructions for WireViz.
    - `generate_diagram_from_yaml`: Generates circuit diagrams from YAML.
      - **New:** Automatically opens the generated `.png` file in the default image viewer.

Key Enhancements in this Version (v2.3):
- **Auto-Open Functionality:** Added helpers to open created/generated files
  (`.ino`, `.png`) in their default system applications after successful tool execution.
- Split WireViz functionality into two tools: one for instructions, one for generation.
- Added `get_wireviz_instructions` tool (now a resource).
- Returns generated PNG image data directly via MCP ImageContent in `generate_diagram_from_yaml`.
- Improved Readability: Enhanced docstrings, type hints, logging, and code structure.
- Robust Error Handling: More specific exceptions, clearer error messages,
  better handling of CLI command failures and path issues.
- Refined Path Validation: Stricter checks for file operations to prevent
  access outside allowed directories (User Home or specific Arduino paths).
- Consistent Logging: Standardized log message formats and levels.
- Dependency Checks: Clearer messages if dependencies like `mcp` or `thefuzz`
  are missing.
- Security Emphasis: Stronger warnings in docstrings for potentially
  destructive operations (`write_file`, `rename_file`, `remove_file`).
- ANSI Code Stripping: Fixed potential issues in parsing `lib examples` output.
- Auto-Compile Logic: Clarified FQBN handling for `write_file` auto-compile.

Configuration Defaults:
- Sketches Base Directory: ~/Documents/Arduino_MCP_Sketches/
- Build Temp Directory: ~/Documents/Arduino_MCP_Sketches/_build_temp/
- Arduino Data Directory: Auto-detected (~/.arduino15 or ~/Library/Arduino15)
- Arduino User Directory: ~/Documents/Arduino/
- Arduino CLI Path: Auto-detected via `shutil.which` and common paths,
  defaults to 'arduino-cli'. Can be overridden via ARDUINO_CLI_PATH env var.
- WireViz Path: Auto-detected via `shutil.which`, defaults to 'wireviz'.
  Can be overridden via WIREVIZ_PATH env var.
- Default FQBN (for auto-compile): 'arduino:avr:uno'

Prerequisites:
- Python 3.8+
- `arduino-cli` installed and accessible in the system PATH or common locations.
- `wireviz` installed and accessible in the system PATH.
- MCP SDK: `pip install "mcp[cli]"`
- Fuzzy Search (Optional but Recommended): `pip install "thefuzz[speedup]"`

Debugging Tips:
- Check Server Logs: Detailed errors from `arduino-cli` and `wireviz` are logged.
- Permissions: Ensure user has access to sketch/data/user/build directories,
  serial ports, and can execute `wireviz`.
- Environment PATH: Verify `arduino-cli`, `wireviz`, and required toolchains
  are in the PATH accessible by the server process.
- Cores/Toolchains: Use `arduino-cli core install <core_spec>` if compilation fails.
- "No such file or directory": Check paths, missing core tools, or sketch paths.
"""

import asyncio
import base64 # Added for WireViz image encoding
import datetime # Added for WireViz timestamped directories
import uuid
import json
import logging
import os
import re
import shutil  # Used for finding executable and file operations
import subprocess
import threading
import sys # Added for exit calls and platform detection
import openai  # For GPT-4.1 API calls

# --- OpenAI API Key Management ---
_OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", None)

def set_openai_api_key(key: str):
    global _OPENAI_API_KEY
    _OPENAI_API_KEY = key
    os.environ["OPENAI_API_KEY"] = key
    log.info("OpenAI API key set.")
    return "OpenAI API key set."

from contextlib import asynccontextmanager
from pathlib import Path
from typing import (Any, AsyncIterator, Deque, Dict, List, Optional, Set, Tuple,
                    Union)
from collections import deque
from dataclasses import dataclass, field

# --- MCP Imports ---
try:
    from mcp.server.fastmcp import Context, FastMCP, Image # Added Image import
    import mcp.types as types # Added for WireViz return type
except ImportError:
    sys.stderr.write(
        "CRITICAL: MCP SDK (FastMCP) not found. "
        "Please install it: pip install \"mcp[cli]\"\n"
    )
    sys.exit(1)
# --- End MCP Imports ---

# --- Logging Configuration ---
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format='%(asctime)s - %(levelname)-8s - %(name)-18s - [%(funcName)s:%(lineno)d] %(message)s'
)
log = logging.getLogger("ArduinoWireVizMCPServer") # Updated logger name
# --- End Logging Configuration ---

# --- Fuzzy Search Import ---
try:
    from thefuzz import fuzz
    try:
        import Levenshtein  # noqa: F401
        FUZZY_ENABLED = True
        log.debug("Using thefuzz with python-Levenshtein for faster fuzzy search.")
    except ImportError:
        FUZZY_ENABLED = True
        log.warning(
            "Using thefuzz for fuzzy search. "
            "Install 'python-Levenshtein' for optimal performance: "
            "pip install python-Levenshtein"
        )
except ImportError:
    FUZZY_ENABLED = False
    log.warning(
        "Fuzzy search library 'thefuzz' not found. Fuzzy search in "
        "lib_search fallback disabled. Install it: pip install \"thefuzz[speedup]\""
    )
# --- End Fuzzy Search Import ---


# ==============================================================================
# Configuration & Constants
# ==============================================================================
USER_HOME: Path = Path.home()
SKETCHES_BASE_DIR: Path = USER_HOME / "Documents" / "Arduino_MCP_Sketches"
BUILD_TEMP_DIR: Path = SKETCHES_BASE_DIR / "_build_temp"
FUZZY_SEARCH_THRESHOLD: int = 75  # Minimum score (0-100) for fuzzy matches
DEFAULT_FQBN: str = "arduino:avr:uno" # Default FQBN for write_file auto-compile
SERIAL_LOG_DIR: Path = SKETCHES_BASE_DIR / "_serial_logs"
SERIAL_DEFAULT_BAUD: int = 115200
SERIAL_DEFAULT_BUFFER_LINES: int = 2000
SERIAL_DEFAULT_LOG_MAX_BYTES: int = 5 * 1024 * 1024
SERIAL_DEFAULT_LOG_ROTATE_COUNT: int = 3
SERIAL_RECONNECT_INITIAL_DELAY: float = 1.0
SERIAL_RECONNECT_MAX_DELAY: float = 5.0

def _env_int(name: str, default: int, allow_zero: bool = True) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning(f"Invalid integer for {name}='{raw}', using default {default}.")
        return default
    if value < 0 or (value == 0 and not allow_zero):
        log.warning(f"Out of range value for {name}='{raw}', using default {default}.")
        return default
    return value

# Env overrides for serial log defaults
SERIAL_DEFAULT_LOG_MAX_BYTES = _env_int(
    "ARDUINO_SERIAL_LOG_MAX_BYTES",
    SERIAL_DEFAULT_LOG_MAX_BYTES,
    allow_zero=True
)
SERIAL_DEFAULT_LOG_ROTATE_COUNT = _env_int(
    "ARDUINO_SERIAL_LOG_ROTATE_COUNT",
    SERIAL_DEFAULT_LOG_ROTATE_COUNT,
    allow_zero=True
)

# --- Arduino Directories Detection ---
ARDUINO_DATA_DIR: Path
ARDUINO_USER_DIR: Path = USER_HOME / "Documents" / "Arduino" # Standard user dir

if os.name == 'nt':  # Windows
    ARDUINO_DATA_DIR = USER_HOME / "AppData" / "Local" / "Arduino15"
else:  # macOS, Linux
    # Default Linux/other path
    ARDUINO_DATA_DIR = USER_HOME / ".arduino15"
    # Check for standard macOS path
    macos_data_dir = USER_HOME / "Library" / "Arduino15"
    if macos_data_dir.is_dir():
        ARDUINO_DATA_DIR = macos_data_dir
        log.debug(f"Detected macOS Arduino data directory: {ARDUINO_DATA_DIR}")
    elif not ARDUINO_DATA_DIR.is_dir():
         log.warning(f"Default Arduino data directory ({ARDUINO_DATA_DIR}) not found. "
                     "arduino-cli might use a different location or need initialization.")

# --- Arduino CLI Path Detection ---
_cli_path_override = os.environ.get("ARDUINO_CLI_PATH")
if _cli_path_override:
    ARDUINO_CLI_PATH = _cli_path_override
    log.info(f"Using arduino-cli path from environment variable: {ARDUINO_CLI_PATH}")
else:
    ARDUINO_CLI_PATH = "arduino-cli" # Default if not found
    _cli_found_path = shutil.which("arduino-cli")

    if _cli_found_path:
        ARDUINO_CLI_PATH = _cli_found_path
        log.debug(f"Found arduino-cli via shutil.which: {ARDUINO_CLI_PATH}")
    else:
        log.debug("arduino-cli not found via shutil.which, checking common paths...")
        # Common paths to check if 'which' fails
        common_paths_to_check = [
            "/opt/homebrew/bin/arduino-cli",    # macOS Homebrew (Apple Silicon)
            "/usr/local/bin/arduino-cli",       # macOS Homebrew (Intel), Linux manual install
            "/usr/bin/arduino-cli",             # Linux package manager
            str(USER_HOME / "bin" / "arduino-cli"), # User bin directory
            # Add platform-specific paths if needed (e.g., Windows Program Files)
        ]
        for path_str in common_paths_to_check:
            path = Path(path_str)
            if path.is_file() and os.access(path, os.X_OK):
                ARDUINO_CLI_PATH = str(path)
                log.debug(f"Found arduino-cli in common path: {ARDUINO_CLI_PATH}")
                break
        else: # Use default if not found in common paths either
             log.warning(
                 f"arduino-cli not found via 'which' or common paths. Using default "
                 f"'{ARDUINO_CLI_PATH}'. Ensure it's installed and accessible in PATH."
             )

# ==============================================================================
# Serial Monitor State
# ==============================================================================

@dataclass
class SerialMonitorState:
    monitor_id: str
    port: str
    baud: int
    board_fqbn: Optional[str]
    protocol: Optional[str]
    auto_reconnect: bool
    auto_reconnect_user: bool
    reconnect_delay: float
    stop_requested: bool
    buffer_max: int
    buffer: Deque[str]
    log_path: Optional[Path]
    log_max_bytes: int
    log_rotate_count: int
    process: Optional[subprocess.Popen] = None
    reader_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    tasks: List[asyncio.Task] = field(default_factory=list)
    running: bool = False
    started_at: Optional[datetime.datetime] = None
    last_line_at: Optional[datetime.datetime] = None


_SERIAL_MONITORS: Dict[str, SerialMonitorState] = {}
_SERIAL_MONITORS_LOCK = asyncio.Lock()

# --- WireViz Path Detection ---
_wireviz_path_override = os.environ.get("WIREVIZ_PATH")
if _wireviz_path_override:
    WIREVIZ_PATH = _wireviz_path_override
    log.info(f"Using wireviz path from environment variable: {WIREVIZ_PATH}")
else:
    WIREVIZ_PATH = "wireviz" # Default if not found
    _wireviz_found_path = shutil.which("wireviz")
    if _wireviz_found_path:
        WIREVIZ_PATH = _wireviz_found_path
        log.debug(f"Found wireviz via shutil.which: {WIREVIZ_PATH}")
    else:
        log.warning(
            f"wireviz not found via 'which'. Using default '{WIREVIZ_PATH}'. "
            "Ensure it's installed and accessible in PATH for diagram generation."
        )

log.info(f"Configuration Loaded:")
log.info(f"  - Sketch Base Dir: {SKETCHES_BASE_DIR}")
log.info(f"  - Build Temp Dir : {BUILD_TEMP_DIR}")
log.info(f"  - Arduino Data Dir: {ARDUINO_DATA_DIR}")
log.info(f"  - Arduino User Dir: {ARDUINO_USER_DIR}")
log.info(f"  - Arduino CLI Path: {ARDUINO_CLI_PATH}")
log.info(f"  - WireViz Path     : {WIREVIZ_PATH}") # Added WireViz path
log.info(f"  - Default FQBN   : {DEFAULT_FQBN}")
log.info(f"  - Fuzzy Search   : {'Enabled' if FUZZY_ENABLED else 'Disabled'}")

# --- Ensure Core Directories Exist ---
try:
    SKETCHES_BASE_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # Don't create data/user dirs, just warn if missing
    if not ARDUINO_DATA_DIR.is_dir():
        log.warning(f"Arduino data directory does not exist: {ARDUINO_DATA_DIR}. "
                    "CLI might auto-create or fail if config is needed.")
    if not ARDUINO_USER_DIR.is_dir():
        log.warning(f"Arduino user directory does not exist: {ARDUINO_USER_DIR}. "
                    "CLI might auto-create or fail if libraries are needed.")
    log.debug("Ensured sketch and build directories exist.")
except OSError as e:
    log.error(f"Failed to create required sketch/build directories. Check permissions. Error: {e}")
    # Consider exiting if essential dirs can't be made, but warning might be okay
# --- End Core Directory Setup ---

# --- ANSI Escape Code Regex ---
ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
# ---

# ==============================================================================
# MCP Server Initialization
# ==============================================================================
mcp = FastMCP(
    "Arduino & WireViz Tools", # Updated name
    description=(
        "Tools for managing Arduino sketches (create, list, read, write with auto-compile), "
        "code verification (compile), uploading, libraries (search, install, examples), "
        "board discovery via arduino-cli, basic file operations, and generating "
        "circuit diagrams from WireViz YAML." # Updated description
    ),
    dependencies=["mcp[cli]", "thefuzz[speedup]", "wireviz"] # Added wireviz dependency info
)
# ==============================================================================
# Helper Functions
# ==============================================================================

async def _run_cli_command(
    executable_path: str,
    cmd_args: List[str],
    check: bool = True,
    cwd: Optional[Path] = None,
    env_extras: Optional[Dict[str, str]] = None,
    log_prefix: str = "CLI"
) -> Tuple[str, str, int]:
    """
    Generic helper to run external CLI commands asynchronously.

    Manages environment variables, handles stdout, stderr, return code,
    and raises specific exceptions on failure if check=True.

    Args:
        executable_path: Path to the executable (e.g., ARDUINO_CLI_PATH, WIREVIZ_PATH).
        cmd_args: List of arguments to pass to the executable.
        check: If True, raise an Exception if the command returns a non-zero exit code.
        cwd: Optional Path object for the working directory.
        env_extras: Optional dictionary of extra environment variables to set.
        log_prefix: Prefix for log messages (e.g., "arduino-cli", "wireviz").

    Returns:
        Tuple containing (stdout string, stderr string, return code).

    Raises:
        FileNotFoundError: If executable_path is not found or if the command output
                           indicates a missing file/library needed by the CLI itself.
        PermissionError: If execution permission is denied.
        ConnectionError: If upload fails due to port communication issues (specific to arduino-cli).
        TimeoutError: If upload times out (specific to arduino-cli).
        Exception: For other command failures when check=True.
    """
    full_cmd = [executable_path] + cmd_args
    env = os.environ.copy()
    if env_extras:
        env.update(env_extras)

    effective_cwd_str = str(cwd.resolve()) if cwd else None
    cmd_str_for_log = ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in full_cmd) # Safer logging

    log.debug(f"Running {log_prefix} Command : {cmd_str_for_log}")
    log.debug(f"  Environment Extras: {env_extras or '{}'}")
    if effective_cwd_str:
        log.debug(f"  Working Dir       : {effective_cwd_str}")

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=effective_cwd_str,
            limit=1024 * 1024 * 10 # Set a buffer limit (e.g., 10MB)
        )
        stdout, stderr = await process.communicate()

        stdout_str = stdout.decode(errors='replace').strip() if stdout else ""
        stderr_str = stderr.decode(errors='replace').strip() if stderr else ""
        return_code = process.returncode if process and process.returncode is not None else -1

        log_level = logging.DEBUG if return_code == 0 else logging.WARNING
        log.log(log_level, f"{log_prefix} command finished: Code={return_code}, Cmd='{cmd_str_for_log}'")
        if stdout_str:
            log.debug(f"{log_prefix} STDOUT:\n---\n{stdout_str}\n---")
        if stderr_str:
            stderr_log_level = logging.WARNING if return_code != 0 else logging.DEBUG
            log.log(stderr_log_level, f"{log_prefix} STDERR:\n---\n{stderr_str}\n---")

        if check and return_code != 0:
            error_message = stderr_str if stderr_str else stdout_str
            if not error_message: error_message = f"{log_prefix} command failed with exit code {return_code} but produced no output."
            log.error(f"{log_prefix} command failed! Code: {return_code}. Error: {error_message}")

            # Raise specific errors based on common stderr messages
            error_lower = error_message.lower()
            if "no such file or directory" in error_lower:
                 if "bossac" in error_lower or "esptool" in error_lower or "dfu-util" in error_lower:
                     raise FileNotFoundError(f"Required uploader tool not found. Ensure platform core is installed ('arduino-cli core install ...'). Error: {error_message}")
                 else:
                     raise FileNotFoundError(f"File or directory not found. Check paths or required build artifacts. Error: {error_message}")
            if "library not found" in error_lower:
                 raise FileNotFoundError(f"Library not found. Use 'lib_search' and 'lib_install'. Error: {error_message}")
            if "permission denied" in error_lower or "access is denied" in error_lower:
                 raise PermissionError(f"Permission denied. Check user rights for files/ports. Error: {error_message}")
            if "no device found" in error_lower or "can't open device" in error_lower or "serial port not found" in error_lower:
                 raise ConnectionError(f"Device/port not found or cannot be opened. Check connection. Error: {error_message}")
            if "programmer is not responding" in error_lower or "timed out" in error_lower or "error resetting" in error_lower or "not in sync" in error_lower:
                 raise TimeoutError(f"Communication error with board. Check connection/board state. Error: {error_message}")

            # Generic exception for other failures
            raise Exception(f"{log_prefix} command failed (code {return_code}): {error_message}")

        return stdout_str, stderr_str, return_code

    except FileNotFoundError as e:
        if executable_path in str(e):
            error_msg = f"Command '{executable_path}' not found. Is {log_prefix} installed and in PATH or correctly detected?"
            log.error(error_msg)
            raise FileNotFoundError(error_msg) from e
        else:
            log.error(f"{log_prefix} command failed due to missing file/resource: {e}")
            raise e
    except PermissionError as e:
         log.error(f"{log_prefix} command failed due to permissions: {e}")
         raise e
    except ConnectionError as e: # Specific to arduino-cli upload
         log.error(f"{log_prefix} command failed due to connection error: {e}")
         raise e
    except TimeoutError as e: # Specific to arduino-cli upload
         log.error(f"{log_prefix} command failed due to timeout/communication error: {e}")
         raise e
    except Exception as e:
        if isinstance(e, Exception) and e.args and f"{log_prefix} command failed" in str(e.args[0]):
            log.error(f"Caught specific {log_prefix} command failure: {e}")
            raise e
        error_msg = f"Unexpected error running {log_prefix} command '{cmd_str_for_log}': {type(e).__name__}: {e}"
        log.exception(error_msg)
        raise Exception(error_msg) from e
    finally:
        if process and process.returncode is None:
            log.warning(f"{log_prefix} command process '{cmd_str_for_log}' did not exit cleanly, attempting termination.")
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                log.error(f"Process '{cmd_str_for_log}' did not terminate after 2s, sending kill signal.")
                process.kill()
            except ProcessLookupError:
                log.debug("Process already terminated.")
            except Exception as term_err:
                log.error(f"Error during process termination: {term_err}")

async def _run_arduino_cli_command(
    cmd_args: List[str],
    check: bool = True,
    cwd: Optional[Path] = None
) -> Tuple[str, str, int]:
    """Runs arduino-cli commands using the generic helper."""
    env_extras = {
        "ARDUINO_DIRECTORIES_DATA": str(ARDUINO_DATA_DIR.resolve()),
        "ARDUINO_DIRECTORIES_USER": str(ARDUINO_USER_DIR.resolve()),
        'TMPDIR': str(BUILD_TEMP_DIR.resolve()),
        'HOME': str(USER_HOME.resolve())
    }
    return await _run_cli_command(
        ARDUINO_CLI_PATH, cmd_args, check, cwd, env_extras, log_prefix="arduino-cli"
    )

async def _run_wireviz_command(
    yaml_filepath: Path,
    check: bool = True
) -> Tuple[str, str, int]:
    """Runs wireviz commands using the generic helper."""
    # Wireviz typically runs in the directory containing the YAML file
    cwd = yaml_filepath.parent
    cmd_args = [str(yaml_filepath)] # Wireviz takes the YAML file as the main argument
    return await _run_cli_command(
        WIREVIZ_PATH, cmd_args, check, cwd, log_prefix="wireviz"
    )

# ==============================================================================
# Serial Monitor Helpers
# ==============================================================================

def _sanitize_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)

def _default_serial_log_path(port: str) -> Path:
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_port = _sanitize_filename(port)
    return SERIAL_LOG_DIR / f"{safe_port}_{timestamp}.log"

def _rotate_log_path(base_path: Path, index: int) -> Path:
    return base_path.with_suffix(base_path.suffix + f".{index}")

def _sync_rotate_log_if_needed(filepath: Path, max_bytes: int, rotate_count: int) -> None:
    if max_bytes <= 0:
        return
    if not filepath.exists():
        return
    try:
        size = filepath.stat().st_size
    except OSError:
        return
    if size < max_bytes:
        return

    if rotate_count <= 0:
        filepath.write_text("", encoding="utf-8")
        return

    for i in range(rotate_count, 1, -1):
        src = _rotate_log_path(filepath, i - 1)
        dst = _rotate_log_path(filepath, i)
        if src.exists():
            src.replace(dst)
    filepath.replace(_rotate_log_path(filepath, 1))

def _sync_append_line_with_rotation(
    filepath: Path,
    line: str,
    max_bytes: int,
    rotate_count: int,
    encoding: str = "utf-8"
) -> None:
    filepath.parent.mkdir(parents=True, exist_ok=True)
    _sync_rotate_log_if_needed(filepath, max_bytes, rotate_count)
    with filepath.open("a", encoding=encoding) as handle:
        handle.write(line + "\n")

def _build_serial_monitor_cmd_args(state: SerialMonitorState) -> List[str]:
    args = [
        "monitor",
        "-p", state.port,
        "--config", f"baudrate={state.baud}",
        "--quiet",
        "--no-color",
    ]
    if state.board_fqbn:
        args += ["-b", state.board_fqbn]
    if state.protocol:
        args += ["-l", state.protocol]
    return args

# Windows + asyncio subprocess pipes can starve the event loop; read in a thread.
def _serial_monitor_reader_thread(state: SerialMonitorState) -> None:
    if not state.process or not state.process.stdout:
        return
    while not state.stop_event.is_set():
        try:
            chunk = state.process.stdout.readline()
        except Exception as e:
            log.warning(f"Serial monitor reader error ({state.monitor_id}): {e}")
            break
        if not chunk:
            break
        line = chunk.rstrip("\r\n")
        state.last_line_at = datetime.datetime.now()
        if line != "":
            state.buffer.append(line)
            if state.log_path:
                _sync_append_line_with_rotation(
                    state.log_path,
                    line,
                    state.log_max_bytes,
                    state.log_rotate_count,
                )
        else:
            state.buffer.append("")
            if state.log_path:
                _sync_append_line_with_rotation(
                    state.log_path,
                    "",
                    state.log_max_bytes,
                    state.log_rotate_count,
                )

async def _serial_monitor_waiter(state: SerialMonitorState) -> None:
    if not state.process:
        return
    while True:
        try:
            await asyncio.to_thread(state.process.wait)
        except Exception as e:
            log.warning(f"Serial monitor process wait error ({state.monitor_id}): {e}")
        state.running = False
        state.stop_event.set()
        if state.reader_thread and state.reader_thread.is_alive():
            try:
                await asyncio.to_thread(state.reader_thread.join, 1.0)
            except Exception:
                pass
        state.reader_thread = None
        if state.stop_requested or not state.auto_reconnect:
            break

        delay = max(state.reconnect_delay, SERIAL_RECONNECT_INITIAL_DELAY)
        log.info(f"Serial monitor reconnecting ({state.monitor_id}) in {delay:.1f}s...")
        await asyncio.sleep(delay)
        if state.stop_requested or not state.auto_reconnect:
            break
        try:
            await _start_serial_monitor_state(state)
            state.reconnect_delay = SERIAL_RECONNECT_INITIAL_DELAY
            break
        except Exception as e:
            log.warning(f"Serial monitor reconnect failed ({state.monitor_id}): {e}")
            state.reconnect_delay = min(delay * 2.0, SERIAL_RECONNECT_MAX_DELAY)

async def _start_serial_monitor_state(state: SerialMonitorState) -> None:
    cmd_args = _build_serial_monitor_cmd_args(state)
    env = os.environ.copy()
    env.update({
        "ARDUINO_DIRECTORIES_DATA": str(ARDUINO_DATA_DIR.resolve()),
        "ARDUINO_DIRECTORIES_USER": str(ARDUINO_USER_DIR.resolve()),
        "TMPDIR": str(BUILD_TEMP_DIR.resolve()),
        "HOME": str(USER_HOME.resolve()),
    })
    cmd_str_for_log = " ".join(f'"{arg}"' if " " in arg else arg for arg in [ARDUINO_CLI_PATH] + cmd_args)
    log.info(f"Starting serial monitor ({state.monitor_id}): {cmd_str_for_log}")
    process = subprocess.Popen(
        [ARDUINO_CLI_PATH, *cmd_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    state.process = process
    state.running = True
    state.started_at = datetime.datetime.now()
    state.stop_requested = False
    state.stop_event.clear()
    if state.reconnect_delay <= 0:
        state.reconnect_delay = SERIAL_RECONNECT_INITIAL_DELAY
    state.tasks = [
        asyncio.create_task(_serial_monitor_waiter(state)),
    ]
    state.reader_thread = threading.Thread(
        target=_serial_monitor_reader_thread,
        args=(state,),
        daemon=True,
        name=f"mcp-serial-reader-{state.monitor_id}",
    )
    state.reader_thread.start()

async def _detached_process_wait_popen(proc: subprocess.Popen, timeout_sec: float) -> None:
    def _wait() -> None:
        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        except Exception:
            pass

    await asyncio.to_thread(_wait)

async def _stop_serial_monitor_state(
    state: SerialMonitorState,
    remove: bool = False,
    force: bool = False,
    timeout_sec: float = 2.0,
    wait_tasks: bool = True,
) -> None:
    state.stop_requested = True
    state.auto_reconnect = False
    state.stop_event.set()
    if state.process and state.process.poll() is None:
        try:
            if force:
                state.process.kill()
            else:
                state.process.terminate()
            if wait_tasks:
                try:
                    await asyncio.to_thread(state.process.wait, timeout_sec)
                except subprocess.TimeoutExpired:
                    log.warning(f"Serial monitor terminate timeout ({state.monitor_id}); killing process.")
                    state.process.kill()
            else:
                asyncio.create_task(_detached_process_wait_popen(state.process, timeout_sec))
        except ProcessLookupError:
            pass
        except Exception as e:
            log.warning(f"Serial monitor terminate error ({state.monitor_id}): {e}")

    for task in state.tasks:
        task.cancel()
    if wait_tasks:
        for task in state.tasks:
            try:
                await asyncio.wait_for(task, timeout=timeout_sec)
            except asyncio.TimeoutError:
                log.warning(f"Serial monitor task timeout ({state.monitor_id}); skipping.")
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
    state.tasks = []
    if state.reader_thread and state.reader_thread.is_alive():
        if wait_tasks:
            try:
                await asyncio.to_thread(state.reader_thread.join, timeout_sec)
            except Exception:
                pass
    state.reader_thread = None
    state.running = False
    state.process = None

    if remove:
        async with _SERIAL_MONITORS_LOCK:
            _SERIAL_MONITORS.pop(state.monitor_id, None)

async def _create_serial_monitor_state(
    port: str,
    baud: int,
    buffer_lines: int,
    log_path: Optional[Path],
    board_fqbn: Optional[str],
    protocol: Optional[str],
    auto_reconnect: bool,
    log_max_bytes: int,
    log_rotate_count: int,
) -> SerialMonitorState:
    monitor_id = uuid.uuid4().hex[:8]
    buffer: Deque[str] = deque(maxlen=buffer_lines)
    state = SerialMonitorState(
        monitor_id=monitor_id,
        port=port,
        baud=baud,
        board_fqbn=board_fqbn,
        protocol=protocol,
        auto_reconnect=auto_reconnect,
        auto_reconnect_user=auto_reconnect,
        reconnect_delay=SERIAL_RECONNECT_INITIAL_DELAY,
        stop_requested=False,
        buffer_max=buffer_lines,
        buffer=buffer,
        log_path=log_path,
        log_max_bytes=log_max_bytes,
        log_rotate_count=log_rotate_count,
    )
    async with _SERIAL_MONITORS_LOCK:
        _SERIAL_MONITORS[monitor_id] = state
    try:
        await _start_serial_monitor_state(state)
        return state
    except Exception:
        async with _SERIAL_MONITORS_LOCK:
            _SERIAL_MONITORS.pop(monitor_id, None)
        raise

async def _stop_monitors_for_port(port: str, force: bool = False, wait_tasks: bool = False) -> None:
    to_stop: List[SerialMonitorState] = []
    async with _SERIAL_MONITORS_LOCK:
        for state in _SERIAL_MONITORS.values():
            if state.port == port:
                to_stop.append(state)
    for state in to_stop:
        await _stop_serial_monitor_state(state, remove=True, force=force, wait_tasks=wait_tasks)

async def _pause_monitors_for_port(port: str, force: bool = False, wait_tasks: bool = False) -> List[SerialMonitorState]:
    paused: List[SerialMonitorState] = []
    async with _SERIAL_MONITORS_LOCK:
        for state in _SERIAL_MONITORS.values():
            if state.port == port and state.running:
                paused.append(state)
    for state in paused:
        await _stop_serial_monitor_state(state, remove=False, force=force, wait_tasks=wait_tasks)
    return paused

async def _resume_monitors(states: List[SerialMonitorState]) -> List[str]:
    restarted: List[str] = []
    for state in states:
        if not state.running:
            try:
                state.auto_reconnect = state.auto_reconnect_user
                await _start_serial_monitor_state(state)
                restarted.append(state.monitor_id)
            except Exception as e:
                log.warning(f"Failed to restart serial monitor ({state.monitor_id}): {e}")
    return restarted
# --- End Serial Monitor Helpers ---

# --- Synchronous File I/O Helpers (Run in Executor Thread) ---
# These helpers ensure blocking file I/O doesn't block the asyncio event loop.

async def _async_file_op(func, *args, **kwargs):
    """Runs a synchronous function in a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args, **kwargs)

def _sync_write_file(filepath: Path, content: str, encoding: str = "utf-8"):
    """Synchronously writes content to a file."""
    log.debug(f"Executing sync write to: {filepath}")
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding=encoding)
        log.debug(f"Sync write successful: {filepath}")
    except OSError as e:
        log.error(f"Sync write failed for {filepath}: {e}")
        raise # Re-raise to be caught by the async wrapper

def _sync_read_file(filepath: Path, encoding: str = "utf-8") -> str:
    """Synchronously reads content from a file."""
    log.debug(f"Executing sync read from: {filepath}")
    try:
        content = filepath.read_text(encoding=encoding)
        log.debug(f"Sync read successful: {filepath} ({len(content)} chars)")
        return content
    except FileNotFoundError:
        log.warning(f"Sync read failed: File not found at {filepath}")
        raise
    except OSError as e:
        log.error(f"Sync read failed for {filepath}: {e}")
        raise

def _sync_read_binary_file(filepath: Path) -> bytes:
    """Synchronously reads binary content from a file."""
    log.debug(f"Executing sync binary read from: {filepath}")
    try:
        content = filepath.read_bytes()
        log.debug(f"Sync binary read successful: {filepath} ({len(content)} bytes)")
        return content
    except FileNotFoundError:
        log.warning(f"Sync binary read failed: File not found at {filepath}")
        raise
    except OSError as e:
        log.error(f"Sync binary read failed for {filepath}: {e}")
        raise

def _sync_rename_file(old_path: Path, new_path: Path):
    """Synchronously renames/moves a file or directory."""
    log.debug(f"Executing sync rename from {old_path} to {new_path}")
    try:
        # Ensure target directory exists if moving across directories
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)
        log.debug(f"Sync rename successful: {old_path} -> {new_path}")
    except OSError as e:
        log.error(f"Sync rename failed for {old_path} -> {new_path}: {e}")
        raise

def _sync_check_exists(path: Path) -> Tuple[bool, bool, bool]:
    """Synchronously checks if a path exists and its type."""
    exists = path.exists()
    is_file = path.is_file() if exists else False
    is_dir = path.is_dir() if exists else False
    log.debug(f"Sync check exists for {path}: exists={exists}, is_file={is_file}, is_dir={is_dir}")
    return exists, is_file, is_dir

def _sync_list_dir(dir_path: Path) -> List[str]:
    """Synchronously lists items in a directory."""
    log.debug(f"Executing sync listdir for: {dir_path}")
    try:
        return [item.name for item in dir_path.iterdir()]
    except FileNotFoundError:
        log.warning(f"Sync listdir failed: Directory not found at {dir_path}")
        raise
    except OSError as e:
        log.error(f"Sync listdir failed for {dir_path}: {e}")
        raise

def _sync_mkdir(dir_path: Path):
    """Synchronously creates a directory, including parents."""
    log.debug(f"Executing sync mkdir for: {dir_path}")
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        log.debug(f"Sync mkdir successful: {dir_path}")
    except OSError as e:
        log.error(f"Sync mkdir failed for {dir_path}: {e}")
        raise

def _sync_remove_file(filepath: Path):
    """Synchronously removes a file."""
    log.debug(f"Executing sync remove file: {filepath}")
    try:
        filepath.unlink(missing_ok=False) # Raise error if not found
        log.debug(f"Sync remove file successful: {filepath}")
    except IsADirectoryError:
        log.error(f"Sync remove file failed: Path is a directory: {filepath}")
        raise
    except FileNotFoundError:
        log.error(f"Sync remove file failed: File not found: {filepath}")
        raise
    except PermissionError:
        log.error(f"Sync remove file failed: Permission denied for {filepath}")
        raise
    except OSError as e:
        log.error(f"Sync remove file failed for {filepath}: {e}")
        raise
# --- End Synchronous Helpers ---

# --- Path Validation Helper ---
async def _resolve_and_validate_path(
    filepath_str: str,
    must_be_within: Optional[Path] = USER_HOME, # DEPRECATED
    allowed_bases: Optional[List[Path]] = None,
    check_existence: bool = False # Only resolve, don't check if it exists yet
) -> Path:
    """
    Resolves a user-provided path string and validates it against allowed base directories.

    Args:
        filepath_str: The path string (can contain '~').
        must_be_within: DEPRECATED - use allowed_bases. If provided, path must be inside this single base.
        allowed_bases: A list of Path objects. The resolved path must be within one of these bases.
                       Defaults to [USER_HOME]. Provide an empty list or None to allow any path
                       (USE WITH EXTREME CAUTION).
        check_existence: If True, raises FileNotFoundError if the resolved path doesn't exist.

    Returns:
        The resolved, absolute Path object.

    Raises:
        ValueError: If the path string is invalid or cannot be resolved.
        PermissionError: If the resolved path is outside all allowed_bases or targets
                         potentially sensitive system directories.
        FileNotFoundError: If check_existence is True and the path does not exist.
    """
    if not filepath_str:
        raise ValueError("File path cannot be empty.")

    try:
        # Expand ~ and resolve to absolute path
        if "~" in filepath_str:
            expanded_path = Path(filepath_str).expanduser()
        else:
            expanded_path = Path(filepath_str)

        # Resolve symbolic links and make absolute
        # Use strict=False initially to allow resolving paths that might not exist yet (for write/rename)
        resolved_path = expanded_path.resolve(strict=False)

    except Exception as e:
        log.error(f"Failed to resolve path '{filepath_str}': {e}")
        raise ValueError(f"Invalid path specified: '{filepath_str}'. Error: {e}") from e

    # Determine the set of allowed base paths
    effective_allowed_bases: List[Path] = []
    if allowed_bases is not None:
        effective_allowed_bases = [p.resolve(strict=False) for p in allowed_bases]
    elif must_be_within is not None: # Handle deprecated arg
         log.warning("Using deprecated 'must_be_within' in _resolve_and_validate_path. Use 'allowed_bases' instead.")
         effective_allowed_bases = [must_be_within.resolve(strict=False)]
    else: # Default to USER_HOME if neither is provided
         effective_allowed_bases = [USER_HOME.resolve(strict=False)]

    # --- Security Checks ---
    # 1. Check against explicitly restricted system directories
    #    (Customize this list based on platform and security needs)
    restricted_starts = ["/etc", "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/System", "/dev", "/proc", "/windows/system32"]
    resolved_path_str_lower = str(resolved_path).lower()
    for restricted in restricted_starts:
        # Allow access if an allowed_base *is* or *is within* the restricted path
        # (e.g., allowing ~/.config which might be under /etc on some systems if explicitly allowed)
        is_exception_allowed = any(
            str(base).lower().startswith(restricted) or restricted.startswith(str(base).lower())
            for base in effective_allowed_bases
        )
        if resolved_path_str_lower.startswith(restricted) and not is_exception_allowed:
            raise PermissionError(f"Access to potentially sensitive system directory '{resolved_path}' is restricted.")

    # 2. Check if path is within any of the allowed base directories
    if effective_allowed_bases: # Only check if bases are specified
        is_within_allowed = False
        for base in effective_allowed_bases:
            try:
                # Use is_relative_to for robust check (requires Python 3.9+)
                if resolved_path.is_relative_to(base):
                    is_within_allowed = True
                    break
            except ValueError: # Happens if paths are on different drives (Windows) or not related
                continue
            except AttributeError: # Fallback for Python < 3.9 (less robust)
                 common = os.path.commonpath([str(base), str(resolved_path)])
                 if common == str(base):
                     is_within_allowed = True
                     break

        if not is_within_allowed:
            allowed_strs = ', '.join(f"'{str(p)}'" for p in effective_allowed_bases)
            raise PermissionError(f"Path '{filepath_str}' resolves to '{resolved_path}', which is outside the allowed base directories: {allowed_strs}.")

    log.debug(f"Resolved path '{filepath_str}' to '{resolved_path}' (Allowed within: {effective_allowed_bases or 'Anywhere'})")

    # 3. Optional existence check
    if check_existence:
         exists, _, _ = await _async_file_op(_sync_check_exists, resolved_path)
         if not exists:
             raise FileNotFoundError(f"Path does not exist: {resolved_path}")

    return resolved_path
# --- End Path Validation Helper ---

# --- Compile Execution & Output Parsing Helper ---
def _parse_compile_output(stdout_str: str, stderr_str: str) -> str:
    """Parses arduino-cli compile output for sketch size and RAM usage."""
    size_info = ""
    # Combine outputs as size info might be in either stdout or stderr
    combined_output = stdout_str + "\n" + stderr_str

    # Regex patterns (case-insensitive, multiline)
    # Sketch uses 1084 bytes (5%) of program storage space. Maximum is 20480 bytes.
    sketch_size_match = re.search(
        r"Sketch uses\s+(\d+)\s+bytes.*?maximum is\s+(\d+)",
        combined_output, re.IGNORECASE | re.DOTALL
    )
    # Global variables use 9 bytes (0%) of dynamic memory, leaving 2039 bytes for local variables. Maximum is 2048 bytes.
    ram_size_match = re.search(
        r"Global variables use\s+(\d+)\s+bytes.*?maximum is\s+(\d+)",
        combined_output, re.IGNORECASE | re.DOTALL
    )

    if sketch_size_match:
        try:
            used_s, max_s = int(sketch_size_match.group(1)), int(sketch_size_match.group(2))
            percent_s = (used_s / max_s * 100) if max_s > 0 else 0
            size_info += f" Program storage: {used_s} / {max_s} bytes ({percent_s:.1f}%)."
        except (ValueError, ZeroDivisionError, IndexError):
            log.warning("Could not parse sketch size numbers from compile output.")

    if ram_size_match:
         try:
             used_r, max_r = int(ram_size_match.group(1)), int(ram_size_match.group(2))
             percent_r = (used_r / max_r * 100) if max_r > 0 else 0
             size_info += f" Dynamic memory: {used_r} / {max_r} bytes ({percent_r:.1f}%)."
         except (ValueError, ZeroDivisionError, IndexError):
             log.warning("Could not parse RAM size numbers from compile output.")

    return size_info.strip()

async def _execute_compile(sketch_path_abs: Path, build_path_abs: Path, board_fqbn: str) -> str:
    """Executes the arduino-cli compile command and returns success message with size info."""
    log.info(f"Executing compilation: Sketch='{sketch_path_abs.name}', FQBN='{board_fqbn}', BuildPath='{build_path_abs}'")
    await _async_file_op(_sync_mkdir, build_path_abs) # Ensure build path exists

    cmd_args = [
        "compile",
        "--fqbn", board_fqbn,
        "--verbose", # Get detailed output for parsing size
        "--build-path", str(build_path_abs),
        str(sketch_path_abs) # Path to the sketch directory
    ]
    cmd_str_for_log = ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in cmd_args)
    log.info(f"Compile Command: arduino-cli {cmd_str_for_log}")

    try:
        stdout_str, stderr_str, _ = await _run_arduino_cli_command(cmd_args, check=True)
        size_info = _parse_compile_output(stdout_str, stderr_str)
        success_message = f"Compilation successful.{' ' + size_info if size_info else ''}"
        log.info(f"Compilation successful for '{sketch_path_abs.name}'.{(' ' + size_info) if size_info else ''}")
        return success_message
    except (FileNotFoundError, PermissionError, ValueError, Exception) as e:
        # Errors like FileNotFoundError (missing core), PermissionError,
        # or generic Exception (compile errors) are caught by _run_arduino_cli_command
        log.error(f"Compilation failed for '{sketch_path_abs.name}' with FQBN '{board_fqbn}': {e}")
        # Re-raise the specific error caught by the helper
        raise Exception(f"Compilation failed: {e}") from e

# --- Board Info Helper ---
async def _fetch_and_format_board_info(port_address: str, board_name: str, fqbn: str) -> str:
    """Helper to fetch platform libs for a board and format its output string."""
    platform_libraries: Dict[str, List[str]] = {}
    lib_cmd_args = ["lib", "list", "-b", fqbn, "--format", "json"]
    log.debug(f"Fetching platform libraries for FQBN '{fqbn}'")
    try:
        # Run command, don't check=True as failure here is non-critical for board listing
        lib_stdout, lib_stderr, lib_retcode = await _run_arduino_cli_command(lib_cmd_args, check=False)

        if lib_retcode == 0 and lib_stdout:
            try:
                lib_data = json.loads(lib_stdout)
                # Handle potential variations in JSON structure
                installed_libs_outer = lib_data.get("libraries", lib_data.get("installed_libraries"))
                if isinstance(installed_libs_outer, list):
                    for lib_item in installed_libs_outer:
                         # Handle nested 'library' key if present
                         lib_details = lib_item.get("library", lib_item)
                         if isinstance(lib_details, dict) and lib_details.get("location") == "platform":
                             lib_name = lib_details.get("name")
                             provides_includes = lib_details.get("provides_includes", [])
                             if lib_name and isinstance(provides_includes, list):
                                 platform_libraries[lib_name] = provides_includes
                    log.debug(f"Found {len(platform_libraries)} platform libraries for {fqbn}")
                else:
                    log.warning(f"Unexpected format for 'libraries'/'installed_libraries' in JSON for FQBN '{fqbn}'.")
            except json.JSONDecodeError as json_e:
                log.warning(f"Failed to decode lib list JSON for FQBN '{fqbn}'. Error: {json_e}. Raw: {lib_stdout[:200]}...")
            except Exception as parse_e:
                log.warning(f"Error parsing lib list JSON for FQBN '{fqbn}'. Error: {parse_e}")
        elif lib_retcode != 0:
            log.warning(f"Failed to list libs for FQBN '{fqbn}'. Exit: {lib_retcode}. Stderr: {lib_stderr}")

    except Exception as fetch_err:
        log.error(f"Error fetching libraries for FQBN '{fqbn}': {fetch_err}")

    # Format the output string
    line = f"- Port: {port_address}, Board: {board_name}, FQBN: {fqbn}"
    if platform_libraries:
        line += "\n    Platform Libraries:"
        for lib_name, includes in sorted(platform_libraries.items()):
            include_str = ", ".join(includes) if includes else "(no includes listed)"
            line += f"\n      - {lib_name} (Includes: {include_str})"
    else:
        line += "\n    Platform Libraries: (None found or error fetching)"
    return line
# --- End Board Info Helper ---

# --- File Opening Helper ---
async def _open_file_in_default_app(filepath: Path):
    """
    Attempts to open the specified file using the system's default application.
    Logs a warning if opening fails, but does not raise an exception.
    """
    filepath_str = str(filepath.resolve()) # Ensure absolute path
    log.info(f"Attempting to open file in default application: {filepath_str}")
    command: Optional[List[str]] = None
    try:
        if sys.platform == "win32":
            # Using 'start ""' handles paths with spaces correctly
            command = ["start", "", filepath_str]
            # Use shell=True on Windows for 'start'
            process = await asyncio.create_subprocess_shell(
                " ".join(command), # Pass as single string for shell
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            # Don't wait for completion, just launch
            log.debug(f"Launched Windows 'start' command for {filepath_str}")
        elif sys.platform == "darwin": # macOS
            command = ["open", filepath_str]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            log.debug(f"Launched macOS 'open' command for {filepath_str}")
        else: # Linux and other POSIX
            command = ["xdg-open", filepath_str]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            log.debug(f"Launched Linux 'xdg-open' command for {filepath_str}")

        # We don't await process.wait() because we want the server to continue
        # immediately after launching the application.

    except FileNotFoundError:
        cmd_name = command[0] if command else "System command"
        log.warning(f"Could not open file '{filepath_str}'. Command '{cmd_name}' not found. Is the associated application installed and PATH configured?")
    except Exception as e:
        cmd_str = ' '.join(command) if command else "System command"
        log.warning(f"Failed to open file '{filepath_str}' using '{cmd_str}'. Error: {type(e).__name__}: {e}")
# --- End File Opening Helper ---


# ==============================================================================
# MCP Tool Definitions
# ==============================================================================

@mcp.tool()
async def create_new_sketch(sketch_name: str) -> str:
    """
    Creates a new Arduino sketch directory and .ino file with the given name
    inside the designated sketches directory ('~/Documents/Arduino_MCP_Sketches/').
    The sketch name must be a valid directory name and cannot contain path separators.
    If a directory or file with that name already exists, it returns an error.
    **After successful creation, attempts to open the new .ino file in the default editor.**

    Args:
        sketch_name: The desired name for the sketch (e.g., 'MyBlink').
                     Cannot be empty or contain '/', '\\', or '..'.

    Returns:
        Success message indicating the absolute path of the created sketch directory.

    Raises:
        ValueError: If sketch_name is invalid.
        FileExistsError: If a file or directory with that name already exists.
        PermissionError: If directory creation fails due to permissions.
        Exception: For other errors during creation reported by arduino-cli.
    """
    log.info(f"Tool Call: create_new_sketch(sketch_name='{sketch_name}')")
    # Basic name validation
    if not sketch_name or any(c in sketch_name for c in ['/', '\\']) or ".." in sketch_name:
         raise ValueError("Invalid sketch_name. Cannot be empty, contain path separators ('/', '\\'), or '..'.")

    sketch_dir = SKETCHES_BASE_DIR / sketch_name
    # Resolve *before* checking existence to get the final intended path
    sketch_dir_abs = sketch_dir.resolve(strict=False)
    main_ino_file_abs = sketch_dir_abs / f"{sketch_name}.ino" # Path for opening

    # Check if path exists using our async helper
    exists, _, _ = await _async_file_op(_sync_check_exists, sketch_dir_abs)
    if exists:
        error_msg = f"Failed to create sketch: Path '{sketch_dir_abs}' already exists."
        log.error(error_msg)
        raise FileExistsError(error_msg)

    # Use the absolute path for the arduino-cli command
    cmd_args = ["sketch", "new", str(sketch_dir_abs)]
    try:
        # Run the command, check=True will raise specific errors on failure
        await _run_arduino_cli_command(cmd_args, check=True)
        success_message = f"Successfully created sketch '{sketch_name}' at '{sketch_dir_abs}'."
        log.info(success_message)

        # --- Attempt to open the created .ino file ---
        await _open_file_in_default_app(main_ino_file_abs)
        # --- End open attempt ---

        return success_message
    except (FileExistsError, PermissionError, ValueError) as e:
        # Re-raise specific errors if they occurred during validation or execution
        log.error(f"Sketch creation failed for '{sketch_name}': {e}")
        raise e
    except Exception as e:
        # Catch generic exceptions from _run_arduino_cli_command or other issues
        log.error(f"Unexpected error creating sketch '{sketch_name}' at {sketch_dir_abs}: {e}")
        # Attempt cleanup only if the directory was likely created by the failed command
        try:
            if sketch_dir_abs.is_dir() and not any(sketch_dir_abs.iterdir()):
                log.warning(f"Attempting cleanup of empty sketch directory created during failed attempt: {sketch_dir_abs}")
                await _async_file_op(sketch_dir_abs.rmdir)
        except Exception as cleanup_err:
            log.warning(f"Cleanup failed for {sketch_dir_abs}: {cleanup_err}")
        raise Exception(f"Failed to create sketch '{sketch_name}': {e}") from e


@mcp.tool()
async def list_sketches() -> str:
    """
    Lists all valid Arduino sketches found within the designated sketches directory
    ('~/Documents/Arduino_MCP_Sketches/'). A valid sketch is defined as a directory
    containing an '.ino' file that shares the same base name as the directory.
    Excludes hidden files/directories and the build temp directory.

    Returns:
        A string listing the names of valid sketches, or a message indicating none were found
        or the base directory doesn't exist.

    Raises:
        Exception: If there's an error reading the sketches directory.
    """
    log.info(f"Tool Call: list_sketches (in '{SKETCHES_BASE_DIR}')")
    base_exists, _, base_is_dir = await _async_file_op(_sync_check_exists, SKETCHES_BASE_DIR)

    if not base_exists:
        log.warning(f"Sketch base directory '{SKETCHES_BASE_DIR}' not found.")
        return f"Sketch base directory '{SKETCHES_BASE_DIR}' not found."
    if not base_is_dir:
        log.error(f"Path '{SKETCHES_BASE_DIR}' exists but is not a directory.")
        return f"Error: Path '{SKETCHES_BASE_DIR}' is not a directory."

    try:
        items = await _async_file_op(_sync_list_dir, SKETCHES_BASE_DIR)
    except Exception as e:
        log.error(f"Error listing directory '{SKETCHES_BASE_DIR}': {e}")
        raise Exception(f"Error listing sketches directory: {e}") from e

    # Asynchronously check each item to see if it's a valid sketch directory
    async def check_item_is_sketch(item_name: str) -> Optional[str]:
        if item_name.startswith('.') or item_name == BUILD_TEMP_DIR.name:
            return None # Skip hidden items and build dir

        item_path = SKETCHES_BASE_DIR / item_name
        # Check if it's a directory
        exists, _, is_dir = await _async_file_op(_sync_check_exists, item_path)
        if not (exists and is_dir):
            return None

        # Check if the corresponding .ino file exists inside
        sketch_file_path = item_path / f"{item_name}.ino"
        file_exists, is_file, _ = await _async_file_op(_sync_check_exists, sketch_file_path)
        if file_exists and is_file:
            return item_name # It's a valid sketch

        return None

    # Run checks concurrently
    results = await asyncio.gather(*(check_item_is_sketch(item) for item in items))

    # Filter out None results and sort
    valid_sketches = sorted([name for name in results if name is not None])

    if not valid_sketches:
        log.info(f"No valid sketches found in '{SKETCHES_BASE_DIR}'.")
        return f"No valid sketches found in '{SKETCHES_BASE_DIR}'."

    log.info(f"Found {len(valid_sketches)} valid sketches.")
    return "Available sketches:\n" + "\n".join(f"- {sketch}" for sketch in valid_sketches)


@mcp.tool()
async def list_boards() -> str:
    """
    Lists connected Arduino boards detected by 'arduino-cli board list'.
    Provides Port, Board Name, and the crucial Fully Qualified Board Name (FQBN)
    needed for verification and uploading. Also attempts to list associated
    platform libraries for each detected board for additional context.

    Returns:
        A formatted string listing detected boards with their details and platform libraries,
        or a message indicating no boards were detected or an error occurred.

    Raises:
        Exception: If the 'arduino-cli board list' command fails unexpectedly or
                   if the JSON output cannot be parsed.
    """
    log.info("Tool Call: list_boards")
    board_list_cmd_args = ["board", "list", "--format", "json"]
    try:
        # Run command, don't check=True initially to handle "no boards found" gracefully
        board_list_json, board_list_stderr, board_list_retcode = await _run_arduino_cli_command(
            board_list_cmd_args, check=False
        )

        # Handle cases where the command might succeed but return empty or indicate no boards
        if board_list_retcode == 0 and not board_list_json.strip():
            log.info("arduino-cli board list returned success but empty JSON output.")
            return "No connected Arduino boards detected (Command succeeded, but no boards found)."

        # Handle explicit "no boards found" messages even if retcode is 0 or non-zero
        combined_output_lower = (board_list_json + board_list_stderr).lower()
        if "no boards found" in combined_output_lower or "could not find any board" in combined_output_lower:
            log.info("arduino-cli board list reported no boards found.")
            return "No connected Arduino boards detected."

        # If retcode is non-zero and it wasn't a "no boards" message, raise error
        if board_list_retcode != 0:
            raise Exception(f"arduino-cli board list failed (code {board_list_retcode}): {board_list_stderr or board_list_json}")

        # Proceed with JSON parsing if command succeeded and output is not empty
        try:
            boards_data = json.loads(board_list_json)
        except json.JSONDecodeError as e:
            log.error(f"Error decoding JSON from 'board list': {e}. Raw: {board_list_json[:500]}...")
            raise Exception(f"Error decoding JSON from 'board list': {e}") from e

        # Parse the potentially varied JSON structure
        ports_list = []
        if isinstance(boards_data, dict):
            ports_list = boards_data.get("detected_ports", []) # Newer format
        elif isinstance(boards_data, list):
            ports_list = boards_data # Older format
        else:
            log.error(f"Could not parse board list output (unexpected JSON root type): {board_list_json[:500]}...")
            raise Exception("Could not parse board list output (unexpected JSON root type).")

        if not isinstance(ports_list, list):
            log.error(f"Could not parse board list output (expected list of ports): {board_list_json[:500]}...")
            raise Exception("Could not parse board list output (expected list of ports).")

        # Create tasks to fetch library info for each board concurrently
        tasks = []
        for port_info in ports_list:
             if not isinstance(port_info, dict): continue
             port_details = port_info.get("port", {})
             if not isinstance(port_details, dict): continue
             port_address = port_details.get("address")

             # Handle different keys for board info ('matching_boards' vs 'boards')
             matching_boards = port_info.get("matching_boards", port_info.get("boards", []))

             if port_address and isinstance(matching_boards, list):
                 for board in matching_boards:
                     if isinstance(board, dict) and board.get("fqbn"):
                         fqbn = board["fqbn"]
                         board_name = board.get("name", "Unknown Board")
                         # Schedule the helper function call
                         tasks.append(_fetch_and_format_board_info(port_address, board_name, fqbn))

        # Gather results from library fetching tasks
        detected_boards_info = []
        if tasks:
            detected_boards_info = await asyncio.gather(*tasks)

        # Format final output
        if not detected_boards_info:
            ports_without_boards = [
                p.get("port", {}).get("address") for p in ports_list
                if p.get("port", {}).get("address") and not p.get("matching_boards") and not p.get("boards")
            ]
            if ports_without_boards:
                return f"No connected boards with recognized FQBN detected.\nFound ports without recognized boards: {', '.join(ports_without_boards)}"
            else:
                return "No connected Arduino boards with recognized FQBN detected."

        output_lines = ["Detected Arduino Boards:"]
        output_lines.extend(detected_boards_info)
        formatted_output = "\n".join(output_lines).strip()
        log.info(f"Detected boards (summary):\n{formatted_output}") # Log summary
        return formatted_output

    except Exception as e:
        log.exception("Error during list_boards execution.")
        # Avoid leaking raw internal errors directly if possible
        raise Exception(f"Failed to list boards: {type(e).__name__}") from e


@mcp.tool()
async def serial_monitor_start(
    port: str,
    baud: int = SERIAL_DEFAULT_BAUD,
    buffer_lines: int = SERIAL_DEFAULT_BUFFER_LINES,
    log_to_file: bool = True,
    log_file: str = "",
    board_fqbn: str = "",
    protocol: str = "",
    auto_reconnect: bool = True,
    log_max_bytes: int = SERIAL_DEFAULT_LOG_MAX_BYTES,
    log_rotate_count: int = SERIAL_DEFAULT_LOG_ROTATE_COUNT
) -> str:
    """
    Starts a background serial monitor using arduino-cli and returns a monitor id.

    Args:
        port: Serial port address (e.g., COM3, /dev/ttyACM0).
        baud: Baud rate for the serial connection (default 115200).
        buffer_lines: Max lines to keep in memory (default 2000).
        log_to_file: Whether to append incoming lines to a log file.
        log_file: Optional explicit log file path (within user home). If empty, a default is used.
        board_fqbn: Optional FQBN to help arduino-cli identify port settings.
        protocol: Optional protocol (e.g., "serial").
        auto_reconnect: If True, the monitor will restart when the port disconnects.
        log_max_bytes: Max size of a log file before rotation (0 disables).
        log_rotate_count: Number of rotated log files to keep (0 disables).

    Returns:
        A message with monitor id and log path.
    """
    log.info(f"Tool Call: serial_monitor_start(port='{port}', baud={baud})")
    if not port:
        raise ValueError("Serial port must be specified.")
    if baud <= 0:
        raise ValueError("Baud rate must be a positive integer.")
    if buffer_lines <= 0:
        raise ValueError("buffer_lines must be a positive integer.")
    if log_max_bytes < 0:
        raise ValueError("log_max_bytes must be >= 0.")
    if log_rotate_count < 0:
        raise ValueError("log_rotate_count must be >= 0.")

    await _stop_monitors_for_port(port)

    log_path: Optional[Path] = None
    if log_to_file:
        if log_file:
            log_path = await _resolve_and_validate_path(
                log_file,
                allowed_bases=[USER_HOME],
                check_existence=False
            )
        else:
            log_path = _default_serial_log_path(port)

    state = await _create_serial_monitor_state(
        port=port,
        baud=baud,
        buffer_lines=buffer_lines,
        log_path=log_path,
        board_fqbn=board_fqbn or None,
        protocol=protocol or None,
        auto_reconnect=auto_reconnect,
        log_max_bytes=log_max_bytes,
        log_rotate_count=log_rotate_count,
    )

    log_hint = str(state.log_path) if state.log_path else "disabled"
    return (
        f"Serial monitor started: id={state.monitor_id}, port={state.port}, "
        f"baud={state.baud}, log={log_hint}, max_bytes={state.log_max_bytes}, "
        f"rotate={state.log_rotate_count}"
    )


@mcp.tool()
async def serial_monitor_read(monitor_id: str, lines: int = 200) -> str:
    """
    Reads the latest lines from a running serial monitor buffer.
    """
    if not monitor_id:
        raise ValueError("monitor_id is required.")
    if lines <= 0:
        raise ValueError("lines must be a positive integer.")

    if _SERIAL_MONITORS_LOCK.locked():
        return "Serial monitor registry busy; try again."
    async with _SERIAL_MONITORS_LOCK:
        state = _SERIAL_MONITORS.get(monitor_id)
    if not state:
        raise FileNotFoundError(f"Serial monitor not found: {monitor_id}")

    buffer_snapshot = list(state.buffer)[-lines:]
    header = f"Serial monitor {state.monitor_id} (port={state.port}, baud={state.baud}, running={state.running})"
    if not buffer_snapshot:
        return f"{header}\n(no data yet)"
    return header + "\n" + "\n".join(buffer_snapshot)


@mcp.tool()
async def serial_monitor_list() -> str:
    """
    Lists active and paused serial monitors.
    """
    log.info("Tool Call: serial_monitor_list()")
    if _SERIAL_MONITORS_LOCK.locked():
        return "Serial monitor registry busy; try again."
    async with _SERIAL_MONITORS_LOCK:
        states = list(_SERIAL_MONITORS.values())

    if not states:
        return "No serial monitors."

    lines_out = ["Serial monitors:"]
    for state in states:
        log_path = str(state.log_path) if state.log_path else "disabled"
        lines_out.append(
            f"- id={state.monitor_id} port={state.port} baud={state.baud} running={state.running} "
            f"auto_reconnect={state.auto_reconnect} log={log_path} "
            f"max_bytes={state.log_max_bytes} rotate={state.log_rotate_count} "
            f"buffer={len(state.buffer)}/{state.buffer_max}"
        )
    return "\n".join(lines_out)


@mcp.tool()
async def serial_monitor_stop(
    monitor_id: str,
    force: bool = True,
    wait_tasks: bool = False
) -> str:
    """
    Stops a running serial monitor and releases the port.
    Defaults to a fast stop (force kill + no waiting) to avoid tool timeouts.
    When wait_tasks is False, the stop is scheduled in the background.
    You do NOT need to call this before upload; upload auto-pauses/resumes monitors.
    """
    log.info(f"Tool Call: serial_monitor_stop(monitor_id='{monitor_id}', force={force}, wait_tasks={wait_tasks})")
    if not monitor_id:
        raise ValueError("monitor_id is required.")

    async with _SERIAL_MONITORS_LOCK:
        state = _SERIAL_MONITORS.get(monitor_id)
    if not state:
        raise FileNotFoundError(f"Serial monitor not found: {monitor_id}")

    # Remove from registry immediately to avoid blocking calls
    async with _SERIAL_MONITORS_LOCK:
        _SERIAL_MONITORS.pop(monitor_id, None)

    stop_coro = _stop_serial_monitor_state(
        state,
        remove=False,
        force=force,
        timeout_sec=2.0,
        wait_tasks=wait_tasks
    )
    if wait_tasks:
        await stop_coro
        return f"Serial monitor stopped: {monitor_id}"
    asyncio.create_task(stop_coro)
    return f"Serial monitor stopping (async): {monitor_id}"


@mcp.tool()
async def verify_code(sketch_name: str, board_fqbn: str) -> str:
    """
    Verifies (compiles) the specified Arduino sketch for the given board FQBN
    to check for errors, without uploading.

    Args:
        sketch_name: Name of the sketch directory within '~/Documents/Arduino_MCP_Sketches/'.
                     Must be a valid directory name (no path separators).
        board_fqbn: The Fully Qualified Board Name identifying the target board hardware
                    (e.g., 'arduino:avr:uno', 'arduino:renesas_uno:unor4wifi').
                    Format must be 'vendor:arch:board'. Use 'list_boards' or 'board_search'.

    Returns:
        Success message including compilation stats (program storage, dynamic memory usage),
        or raises an error if verification fails.

    Raises:
        ValueError: If sketch_name or board_fqbn is invalid or badly formatted.
        FileNotFoundError: If the sketch directory, main .ino file, or required
                           cores/tools are not found.
        PermissionError: If there are permission issues accessing files.
        Exception: For compilation errors reported by arduino-cli or other issues.
    """
    log.info(f"Tool Call: verify_code(sketch='{sketch_name}', fqbn='{board_fqbn}')")
    if not sketch_name or any(c in sketch_name for c in ['/', '\\']) or ".." in sketch_name:
        raise ValueError("Invalid sketch_name. Cannot be empty or contain path separators or '..'.")
    if not board_fqbn or ":" not in board_fqbn or len(board_fqbn.split(':')) < 3:
        raise ValueError("Invalid or missing board_fqbn. Format must be 'vendor:arch:board'. Use list_boards or board_search.")

    sketch_dir = SKETCHES_BASE_DIR / sketch_name
    # Resolve first to ensure we have the absolute path for checks and commands
    sketch_path_abs = sketch_dir.resolve(strict=False)
    build_path_abs = (BUILD_TEMP_DIR / f"{sketch_name}_verify_{board_fqbn.replace(':', '_')}").resolve(strict=False)

    # Check sketch directory existence
    exists, _, is_dir = await _async_file_op(_sync_check_exists, sketch_path_abs)
    if not exists or not is_dir:
         raise FileNotFoundError(f"Sketch directory not found or is not a directory: {sketch_path_abs}")

    # Check main .ino file existence
    main_ino_file = sketch_path_abs / f"{sketch_name}.ino"
    ino_exists, is_file, _ = await _async_file_op(_sync_check_exists, main_ino_file)
    if not ino_exists or not is_file:
         raise FileNotFoundError(f"Main sketch file '{main_ino_file.name}' not found or is not a file in {sketch_path_abs}")

    try:
        # _execute_compile handles mkdir for build_path_abs
        compile_message = await _execute_compile(sketch_path_abs, build_path_abs, board_fqbn)
        # compile_message already contains "Compilation successful." prefix
        success_message = f"Verification successful for sketch '{sketch_name}'.{compile_message.replace('Compilation successful.', '')}"
        log.info(success_message)
        return success_message
    except (FileNotFoundError, PermissionError, ValueError, Exception) as e:
        # Specific errors raised by _execute_compile or _run_arduino_cli_command
        log.error(f"Verification failed for sketch '{sketch_name}' with FQBN '{board_fqbn}': {e}")
        # Re-raise the original specific error for clarity
        raise Exception(f"Verification failed for sketch '{sketch_name}': {e}") from e


@mcp.tool()
async def upload_sketch(
    sketch_name: str,
    port: str,
    board_fqbn: str,
    monitor_after_upload: bool = False,
    monitor_baud: int = SERIAL_DEFAULT_BAUD
) -> str:
    """
    Verifies (compiles) AND uploads the specified sketch to the Arduino board
    connected to the given serial port, using the specified FQBN.
    If a serial monitor is running on the same port, it will be paused and
    automatically resumed after the upload.

    Args:
        sketch_name: Name of the sketch directory within '~/Documents/Arduino_MCP_Sketches/'.
                     Must be a valid directory name.
        port: The serial port address of the target board (e.g., '/dev/ttyACM0', 'COM3').
              Use 'list_boards' to find the correct port. Cannot be empty.
        board_fqbn: The Fully Qualified Board Name identifying the target board hardware
                    (e.g., 'arduino:avr:uno', 'arduino:renesas_uno:unor4wifi').
                    Format must be 'vendor:arch:board'. Use 'list_boards' or 'board_search'. MANDATORY.
        monitor_after_upload: If True, start a serial monitor after a successful upload
                              (only if one wasn't already running on the port).
        monitor_baud: Baud rate for the auto-started monitor (default 115200).

    Returns:
        Success message confirming the upload, potentially including compilation stats.

    Raises:
        ValueError: If any argument is invalid (empty, bad format).
        FileNotFoundError: If the sketch directory, main .ino file, or required
                           cores/tools/uploaders are not found.
        PermissionError: If there are permission issues accessing the port or files.
        ConnectionError: If the board cannot be found or communicated with on the specified port.
        TimeoutError: If communication with the board times out during upload.
        Exception: For compilation errors or other upload issues reported by arduino-cli.
    """
    log.info(f"Tool Call: upload_sketch(sketch='{sketch_name}', port='{port}', fqbn='{board_fqbn}', monitor_after_upload={monitor_after_upload})")
    # Input validation
    if not sketch_name or any(c in sketch_name for c in ['/', '\\']) or ".." in sketch_name:
        raise ValueError("Invalid sketch_name. Cannot be empty or contain path separators or '..'.")
    if not port:
        raise ValueError("Serial port must be specified.")
    if not board_fqbn or ":" not in board_fqbn or len(board_fqbn.split(':')) < 3:
        raise ValueError("Invalid or missing board_fqbn. Format must be 'vendor:arch:board'.")
    if monitor_baud <= 0:
        raise ValueError("monitor_baud must be a positive integer.")

    sketch_dir = SKETCHES_BASE_DIR / sketch_name
    sketch_path_abs = sketch_dir.resolve(strict=False)
    # Use a consistent build path per sketch/FQBN for potential caching by CLI
    build_path_abs = (BUILD_TEMP_DIR / f"{sketch_name}_upload_{board_fqbn.replace(':', '_')}").resolve(strict=False)

    # Check sketch directory and main file existence
    exists, _, is_dir = await _async_file_op(_sync_check_exists, sketch_path_abs)
    if not exists or not is_dir:
        raise FileNotFoundError(f"Sketch directory not found or is not a directory: {sketch_path_abs}")
    main_ino_file = sketch_path_abs / f"{sketch_name}.ino"
    ino_exists, is_file, _ = await _async_file_op(_sync_check_exists, main_ino_file)
    if not ino_exists or not is_file:
        raise FileNotFoundError(f"Main sketch file '{main_ino_file.name}' not found or is not a file in {sketch_path_abs}")

    paused_monitors: List[SerialMonitorState] = []
    restarted_ids: List[str] = []
    try:
        # Ensure build directory exists (handled within _execute_compile now)
        # log.info(f"Using build path: {build_path_abs}")

        # --- Step 1: Compile ---
        log.info("Starting verification (compilation) step before upload...")
        compile_message = await _execute_compile(sketch_path_abs, build_path_abs, board_fqbn)
        log.info(f"Verification successful. {compile_message.replace('Compilation successful.', '').strip()}") # Log size info

        # --- Step 2: Upload ---
        log.info("Starting upload step...")
        paused_monitors = await _pause_monitors_for_port(port, force=True, wait_tasks=False)
        cmd_args_upload = [
            "upload",
            "--port", port,
            "--fqbn", board_fqbn,
            "--verbose", # Useful for debugging upload issues
            "--build-path", str(build_path_abs), # Reuse build path
            str(sketch_path_abs) # Path to sketch directory
        ]
        cmd_str_for_log = ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in cmd_args_upload)
        log.info(f"Upload Command: arduino-cli {cmd_str_for_log}")

        # Run upload command, check=True will raise specific errors on failure
        upload_stdout, upload_stderr, _ = await _run_arduino_cli_command(cmd_args_upload, check=True)

        # Construct success message
        success_message = f"Successfully uploaded sketch '{sketch_name}' to board '{board_fqbn}' on port '{port}'."
        # Optionally add compile stats back if desired
        # success_message += compile_message.replace('Compilation successful.', '').strip()

        # Check output for common success indicators (optional, as check=True handles failure)
        combined_output = (upload_stdout + "\n" + upload_stderr).lower()
        success_indicators = ["leaving...", "hard resetting via", "done uploading", "upload successful", "verify successful", "bytes written"]
        if any(indicator in combined_output for indicator in success_indicators):
            log.info(success_message)
        else:
            # This case is less likely if check=True worked, but good for logging
            log.warning(f"{success_message} (Standard confirmation message not found in output; verify on device). Output:\n{upload_stdout}\n{upload_stderr}")

        # Restart any monitors that were paused for upload
        if paused_monitors:
            restarted_ids = await _resume_monitors(paused_monitors)
        elif monitor_after_upload:
            try:
                new_state = await _create_serial_monitor_state(
                    port=port,
                    baud=monitor_baud,
                    buffer_lines=SERIAL_DEFAULT_BUFFER_LINES,
                    log_path=_default_serial_log_path(port),
                    board_fqbn=board_fqbn,
                    protocol="serial",
                    auto_reconnect=True,
                    log_max_bytes=SERIAL_DEFAULT_LOG_MAX_BYTES,
                    log_rotate_count=SERIAL_DEFAULT_LOG_ROTATE_COUNT
                )
                restarted_ids = [new_state.monitor_id]
            except Exception as e:
                log.warning(f"Failed to start serial monitor after upload: {e}")

        if restarted_ids:
            success_message += f" Serial monitor running: {', '.join(restarted_ids)}."

        return success_message

    except (FileNotFoundError, PermissionError, ValueError, ConnectionError, TimeoutError, Exception) as e:
        # Catch specific errors raised by _execute_compile or _run_arduino_cli_command
        log.error(f"Upload process failed for sketch '{sketch_name}': {e}")
        # Attempt to restore any monitors we paused before failing
        if paused_monitors:
            try:
                await _resume_monitors(paused_monitors)
            except Exception as resume_err:
                log.warning(f"Failed to resume serial monitor after upload error: {resume_err}")
        # Re-raise the specific error for better feedback
        raise Exception(f"Upload failed for sketch '{sketch_name}': {e}") from e


@mcp.tool()
async def board_search(board_name_query: str) -> str:
    """
    Searches the online Arduino board index for boards matching the query.
    Useful for finding the correct FQBN (Fully Qualified Board Name) for a board
    that is not currently connected or detected by 'list_boards'.

    Args:
        board_name_query: A partial or full name of the board to search for
                          (e.g., "uno r4 wifi", "esp32", "seeed xiao").

    Returns:
        A string listing matching boards and their FQBNs, or a message indicating
        no matches were found or an error occurred.

    Raises:
        ValueError: If board_name_query is empty.
        Exception: If the search command fails unexpectedly.
    """
    log.info(f"Tool Call: board_search(query='{board_name_query}')")
    if not board_name_query:
        raise ValueError("Board name query cannot be empty.")

    cmd_args = ["board", "search", board_name_query]
    try:
        # Don't check=True initially to handle "no boards found"
        stdout, stderr, retcode = await _run_arduino_cli_command(cmd_args, check=False)
        output = (stdout or stderr).strip() # Combine output, prefer stdout
        output_lower = output.lower()

        if retcode != 0 or not output or "no boards found" in output_lower or "no matching board" in output_lower:
             log.info(f"Board search for '{board_name_query}' found no results or failed (Code: {retcode}). Output: {output}")
             return f"No boards found matching '{board_name_query}' in the online index."

        log.info(f"Board search results for '{board_name_query}':\n{output}")
        # Return the raw output from the CLI as it's usually well-formatted
        return output

    except Exception as e:
        log.exception(f"Board search failed unexpectedly for query '{board_name_query}'")
        raise Exception(f"Board search failed: {type(e).__name__}") from e


@mcp.tool()
async def lib_search(library_name: str, limit: int = 15) -> str:
    """
    Searches for Arduino libraries matching the given name. Performs BOTH:
    1. An online search via the Arduino Library Manager index.
    2. A fuzzy search against locally installed platform libraries (if 'thefuzz' is installed).

    Args:
        library_name: The name (or part of the name) of the library to search for
                      (e.g., "FastLED", "DHT sensor", "Adafruit GFX").
        limit: The maximum number of results to return for *each* search type
               (online, local fuzzy). Defaults to 15. Must be a positive integer.

    Returns:
        A formatted string containing results from both online and local searches,
        separated clearly. Returns a message if no matches are found in either source.

    Raises:
        ValueError: If library_name is empty or limit is invalid.
        Exception: If underlying CLI commands fail unexpectedly.
    """
    log.info(f"Tool Call: lib_search(library_name='{library_name}', limit={limit})")
    if not library_name:
        raise ValueError("Library name cannot be empty.")
    if not isinstance(limit, int) or limit <= 0:
        log.warning(f"Invalid limit '{limit}' provided, using default 15.")
        limit = 15

    final_output_lines = []
    online_results_found = False
    fuzzy_results_found = False

    # --- Online Search ---
    online_output_section = ["--- Online Search Results (Library Manager) ---"]
    online_search_cmd_args = ["lib", "search", library_name]
    try:
        # Don't check=True initially
        stdout, stderr, retcode = await _run_arduino_cli_command(online_search_cmd_args, check=False)
        full_output = (stdout or stderr).strip() # Combine output
        output_lower = full_output.lower()

        if retcode == 0 and full_output and "no libraries found" not in output_lower and "no matching libraries" not in output_lower:
            online_results_found = True
            lines = full_output.splitlines()
            header = ""
            data_lines = lines
            # Try to detect and format header nicely
            if lines and "Name" in lines[0] and ("Author" in lines[0] or "Version" in lines[0]):
                header = lines[0] + "\n" + ("-" * len(lines[0]))
                data_lines = lines[1:]
                online_output_section.append(header)

            limited_data_lines = data_lines[:limit]
            online_output_section.extend(limited_data_lines)
            if len(data_lines) > limit:
                online_output_section.append(f"... (truncated to {limit} online results)")
            log.info(f"Online lib search found {len(data_lines)} results for '{library_name}'.")
        else:
            if retcode != 0:
                log.warning(f"Online lib search command failed (code {retcode}): {stderr}")
                online_output_section.append(f"(Online search command failed: {stderr or 'Unknown error'})")
            else:
                log.info(f"Online lib search for '{library_name}' found no results.")
                online_output_section.append("(No results found in online index)")
    except Exception as e:
        log.exception(f"Error during online library search for '{library_name}'")
        online_output_section.append(f"(Error during online search: {type(e).__name__})")

    final_output_lines.extend(online_output_section)
    final_output_lines.append("\n") # Separator

    # --- Local Fuzzy Search ---
    fuzzy_output_section = ["--- Local Platform Library Matches (Fuzzy Search) ---"]
    if FUZZY_ENABLED:
        log.info(f"Performing fuzzy search on local platform libraries for '{library_name}'.")
        platform_list_cmd_args = ["lib", "list", "--all", "--format", "json"]
        fuzzy_matches = []
        try:
            # Don't check=True
            plat_stdout, plat_stderr, plat_retcode = await _run_arduino_cli_command(platform_list_cmd_args, check=False)

            if plat_retcode == 0 and plat_stdout:
                try:
                    plat_lib_data = json.loads(plat_stdout)
                    installed_libs_outer = plat_lib_data.get("libraries", plat_lib_data.get("installed_libraries", []))

                    if isinstance(installed_libs_outer, list):
                        for lib_item in installed_libs_outer:
                            lib_details = lib_item.get("library", lib_item)
                            if isinstance(lib_details, dict) and lib_details.get("location") == "platform":
                                lib_name = lib_details.get("name", "")
                                provides_includes = lib_details.get("provides_includes", [])
                                if not lib_name: continue # Skip if no name

                                best_score = 0
                                # Score against library name
                                name_score = fuzz.partial_ratio(library_name.lower(), lib_name.lower())
                                best_score = max(best_score, name_score)
                                # Score against include files (and their stems)
                                if isinstance(provides_includes, list):
                                    for include_file in provides_includes:
                                        if not include_file: continue
                                        include_stem = Path(include_file).stem
                                        include_score = fuzz.partial_ratio(library_name.lower(), include_file.lower())
                                        stem_score = fuzz.partial_ratio(library_name.lower(), include_stem.lower())
                                        best_score = max(best_score, include_score, stem_score)

                                if best_score >= FUZZY_SEARCH_THRESHOLD:
                                    fuzzy_matches.append({"name": lib_name, "includes": provides_includes, "score": best_score})
                    else:
                        log.warning("Could not parse 'libraries'/'installed_libraries' list from platform lib JSON.")
                except json.JSONDecodeError as json_e:
                    log.warning(f"Failed to decode platform lib JSON for fuzzy search. Error: {json_e}. Raw: {plat_stdout[:200]}...")
                except Exception as parse_e:
                    log.warning(f"Error parsing platform lib JSON for fuzzy search. Error: {parse_e}")
            elif plat_retcode != 0:
                log.warning(f"Failed to list all libs for fuzzy search. Exit: {plat_retcode}. Stderr: {plat_stderr}")
                fuzzy_output_section.append("(Failed to retrieve local library list for fuzzy search)")

        except Exception as e:
            log.error(f"Error during fuzzy platform lib search: {e}")
            fuzzy_output_section.append(f"(Error during fuzzy search: {type(e).__name__})")

        # Format fuzzy results
        if fuzzy_matches:
            fuzzy_results_found = True
            fuzzy_matches.sort(key=lambda x: x["score"], reverse=True)
            limited_fuzzy_matches = fuzzy_matches[:limit]
            for match in limited_fuzzy_matches:
                include_str = ", ".join(match['includes']) if match['includes'] else "(none listed)"
                fuzzy_output_section.append(f"- Name: {match['name']} (Score: {match['score']})")
                fuzzy_output_section.append(f"    Includes: {include_str}")
            if len(fuzzy_matches) > limit:
                fuzzy_output_section.append(f"... (truncated to {limit} fuzzy matches)")
            log.info(f"Fuzzy search found {len(fuzzy_matches)} potential platform library matches for '{library_name}'.")
        # Add "no results" message only if no error occurred during listing/parsing
        elif not any("(Failed" in line or "(Error" in line for line in fuzzy_output_section):
            fuzzy_output_section.append("(No relevant platform libraries found)")
    else:
        fuzzy_output_section.append("(Fuzzy search disabled - 'thefuzz' library not installed)")

    final_output_lines.extend(fuzzy_output_section)

    # Final message if nothing was found anywhere
    if not online_results_found and not fuzzy_results_found:
        no_results_msg = f"No libraries found matching '{library_name}' online or in local platform libraries."
        log.info(no_results_msg)
        return no_results_msg

    return "\n".join(final_output_lines).strip()


@mcp.tool()
async def lib_install(library_name: str) -> str:
    """
    Installs or updates an Arduino library from the official Library Manager index.
    Specify the library name exactly as found using 'lib_search'. You can optionally
    specify a version using the format 'LibraryName@Version' (e.g., "FastLED@3.5.0").
    If no version is specified, the latest version is installed.

    Args:
        library_name: The exact name of the library to install, optionally with a version.
                      (e.g., "FastLED", "DHT sensor library", "Adafruit GFX Library@2.5.7").

    Returns:
        A success message indicating installation or update status. Includes a hint
        to use 'list_library_examples' after successful installation.

    Raises:
        ValueError: If library_name is empty.
        FileNotFoundError: If the specified library name (or version) is not found in the index.
        Exception: For other installation errors (e.g., network issues, conflicts,
                   permission errors writing to the library directory).
    """
    log.info(f"Tool Call: lib_install(library_name='{library_name}')")
    if not library_name:
        raise ValueError("Library name cannot be empty.")

    install_cmd_args = ["lib", "install", library_name]
    install_success_message = ""
    try:
        # Run command, check=True will raise specific errors on failure
        install_stdout, install_stderr, _ = await _run_arduino_cli_command(install_cmd_args, check=True)

        # Parse output for success confirmation (even though check=True handles errors)
        install_output = (install_stdout or install_stderr).strip()
        log.info(f"Library install command output for '{library_name}':\n{install_output}")
        install_output_lower = install_output.lower()

        # Determine the outcome based on output messages
        if "already installed" in install_output_lower:
             if "updating" in install_output_lower or "updated" in install_output_lower:
                 install_success_message = f"Library '{library_name}' was already installed and has been updated."
             else:
                 install_success_message = f"Library '{library_name}' is already installed at the specified/latest version."
        elif "successfully installed" in install_output_lower or "downloaded" in install_output_lower:
            install_success_message = f"Successfully installed/updated library '{library_name}'."
        else:
            # Fallback success message if output isn't recognized but command didn't fail
            install_success_message = f"Library install command finished successfully for '{library_name}'. Check logs for details."
            log.warning(f"Unrecognized success message from lib install: {install_output}")

        # Add hint about examples
        # Extract base library name if version was specified
        base_lib_name = library_name.split('@')[0]
        install_success_message += f"\nYou can now use 'list_library_examples' for '{base_lib_name}' to see available examples."
        log.info(install_success_message)
        return install_success_message

    except FileNotFoundError as e:
        # Specifically catch FileNotFoundError which _run_arduino_cli_command raises for "library not found"
        log.error(f"Library install failed: '{library_name}' not found in index. {e}")
        raise FileNotFoundError(f"Install failed: Library '{library_name}' not found in the index. Use 'lib_search' to find the correct name/version.") from e
    except (PermissionError, Exception) as e:
        # Catch other errors like permission issues writing to lib folder
        log.exception(f"Library install failed for '{library_name}'")
        raise Exception(f"Library install failed for '{library_name}': {type(e).__name__}: {e}") from e


@mcp.tool()
async def list_library_examples(library_name: str) -> str:
    """
    Lists the available example sketches provided by a specific *installed* Arduino library.

    Args:
        library_name: The exact name of the INSTALLED library (e.g., "FastLED", "DHT sensor library").
                      Use 'lib_search' to find names, and 'lib_install' to install them first.

    Returns:
        A formatted string listing the examples, including their full, resolved paths.
        Returns a specific message if the library is not found among installed libraries
        or if it contains no examples.

    Raises:
        ValueError: If library_name is empty.
        FileNotFoundError: If the specified library is not found among installed libraries.
        Exception: If the command fails for other reasons (e.g., corrupted index, permission issues).
    """
    log.info(f"Tool Call: list_library_examples(library_name='{library_name}')")
    if not library_name:
        raise ValueError("Library name cannot be empty.")

    examples_cmd_args = ["lib", "examples", library_name]
    try:
        # Don't check=True initially to handle "not found" / "no examples" gracefully
        examples_stdout, examples_stderr, examples_retcode = await _run_arduino_cli_command(
            examples_cmd_args, check=False
        )
        combined_output = (examples_stdout + "\n" + examples_stderr).strip()
        combined_output_lower = combined_output.lower()

        # Handle command failure or specific "not found" messages
        if examples_retcode != 0:
            if "library not found" in combined_output_lower:
                log.warning(f"Library '{library_name}' not found when listing examples.")
                raise FileNotFoundError(f"Library '{library_name}' not found among installed libraries. Use 'lib_install' first.")
            # Handle "no examples" message even if retcode is non-zero (can happen)
            elif "no examples found" in combined_output_lower:
                 log.info(f"No examples found for library '{library_name}' (reported by command failure).")
                 return f"No examples found for library '{library_name}'."
            else:
                log.error(f"Failed to list examples for library '{library_name}'. Exit: {examples_retcode}. Output: {combined_output}")
                raise Exception(f"Failed to list examples for '{library_name}'. Error: {combined_output}")

        # Handle "no examples" message if command succeeded
        if "no examples found" in combined_output_lower:
            log.info(f"No examples found for library '{library_name}' (command succeeded).")
            return f"No examples found for library '{library_name}'."

        # Clean ANSI codes from stdout before parsing paths
        cleaned_stdout = ANSI_ESCAPE_RE.sub('', examples_stdout).strip()
        log.debug(f"Cleaned 'lib examples' stdout for '{library_name}':\n{cleaned_stdout}")

        if not cleaned_stdout:
            log.warning(f"Command 'lib examples {library_name}' succeeded but produced empty stdout after cleaning.")
            return f"Command succeeded, but no example paths were listed for library '{library_name}'."

        # Parse the cleaned output for example paths
        log.info(f"Parsing library examples output for '{library_name}'.")
        example_paths: List[Path] = []
        # Regex to capture paths, potentially handling variations like leading spaces/hyphens
        # Assumes paths don't contain newline characters.
        example_line_pattern = r"^\s*[-*]?\s*(.+?)\s*$"
        processed_lines = set() # Avoid processing duplicate lines if CLI output is weird

        for line in cleaned_stdout.splitlines():
            line_strip = line.strip()
            if not line_strip or line_strip in processed_lines:
                continue
            processed_lines.add(line_strip)

            match = re.match(example_line_pattern, line_strip)
            if match:
                path_str = match.group(1).strip()
                if not path_str:
                    log.warning(f"Parsed an empty path string from line: '{line_strip}'")
                    continue
                try:
                    # Resolve the path to check existence and get absolute path
                    # Allow non-existent paths initially, check later
                    example_path = Path(path_str).resolve(strict=False)
                    exists, _, _ = await _async_file_op(_sync_check_exists, example_path)
                    if exists:
                        example_paths.append(example_path)
                    else:
                        log.warning(f"Path listed by 'lib examples' resolved to '{example_path}' but does not exist. Skipping.")
                except (ValueError, OSError) as path_err:
                     log.warning(f"Could not process or resolve path from 'lib examples' output: '{path_str}'. Error: {path_err}")
                except Exception as path_err: # Catch unexpected errors
                     log.warning(f"Unexpected error processing path '{path_str}': {path_err}")

        # Format the final output
        if example_paths:
            examples_info = f"Examples for library '{library_name}':\n  Example Sketch Paths:"
            for full_path in sorted(example_paths):
                examples_info += f"\n    - {full_path}"
            examples_info += "\n\n(Use 'read_file' with the full path to view an example's code.)"
            log.info(f"Found {len(example_paths)} examples for '{library_name}'.")
            return examples_info.strip()
        else:
            log.warning(f"Could not parse any valid example paths from 'lib examples {library_name}' output, although command succeeded. Cleaned output:\n{cleaned_stdout}")
            return f"Command succeeded, but failed to parse valid example paths for '{library_name}'. Please check server logs."

    except FileNotFoundError as e:
        # Re-raise specific FileNotFoundError if caught
        raise e
    except Exception as e:
        log.exception(f"Error occurred while trying to list examples for '{library_name}'.")
        raise Exception(f"An error occurred retrieving examples for '{library_name}': {type(e).__name__}") from e


@mcp.tool()
async def read_file(filepath: str) -> str:
    """
    Reads the content of a specified file. Operates within the user's home directory ('~').

    *** Special Sketch Handling ***
    If the filepath points specifically to the main '.ino' file within a standard sketch
    directory structure (e.g., '~/Documents/Arduino_MCP_Sketches/MySketch/MySketch.ino'),
    this tool reads and concatenates the content of ALL '.ino' and '.h' files found within that
    SAME sketch directory ('~/Documents/Arduino_MCP_Sketches/MySketch/'), providing the
    complete code context for that sketch. The files are concatenated in alphabetical order.

    For any other file path (even other files within a sketch directory, or files outside
    the sketch base), it reads only the content of that single specified file.

    Args:
        filepath: The path to the file to read (absolute, relative to CWD, or using '~').
                  Must resolve to a path within the user's home directory.

    Returns:
        The content of the file (or combined content of sketch files) as a string.

    Raises:
        ValueError: If the path string is invalid.
        FileNotFoundError: If the specified file (or initial .ino file for sketch read) is not found.
        IsADirectoryError: If the path points to a directory (and doesn't trigger sketch read).
        PermissionError: If file permissions prevent reading or path is outside home directory.
        Exception: For other I/O errors.
    """
    log.info(f"Tool Call: read_file(filepath='{filepath}')")
    resolved_path: Optional[Path] = None
    try:
        # Validate that the path resolves within the user's home directory
        resolved_path = await _resolve_and_validate_path(
            filepath,
            allowed_bases=[USER_HOME], # Restrict reads to user's home
            check_existence=True # Ensure the target exists before proceeding
        )

        # Check if it's a file (check_existence=True already did this)
        # exists, is_file, is_dir = await _async_file_op(_sync_check_exists, resolved_path)
        # if not exists: raise FileNotFoundError(f"File not found: {resolved_path}") # Redundant due to check_existence=True
        if not resolved_path.is_file():
             raise IsADirectoryError(f"Path exists but is a directory, not a file: {resolved_path}")

        # --- Special Sketch Handling Logic ---
        is_main_ino_in_sketch = False
        sketch_dir_to_read: Optional[Path] = None
        if resolved_path.suffix.lower() == ".ino":
            try:
                # Check if it's inside SKETCHES_BASE_DIR and parent dir name matches stem
                if resolved_path.is_relative_to(SKETCHES_BASE_DIR) and resolved_path.parent.name == resolved_path.stem:
                    is_main_ino_in_sketch = True
                    sketch_dir_to_read = resolved_path.parent
            except ValueError:
                pass # Not relative to sketches base, treat as normal file

        if is_main_ino_in_sketch and sketch_dir_to_read:
            sketch_name = sketch_dir_to_read.name
            log.info(f"Detected read for main sketch file '{resolved_path.name}'. Reading all .ino/.h files in directory: {sketch_dir_to_read}")
            try:
                all_items = await _async_file_op(_sync_list_dir, sketch_dir_to_read)
                files_to_combine: List[Path] = []
                for item_name in all_items:
                     item_path = sketch_dir_to_read / item_name
                     # Check if it's a file and ends with .ino or .h
                     exists, is_file, _ = await _async_file_op(_sync_check_exists, item_path)
                     if exists and is_file and item_name.lower().endswith((".ino", ".h")):
                         files_to_combine.append(item_path)

                if not files_to_combine:
                    # Should not happen if the main .ino exists, but handle defensively
                    log.warning(f"Main .ino '{resolved_path.name}' exists, but no .ino/.h files found to combine in {sketch_dir_to_read}. Reading only main file.")
                    content = await _async_file_op(_sync_read_file, resolved_path)
                    return content

                # Sort files alphabetically for consistent order
                files_to_combine.sort()

                combined_content_parts = [f"// --- Combined content of sketch: {sketch_name} ---"]
                # Read files concurrently
                read_tasks = {fp: _async_file_op(_sync_read_file, fp) for fp in files_to_combine}
                results = await asyncio.gather(*read_tasks.values(), return_exceptions=True)

                # Combine results
                for i, fp in enumerate(files_to_combine):
                    result = results[i]
                    combined_content_parts.append(f"\n\n// --- File: {fp.name} ---")
                    if isinstance(result, Exception):
                        log.error(f"Error reading file {fp} during sketch combine: {result}")
                        combined_content_parts.append(f"// Error reading file: {type(result).__name__}: {result}")
                    elif isinstance(result, str):
                        combined_content_parts.append(result)
                    else: # Should not happen
                         log.error(f"Unexpected result type reading {fp}: {type(result)}")
                         combined_content_parts.append(f"// Error: Unexpected read result type {type(result)}")

                final_output = "\n".join(combined_content_parts)
                log.info(f"Read and combined {len(files_to_combine)} .ino/.h files from {sketch_dir_to_read} ({len(final_output)} chars)")
                return final_output

            except Exception as list_read_err:
                # Fallback to reading only the requested file if combining fails
                log.error(f"Error listing/reading files in sketch directory {sketch_dir_to_read}: {list_read_err}. Falling back to reading only {resolved_path.name}.")
                content = await _async_file_op(_sync_read_file, resolved_path)
                log.info(f"Read single file (fallback after combine error): {resolved_path} ({len(content)} chars)")
                return content
        else:
            # Standard single file read
            content = await _async_file_op(_sync_read_file, resolved_path)
            log.info(f"Read single file: {resolved_path} ({len(content)} chars)")
            return content

    except (FileNotFoundError, ValueError, IsADirectoryError, PermissionError) as e:
        log.error(f"Read file error for '{filepath}' (resolved to {resolved_path}): {type(e).__name__}: {e}")
        raise e # Re-raise specific, expected errors
    except Exception as e:
        error_msg = f"Unexpected error reading '{filepath}' (resolved to {resolved_path}): {type(e).__name__}: {e}"
        log.exception(error_msg) # Log with traceback
        raise Exception(error_msg) from e


@mcp.tool()
async def write_file(filepath: str, content: str, board_fqbn: str = DEFAULT_FQBN) -> str:
    """
    Writes content to a specified file, overwriting it if it exists.

    *** Security Restrictions & Warnings ***
    - Writing '.ino' files is RESTRICTED to the designated sketch directory structure
      ('~/Documents/Arduino_MCP_Sketches/sketch_name/'). The filename must match the
      directory name (e.g., .../MySketch/MySketch.ino).
    - Writing all other file types is RESTRICTED to the user's home directory ('~').
    - This operation OVERWRITES existing files without confirmation. Use with caution.

    *** Automatic Compilation Trigger ***
    If the filepath points to a main '.ino' file within the standard sketch
    directory structure (as described above), this tool will automatically attempt
    to compile the sketch AFTER writing the file. It uses the provided 'board_fqbn'
    (defaulting to 'arduino:avr:uno' if not specified). The compilation result
    (success or failure message) will be appended to the return string.

    Args:
        filepath: The path where the file should be written (absolute, relative to CWD, or ~).
                  Must resolve to a path within the allowed directories based on file type.
        content: The text content to write to the file.
        board_fqbn: Required for automatic compilation when writing a main sketch '.ino' file.
                    Defaults to 'arduino:avr:uno'. Provide the correct FQBN for the
                    target board if different. Format must be 'vendor:arch:board'.

    Returns:
        A string indicating success of the write operation, potentially followed by
        the result of the automatic compilation attempt for main sketch .ino files.

    Raises:
        ValueError: If path or FQBN format is invalid.
        PermissionError: If writing is not allowed at the location (outside restricted areas).
        IsADirectoryError: If the path points to an existing directory.
        FileNotFoundError: If the parent directory for a new file cannot be created.
        Exception: For compilation errors during auto-compile or other I/O errors.
    """
    log.info(f"Tool Call: write_file(filepath='{filepath}', fqbn='{board_fqbn}')")
    resolved_path: Optional[Path] = None
    is_main_ino_in_sketch = False
    sketch_dir_for_compile: Optional[Path] = None
    allowed_bases: List[Path]

    try:
        # Determine allowed base directory based on file type and path structure
        is_potential_ino = filepath.lower().endswith(".ino")
        path_obj_pre_resolve = Path(filepath).expanduser() # For structure check

        if is_potential_ino:
            # Check if it looks like a *main* sketch file (parent dir name == stem)
            # AND is directly under SKETCHES_BASE_DIR
            parent_dir = path_obj_pre_resolve.parent
            if parent_dir.parent == SKETCHES_BASE_DIR and parent_dir.name == path_obj_pre_resolve.stem:
                allowed_bases = [SKETCHES_BASE_DIR] # Allow writing within any sketch dir
                is_main_ino_in_sketch = True # Mark for potential auto-compile
                sketch_dir_for_compile = parent_dir.resolve(strict=False) # Use resolved parent for compile
                log.debug(f"Path '{filepath}' identified as main sketch .ino. Allowed base: {allowed_bases}")
            else:
                # It's an .ino file but not in the standard sketch structure, restrict to home
                allowed_bases = [USER_HOME]
                log.debug(f"Path '{filepath}' is .ino but not main sketch file. Allowed base: {allowed_bases}")
        else:
            # Not an .ino file, restrict to home directory
            allowed_bases = [USER_HOME]
            log.debug(f"Path '{filepath}' is not .ino. Allowed base: {allowed_bases}")

        # Validate and resolve the path against the determined allowed bases
        resolved_path = await _resolve_and_validate_path(
            filepath,
            allowed_bases=allowed_bases,
            check_existence=False # Don't require existence for writing
        )

        # Explicitly re-check if it's the main sketch file *after* resolution
        # This handles cases where symlinks might change the structure
        if resolved_path.suffix.lower() == ".ino":
             try:
                 if resolved_path.is_relative_to(SKETCHES_BASE_DIR) and resolved_path.parent.name == resolved_path.stem:
                     is_main_ino_in_sketch = True
                     sketch_dir_for_compile = resolved_path.parent # Update compile dir based on resolved path
                 else: # If resolved path is not main sketch, disable compile trigger
                      is_main_ino_in_sketch = False
                      sketch_dir_for_compile = None
             except ValueError: # Not relative to sketches base
                 is_main_ino_in_sketch = False
                 sketch_dir_for_compile = None
        else: # Not an ino file after resolution
             is_main_ino_in_sketch = False
             sketch_dir_for_compile = None


        # Validate FQBN format if provided (even if not used for auto-compile)
        if board_fqbn and (":" not in board_fqbn or len(board_fqbn.split(':')) < 3):
             raise ValueError(f"Invalid board_fqbn format provided: '{board_fqbn}'. Must be 'vendor:arch:board'.")

        # Check if the resolved path points to an existing directory
        exists, _, is_dir = await _async_file_op(_sync_check_exists, resolved_path)
        if exists and is_dir:
            raise IsADirectoryError(f"Cannot write file content: path '{resolved_path}' points to an existing directory.")

        # Perform the write operation
        log.warning(f"Attempting to write/overwrite file: {resolved_path}")
        await _async_file_op(_sync_write_file, resolved_path, content)
        write_success_msg = f"Successfully wrote {len(content)} characters to file '{resolved_path}'."
        log.info(write_success_msg)

        # --- Automatic Compilation ---
        compile_result_msg = ""
        if is_main_ino_in_sketch and sketch_dir_for_compile:
            effective_fqbn = board_fqbn # Use provided (and validated) or default FQBN
            log.info(f"Main sketch .ino write detected. Triggering auto-compile for sketch '{sketch_dir_for_compile.name}' with FQBN '{effective_fqbn}'.")
            # Use a consistent build path for potential caching
            build_path_abs = (BUILD_TEMP_DIR / f"{sketch_dir_for_compile.name}_write_{effective_fqbn.replace(':', '_')}").resolve(strict=False)
            try:
                # _execute_compile handles mkdir for build_path_abs
                compile_success_message = await _execute_compile(sketch_dir_for_compile, build_path_abs, effective_fqbn)
                # Append success message from compile (includes size info)
                compile_result_msg = f"\nAutomatic compilation using FQBN '{effective_fqbn}' successful: {compile_success_message.replace('Compilation successful.', '').strip()}"
                log.info(f"Automatic compilation after write succeeded for {resolved_path}")
            except Exception as compile_err:
                # Append failure message
                log.error(f"Automatic compilation after write FAILED for {resolved_path} using FQBN '{effective_fqbn}': {compile_err}")
                compile_result_msg = f"\nWrite succeeded, but automatic compilation using FQBN '{effective_fqbn}' FAILED: {compile_err}"

        return write_success_msg + compile_result_msg

    except (ValueError, IsADirectoryError, PermissionError, FileNotFoundError) as e:
        log.error(f"Write file error for '{filepath}' (resolved to {resolved_path}): {type(e).__name__}: {e}")
        raise e # Re-raise specific, expected errors
    except Exception as e:
        error_msg = f"Unexpected error writing to '{filepath}' (resolved to {resolved_path}): {type(e).__name__}: {e}"
        log.exception(error_msg) # Log with traceback
        raise Exception(error_msg) from e


# @mcp.tool()
async def rename_file(old_path: str, new_path: str) -> str:
    """
    Renames or moves a file or directory.

    *** Security Restrictions & Warnings ***
    - Operation is RESTRICTED to occur entirely within the user's home directory ('~').
      Both the source (old_path) and destination (new_path) must resolve within home.
    - Use with EXTREME CAUTION, especially when moving directories, as this can
      restructure user files and is hard to undo.
    - This operation will FAIL if the destination path already exists.

    Args:
        old_path: The current path of the file or directory (absolute, relative to CWD, or ~).
                  Must resolve to a path within the user's home directory.
        new_path: The desired new path for the file or directory (absolute, relative to CWD, or ~).
                  Must resolve to a path within the user's home directory.

    Returns:
        A success message confirming the rename/move operation.

    Raises:
        ValueError: If paths are invalid.
        FileNotFoundError: If the old_path does not exist.
        FileExistsError: If the new_path already exists.
        PermissionError: If permissions prevent the operation or paths are outside home directory.
        Exception: For other I/O errors.
    """
    log.info(f"Tool Call: rename_file(old='{old_path}', new='{new_path}')")
    resolved_old_path: Optional[Path] = None
    resolved_new_path: Optional[Path] = None
    try:
        # Validate both paths must be within USER_HOME
        allowed_bases = [USER_HOME]
        resolved_old_path = await _resolve_and_validate_path(
            old_path,
            allowed_bases=allowed_bases,
            check_existence=True # Source must exist
        )
        resolved_new_path = await _resolve_and_validate_path(
            new_path,
            allowed_bases=allowed_bases,
            check_existence=False # Destination must NOT exist
        )

        # Check if destination already exists (double check after resolve)
        new_exists, _, _ = await _async_file_op(_sync_check_exists, resolved_new_path)
        if new_exists:
            raise FileExistsError(f"Destination path already exists: {resolved_new_path}")

        # Perform the rename/move
        log.warning(f"Attempting to rename/move '{resolved_old_path}' to '{resolved_new_path}'. Use with caution.")
        await _async_file_op(_sync_rename_file, resolved_old_path, resolved_new_path)
        success_msg = f"Successfully renamed/moved '{resolved_old_path}' to '{resolved_new_path}'."
        log.info(success_msg)
        return success_msg

    except (FileNotFoundError, FileExistsError, ValueError, PermissionError) as e:
        log.error(f"Rename file error '{old_path}' -> '{new_path}': {type(e).__name__}: {e}")
        raise e # Re-raise specific, expected errors
    except Exception as e:
        error_msg = f"Unexpected error renaming '{old_path}' (-> {resolved_old_path}) to '{new_path}' (-> {resolved_new_path}): {type(e).__name__}: {e}"
        log.exception(error_msg) # Log with traceback
        raise Exception(error_msg) from e


# @mcp.tool()
async def remove_file(filepath: str) -> str:
    """
    Removes (deletes) a specified file.

    *** Security Restrictions & Warnings ***
    - Operation is RESTRICTED to files within the user's home directory ('~').
    - This operation is IRREVERSIBLE and permanently deletes the file.
    - This tool WILL NOT remove directories, only files.
    - Use with EXTREME CAUTION.

    Args:
        filepath: The path to the file to be deleted (absolute, relative to CWD, or ~).
                  Must resolve to a file within the user's home directory.

    Returns:
        A success message confirming the file removal.

    Raises:
        ValueError: If the path is invalid.
        FileNotFoundError: If the file does not exist at the specified path.
        IsADirectoryError: If the path points to a directory instead of a file.
        PermissionError: If permissions prevent deletion or path is outside home directory.
        Exception: For other I/O errors.
    """
    log.info(f"Tool Call: remove_file(filepath='{filepath}')")
    resolved_path: Optional[Path] = None
    try:
        # Validate path is within home and exists
        resolved_path = await _resolve_and_validate_path(
            filepath,
            allowed_bases=[USER_HOME],
            check_existence=True
        )

        # Ensure it's a file, not a directory (check_existence validated it exists)
        if not resolved_path.is_file():
             raise IsADirectoryError(f"Cannot remove: Path points to a directory, not a file: {resolved_path}")

        # Perform the removal
        log.warning(f"Attempting to permanently remove file: {resolved_path}. This is irreversible.")
        await _async_file_op(_sync_remove_file, resolved_path)
        success_msg = f"Successfully removed file: {resolved_path}"
        log.info(success_msg)
        return success_msg

    except (ValueError, FileNotFoundError, IsADirectoryError, PermissionError) as e:
        log.error(f"Remove file error for '{filepath}' (resolved to {resolved_path}): {type(e).__name__}: {e}")
        raise e # Re-raise specific, expected errors
    except Exception as e:
        error_msg = f"Unexpected error removing '{filepath}' (resolved to {resolved_path}): {type(e).__name__}: {e}"
        log.exception(error_msg) # Log with traceback
        raise Exception(error_msg) from e

# --- WireViz Tools ---
# Refactored: Instructions are now provided via a resource
# @mcp.resource("wireviz://instructions")
# async def get_wireviz_instructions_resource() -> str:

async def set_openai_api_key_tool(api_key: str) -> str:
    """
    Sets the OpenAI API key for GPT-4.1 calls at runtime.
    """
    return set_openai_api_key(api_key)

@mcp.tool()
async def generate_circuit_diagram_from_description(
    description: str,
    sketch_name: str = "",
    output_filename_base: str = "circuit"
) -> List[Union[types.TextContent, types.ImageContent]]:
    """
    Generates a circuit diagram PNG from a natural language description of components and connections.
    Uses OpenAI GPT-4.1 to convert the description and the WireViz guide into valid YAML, then generates the PNG.
    Returns both the generated YAML and the PNG image.
    """
    if not description or not description.strip():
        raise ValueError("Description cannot be empty.")
    # Get OpenAI/OpenRouter API key (robust lookup)
    api_key = _OPENAI_API_KEY or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("OpenAI/OpenRouter API key is not set. Use set_openai_api_key_tool() or set the OPENAI_API_KEY or OPENROUTER_API_KEY environment variable.")

    # Get the WireViz guide
    wireviz_guide = await getWirevizInstructions()
    prompt = (
        "You are a WireViz YAML expert. Convert the following user description into a valid WireViz YAML file "
        "suitable for generating a circuit diagram. Follow the provided guidelines and examples. "
        "Return ONLY the YAML, and enclose it between triple backticks as a YAML code block, like this: ```yaml ... ``` (do not add any explanation or text outside the code block).\n\n" +
        "WireViz YAML Guidelines:\n" + wireviz_guide + "\n\n" +
        "User Description:\n" + description.strip()
    )
    # --- Provider detection and setup ---
    from openai import OpenAI as OpenAIClient
    import re
    # Heuristic: OpenRouter keys usually start with 'sk-or-' or 'sk-proj-'; OpenAI with 'sk-...'
    is_openrouter = api_key.startswith("sk-or-") or api_key.startswith("sk-proj-") or os.environ.get("OPENAI_PROVIDER", "").lower() == "openrouter"
    if is_openrouter:
        base_url = "https://openrouter.ai/api/v1"
        model = "openai/gpt-4.1-2025-04-14"
        # Optionally allow headers from env/config
        extra_headers = {}
        referer = os.environ.get("OPENROUTER_REFERER")
        xtitle = os.environ.get("OPENROUTER_XTITLE")
        if referer:
            extra_headers["HTTP-Referer"] = referer
        if xtitle:
            extra_headers["X-Title"] = xtitle
    else:
        base_url = None
        model = "gpt-4.1-2025-04-14"
        extra_headers = None
    client = OpenAIClient(api_key=api_key, base_url=base_url) if base_url else OpenAIClient(api_key=api_key)
    # --- End Provider setup ---
    try:
        def do_completion():
            kwargs = dict(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=1800,
            )
            if extra_headers:
                kwargs["extra_headers"] = extra_headers
            return client.chat.completions.create(**kwargs)
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            do_completion
        )
        llm_output = response.choices[0].message.content.strip()
        # --- Robust YAML code block extraction ---
        import re
        code_block_pattern = re.compile(r"```(?:yaml)?\s*([\s\S]+?)\s*```", re.IGNORECASE)
        match = code_block_pattern.search(llm_output)
        if match:
            yaml_content = match.group(1).strip()
        else:
            log.warning("No YAML code block found in LLM response. Using full response as YAML.")
            yaml_content = llm_output
    except Exception as e:
        log.error(f"OpenAI GPT-4.1 call failed: {e}")
        raise Exception(f"OpenAI GPT-4.1 call failed: {e}")
    # Generate diagram from YAML
    try:
        result = await generate_diagram_from_yaml(
            yaml_content=yaml_content,
            sketch_name=sketch_name,
            output_filename_base=output_filename_base
        )
        # Prepend the YAML content as a TextContent result
        if isinstance(result, list):
            yaml_msg = types.TextContent(type="text", text=f"Generated WireViz YAML:\n\n{yaml_content}")
            return [yaml_msg] + result
        else:
            return result
    except Exception as e:
        log.error(f"Failed to generate diagram from YAML: {e}")
        raise

# @mcp.resource("wireviz://instructions")
async def getWirevizInstructions() -> str:
    """
    Provides basic instructions and links on how to use WireViz YAML syntax
    for generating circuit diagrams. USE IT BEFORE generate_diagram_from_yaml.
    """
    log.info("Retrieving wireviz instructions")
    instructions = """
Arduino Connection Diagram YAML Guidelines for WireViz

Use these rules to create valid YAML files:

1. CONNECTORS
	•	List every component: Include Arduino, sensors, LEDs, resistors, and power supplies.
	•	Detail pins: Use pinlabels to list pins in order.
	•	Annotate: Define the type and add notes as needed.
	•	Simple components: For resistors, capacitors, and non-LED diodes, use:

ComponentName:
  style: simple
  type: Resistor
  category: ferrule
  subtype: [resistance] Ω



2. CABLES
	•	Define cable bundles: Specify colors, category, and display options such as show_name, show_wirecount, and show_wirenumbers.

3. CONNECTIONS
	•	Reference only defined components: Every connection must use components from the CONNECTORS section.
	•	Match pin counts: Each connection set must list the same number of pins/wires.
	•	Group logically: Organize connections by component.
	•	Use position numbers only: Use numerals (e.g., [1], [2-4]) rather than pin names like “GND” or “VCC”. (Do not confuse these with digital labels like “D2”.)
	•	Ferrule notation: Use --> for direct connections with ferrules.
	•	Order matters:
	•	List ferrule-involved connections first.
	•	Do not end any connection set containing a ferrule.
	•	Follow with connections that have no ferrules.
	•	Simplify: Combine similar connections when possible.

4. METADATA
	•	Include keys: description, author, and date.

⸻

Examples

Example 1: Arduino with SSD1306 OLED Display and Push Buttons

connectors:
  Arduino Uno:
    pinlabels: ["5V", "GND", "D2", "D3", "A4", "A5"]
    notes: Main control board
  SSD1306 OLED Display:
    pinlabels: ["VCC", "GND", "SCL", "SDA"]
    notes: Display module
  Push_Button1:
    pinlabels: ["Terminal1", "Terminal2"]
    notes: First push button
  Push_Button2:
    pinlabels: ["Terminal1", "Terminal2"]
    notes: Second push button
  R_10k1:
    style: simple
    type: Resistor
    category: ferrule
    subtype: 10k Ω
  R_10k2:
    style: simple
    type: Resistor
    category: ferrule
    subtype: 10k Ω

cables:
  W_SSD1306_OLED:
    colors: [RD, BK, TQ, VT]
    category: bundle
    show_name: false
    show_wirecount: false
    show_wirenumbers: false
  W_Push_Button1:
    colors: [RD, BK]
    category: bundle
    show_name: false
    show_wirecount: false
    show_wirenumbers: false
  W_Push_Button2:
    colors: [RD, BK]
    category: bundle
    show_name: false
    show_wirecount: false
    show_wirenumbers: false

connections:
  - # First push button connections
    - Arduino Uno: [3]
    - W_Push_Button1: [1]
    - Push_Button1: [1]
  - # First push button GND connection
    - Arduino Uno: [2]
    - W_Push_Button1: [2]
    - Push_Button1: [2]
  - # First push button pull-up resistor
    - Arduino Uno: [1]
    - -->
    - R_10k1.
    - W_Push_Button1: [1]
  - # Second push button connections
    - Arduino Uno: [4]
    - W_Push_Button2: [1]
    - Push_Button2: [1]
  - # Second push button GND connection
    - Arduino Uno: [2]
    - W_Push_Button2: [2]
    - Push_Button2: [2]
  - # Second push button pull-up resistor
    - Arduino Uno: [1]
    - -->
    - R_10k2.
    - W_Push_Button2: [1]
  - # SSD1306 OLED Display connections
    - Arduino Uno: [1, 2, 6, 5]
    - W_SSD1306_OLED: [1-4]
    - SSD1306 OLED Display: [1-4]

metadata:
  description: "Wiring diagram for Arduino Uno with SSD1306 OLED Display and Push Buttons"
  author: "User"
  date: "2024-06-23"

Example 2: Ordering Connections

WRONG – Ending with a Ferrule:

connections:
 [...]
  -
    - MainBoard: [1-3]
    - Sensor_Cable: [1-3]
    - Sensor: [1-3]
  - # WRONG ORDER - END WITH FERRULES
    - MainBoard: [6]
    - LED_Cable: [4]
    - Resistor3.
    - -->
    - LED: [4]

metadata:
  description: [...]

CORRECT – Ending with Non-Ferrule Connection:

connections:
 [...]
  -
    - MainBoard: [6]
    - LED_Cable: [4]
    - Resistor3.
    - -->
    - LED: [4]
  - # CORRECT ORDER - END WITH SET WITHOUT FERRULES
    - MainBoard: [1-3]
    - Sensor_Cable: [1-3]
    - Sensor: [1-3]

Key Rule: The final connection in each set must not link to a ferrule.

⸻

Color Codes
	•	“WH”: white
	•	“BN”: brown
	•	“GN”: green
	•	“YE”: yellow
	•	“GY”: grey
	•	“PK”: pink
	•	“BU”: blue
	•	“RD”: red
	•	“BK”: black
	•	“VT”: violet
	•	“SR”: silver
	•	“GD”: gold
	•	“OG”: orange

⸻

Checklist
	1.	Every component in CONNECTIONS must be defined in CONNECTORS.
	2.	Pin position numbers in CONNECTIONS must match the wire count.
	3.	All connections should use cables or the --> notation.
	4.	Do not make direct component-to-component connections without cables or ferrules.
	5.	List ferrule-dependent connections first.
	6.	Combine similar connections for simplicity.
	7.	End connection sets with components that are not ferrules or cables.
	8.	Use only components defined in the circuit; do not add new ones in CONNECTIONS.
	9.	Every connection set must reference the same number of wires.

"""
    return instructions.strip()

async def generate_diagram_from_yaml(
    yaml_content: str,
    sketch_name: str = "",  # Changed: now a string with a default of empty string
    output_filename_base: str = "circuit"
) -> List[Union[types.TextContent, types.ImageContent]]:
    """
    Generates a circuit diagram PNG from provided WireViz YAML content and returns
    the image data directly, along with a confirmation message.
    **After successful generation, attempts to open the new .png file in the default image viewer.**

    Runs the local 'wireviz' command to create the diagram files.

    Args:
        yaml_content: A string containing the complete WireViz YAML code.
        sketch_name: Optional. If provided (non-empty), output files are saved in this sketch's directory.
        output_filename_base: Optional. Base name for output files (default: "circuit").

    Returns:
        A list containing:
        - A TextContent object confirming success and file paths.
        - An ImageContent object with the base64 encoded PNG data.
        Or raises an error on failure.

    Raises:
        ValueError: If inputs are invalid.
        FileNotFoundError: If directories/executables are missing.
        PermissionError: If operations are not permitted.
        Exception: If wireviz fails or other errors occur.
    
    
    """
    log.info(f"Tool Call: generate_diagram_from_yaml(sketch_name='{sketch_name}', output_base='{output_filename_base}')")

    # Input validation
    if not yaml_content or not yaml_content.strip():
        raise ValueError("YAML content cannot be empty.")
    if ':' not in yaml_content or ('components:' not in yaml_content.lower() and 'connectors:' not in yaml_content.lower()):
        log.warning("Input does not strongly resemble WireViz YAML. Proceeding anyway.")
    if any(c in output_filename_base for c in ['/', '\\', '..']):
        raise ValueError("Output filename base invalid.")

    # Determine output directory
    output_directory: Path
    expected_png_path: Optional[Path] = None  # Define here for broader scope
    try:
        if sketch_name:
            if any(c in sketch_name for c in ['/', '\\', '..']):
                raise ValueError("Invalid sketch_name.")
            # Validate sketch_name resolves to a directory within SKETCHES_BASE_DIR
            sketch_dir_path = await _resolve_and_validate_path(
                str(SKETCHES_BASE_DIR / sketch_name),
                allowed_bases=[SKETCHES_BASE_DIR],
                check_existence=True  # Sketch directory must exist
            )
            if not sketch_dir_path.is_dir():  # Double check it's a directory
                raise FileNotFoundError(f"Specified sketch path is not a directory: {sketch_dir_path}")
            output_directory = sketch_dir_path
        else:
            # Create a timestamped directory within SKETCHES_BASE_DIR
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            default_dir_name = f"wireviz_output_{timestamp}"
            # Validate the *intended* path is within the allowed base
            output_directory = await _resolve_and_validate_path(
                str(SKETCHES_BASE_DIR / default_dir_name),
                allowed_bases=[SKETCHES_BASE_DIR],
                check_existence=False  # Don't require existence yet
            )
            # Create the directory
            await _async_file_op(_sync_mkdir, output_directory)
        log.info(f"Output directory set to: {output_directory}")
    except (ValueError, FileNotFoundError, PermissionError) as path_err:
        log.error(f"Failed to determine or create output directory: {path_err}")
        raise path_err
    except Exception as e:
        log.error(f"Unexpected error setting output directory: {e}")
        raise Exception(f"Failed to set output directory: {e}") from e

    # --- Save YAML ---
    yaml_filename = output_filename_base + ".yml"
    yaml_filepath = output_directory / yaml_filename
    try:
        await _async_file_op(_sync_write_file, yaml_filepath, yaml_content.strip())
        log.info(f"Saved provided WireViz YAML to: {yaml_filepath}")
    except Exception as save_err:
        log.error(f"Failed to save YAML file {yaml_filepath}: {save_err}")
        raise Exception(f"Failed to save provided YAML file: {save_err}") from save_err

    # --- Run WireViz ---
    try:
        wv_stdout, wv_stderr, wv_retcode = await _run_wireviz_command(yaml_filepath)
        if wv_retcode != 0:
            error_output = wv_stderr if wv_stderr else wv_stdout
            log.error(f"WireViz command failed (code {wv_retcode}): {error_output}")
            raise Exception(f"WireViz command failed (code {wv_retcode}): {error_output}")

        # --- Verify PNG Output and Read Data ---
        expected_png_filename = output_filename_base + ".png"
        expected_png_path = output_directory / expected_png_filename
        png_exists, _, _ = await _async_file_op(_sync_check_exists, expected_png_path)

        if png_exists:
            log.info(f"WireViz succeeded. Reading generated PNG: {expected_png_path}")
            try:
                # Read the binary data of the PNG
                png_data: bytes = await _async_file_op(_sync_read_binary_file, expected_png_path)
                # Encode it in base64
                base64_encoded_data = base64.b64encode(png_data).decode('ascii')

                # Prepare success message
                success_msg_text = f"Successfully generated circuit diagram: {expected_png_path}"
                try:  # List other generated files
                    generated_files = await _async_file_op(_sync_list_dir, output_directory)
                    other_files = [f for f in generated_files if f != yaml_filename and f != expected_png_filename]
                    if other_files:
                        success_msg_text += f"\nOther files generated in {output_directory}: {', '.join(other_files)}"
                except Exception as list_err:
                    log.warning(f"Could not list other generated files in {output_directory}: {list_err}")

                # --- Attempt to open the generated PNG file ---
                await _open_file_in_default_app(expected_png_path)
                # --- End open attempt ---

                # Return *both* the text message and the image data
                log.info(f"Successfully generated and encoded diagram from {yaml_filepath}")
                return [
                    types.TextContent(type="text", text=success_msg_text),
                    # types.ImageContent(type="image", data=base64_encoded_data, mimeType="image/png")
                ]

            except Exception as read_encode_err:
                log.error(f"WireViz ran, PNG exists at {expected_png_path}, but failed to read/encode it: {read_encode_err}")
                raise Exception(f"Failed to read or encode generated PNG: {read_encode_err}") from read_encode_err
        else:
            # PNG not found after successful wireviz run
            log.error(f"WireViz command succeeded but expected output file '{expected_png_path}' was not found.")
            wv_output_summary = (wv_stdout + "\n" + wv_stderr).strip()[:500]
            raise Exception(f"WireViz ran but the PNG file was not created. WireViz output: {wv_output_summary}")

    except (FileNotFoundError, PermissionError, Exception) as wv_err:
        log.error(f"Error executing WireViz or processing output: {wv_err}")
        # Re-raise specific errors if possible
        if isinstance(wv_err, (FileNotFoundError, PermissionError)):
             raise wv_err
        else:
             raise Exception(f"Failed to run WireViz or process output: {wv_err}") from wv_err

# ==============================================================================
# Main Execution Block
# ==============================================================================
def main():
    """Main entry point for running the server."""
    log.info("==================================================")
    log.info(" Starting Arduino & WireViz FastMCP Server (v2.3)") # Updated version
    log.info("==================================================")
    try:
        SKETCHES_BASE_DIR.mkdir(parents=True, exist_ok=True)
        BUILD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
        log.info("Core sketch/build directories verified/created.")
    except OSError as e:
         log.critical(f"CRITICAL ERROR: Could not create essential directories {SKETCHES_BASE_DIR} or {BUILD_TEMP_DIR}. Check permissions. Server cannot function correctly. Error: {e}")
         # Exit the server process immediately if essential directories fail
         sys.exit(1) # <--- Keep this call
    try:
         log.info("Initializing FastMCP server...")
         # Server instance 'mcp' should be defined globally in the script
         log.info(f"Running MCP server '{mcp.name}' via STDIO transport. Waiting for client connection...")
         mcp.run(transport='stdio') # Blocks here
         log.info("Client disconnected.")
    except KeyboardInterrupt:
         log.info("Server stopped by user (KeyboardInterrupt).")
    except Exception as e:
         log.exception("Server exited with an unhandled error:")
         sys.exit(1) # Also exit on other unhandled errors in main
    finally:
        log.info("Server shutdown sequence initiated.")
        log.info("Server shutdown complete.")
        log.info("==================================================")

# Keep this guard if you still want to run the script directly
if __name__ == "__main__":
    main()
