# MCP Arduino Server (mcp-arduino-server)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![PyPI version](https://img.shields.io/pypi/v/mcp-arduino-server.svg)](https://pypi.org/project/mcp-arduino-server/)

A FastMCP-powered bridge exposing `arduino-cli` functionality via the Model Context Protocol (MCP). Manage sketches, boards, libraries, files, plus generate WireViz schematics from YAML or natural language.

## Requirements

- **Python ≥3.10**
- **arduino-cli** in `PATH`
- **MCP SDK** (`mcp[cli]`)
- **WireViz** (optional; for diagram generation)
- **OPENAI_API_KEY** (for AI‑powered WireViz)
- **thefuzz[speedup]** (optional; enables fuzzy local library search)

## Installation

**From PyPI**:
```bash
pip install mcp-arduino-server
```

**From source**:
```bash
git clone https://github.com/Volt23/mcp-arduino-server.git
cd mcp-arduino-server
pip install .
```

## Configuration

Environment variables override defaults:

| Variable             | Default / Description                              |
|----------------------|-----------------------------------------------------|
| ARDUINO_CLI_PATH     | auto-detected                                       |
| WIREVIZ_PATH         | auto-detected                                       |
| MCP_SKETCH_DIR       | `~/Documents/Arduino_MCP_Sketches/`                 |
| LOG_LEVEL            | `INFO`                                              |
| OPENAI_API_KEY       | your OpenAI API key (required for AI‑powered WireViz)|
| OPENROUTER_API_KEY   | optional alternative to `OPENAI_API_KEY`            |
| ARDUINO_SERIAL_LOG_MAX_BYTES | max size per serial log file (bytes; 0 disables) |
| ARDUINO_SERIAL_LOG_ROTATE_COUNT | number of rotated serial logs to keep      |

## Quick Start

```bash
mcp-arduino-server
```

Server listens on STDIO for JSON-RPC MCP calls. Key methods:

### Sketches
- `create_new_sketch(name)`
- `list_sketches()`
- `read_file(path)`
- `write_file(path, content[, board_fqbn])` _(auto-compiles & opens `.ino`)_

### Build & Deploy
- `verify_code(sketch, board_fqbn)`
- `upload_sketch(sketch, port, board_fqbn)`

### Libraries
- `lib_search(name[, limit])`
- `lib_install(name)`
- `list_library_examples(name)`

### Boards
- `list_boards()`
- `board_search(query)`

### Serial Monitor
- `serial_monitor_start(port, baud, buffer_lines, log_to_file, ...)`
- `serial_monitor_read(monitor_id, lines)`
- `serial_monitor_list()`
- `serial_monitor_stop(monitor_id)`

### File Ops
- `rename_file(src, dest)`
- `remove_file(path)` _(destructive; operations sandboxed to home & sketch directories)_

### WireViz Diagrams
- `generate_circuit_diagram_from_description(desc, sketch="", output_base="circuit")` _(AI‑powered; requires `OPENAI_API_KEY`, opens PNG automatically)_

## MCP Client Configuration

To integrate with MCP clients (e.g., Claude Desktop), set your OpenAI API key in the environment (or alternatively `OPENROUTER_API_KEY` for OpenRouter):

```json
{
  "mcpServers": {
    "arduino": {
      "command": "/path/to/mcp-arduino-server",
      "args": [],
      "env": {
        "WIREVIZ_PATH": "/path/to/wireviz",
        "OPENAI_API_KEY": "<your-openai-api-key>"
      }
    }
  }
}
```

## Troubleshooting

- Set `LOG_LEVEL=DEBUG` for verbose logs.
- Verify file and serial-port permissions.
- Install missing cores: `arduino-cli core install <spec>`.
- Run `arduino-cli` commands manually to debug.

## License

MIT
