"""
Claude-Codex Bridge MCP Server

An intelligent bridge MCP server for orchestrating task delegation
between Claude and OpenAI Codex CLI.
"""

import asyncio
import json
import os
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from mcp.server.fastmcp import FastMCP

try:
    from .engine import DelegationDecisionEngine
except ImportError:
    # When running directly, use absolute imports
    from engine import DelegationDecisionEngine  # type: ignore[no-redef]

# Initialize FastMCP instance
mcp = FastMCP(
    name="claude-codex-bridge",
    instructions="""An intelligent MCP server that leverages Codex's exceptional
capabilities in code analysis, architectural planning, and complex problem-solving.

Codex excels at:
• Deep code comprehension and analysis
• Architectural design and system planning
• Breaking down complex problems into actionable steps
• Generating comprehensive test strategies
• Code review and optimization suggestions

Callers should assess each task's difficulty and set the
`task_complexity` parameter ("low", "medium", or "high") accordingly to
guide Codex's reasoning effort.

By default, operates in read-only mode for safety. Enable write mode with --allow-write
when you're ready to apply Codex's recommendations.""",
)

# Initialize Delegation Decision Engine
dde = DelegationDecisionEngine()

# Backward-compatible single-line delimiter; can be overridden via env var
# Default mirrors historical behavior used by older tests/clients
FINAL_OUTPUT_DELIMITER: str = os.environ.get(
    "FINAL_OUTPUT_DELIMITER", "=x=x=x=x=x=x=x="
)

# Write operations will be checked dynamically in codex_delegate function


async def invoke_codex_cli(
    prompt: str,
    working_directory: str,
    execution_mode: str,
    sandbox_mode: str,
    task_complexity: Literal["low", "medium", "high"] = "medium",
    allow_write: bool = True,
    timeout: int = 3600,  # 1 hour timeout
) -> Tuple[str, str]:
    """
    Asynchronously invoke Codex CLI and return its stdout and stderr.

    Args:
        prompt: The main instruction to send to Codex CLI
        working_directory: Codex working directory
        execution_mode: Codex CLI approval strategy mode
        sandbox_mode: Codex CLI sandbox strategy mode
        task_complexity: Desired model reasoning effort level (default: "medium")
        allow_write: Whether to allow file write operations
        timeout: Command timeout in seconds

    Returns:
        Tuple containing (stdout, stderr)

    Raises:
        RuntimeError: When Codex CLI execution fails
        asyncio.TimeoutError: When command times out
    """
    # Build base command
    command = ["codex", "exec"]

    # Always specify working directory (critical)
    command.extend(["-C", working_directory])

    # Configure file write permissions through sandbox_permissions
    if not allow_write:
        # Disable file operations by using empty sandbox_permissions
        command.extend(["-c", "sandbox_permissions=[]"])

    # Use convenience mode or specify parameters separately
    if (
        execution_mode == "on-failure"
        and sandbox_mode == "workspace-write"
        and allow_write
    ):
        # Use convenient --full-auto mode (only when write is allowed)
        command.append("--full-auto")
    else:
        # Specify sandbox mode only (approval mode not available for exec subcommand)
        command.extend(["-s", sandbox_mode])

    # Configure model reasoning effort based on task complexity
    command.extend(["-c", f'model_reasoning_effort="{task_complexity}"'])

    # Add delimiter to ensure any leading dashes in prompt
    # are treated as positional text, not CLI flags
    command.append("--")

    # Add prompt as final positional argument
    command.append(prompt)

    process = None
    try:
        # Execute subprocess asynchronously
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory,  # Also set as double protection
        )

        # Wait for process completion (with timeout)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        # Check exit code
        if process.returncode != 0:
            error_message = (
                stderr.decode("utf-8").strip() if stderr else "Unknown error"
            )
            raise RuntimeError(
                f"Codex CLI execution failed (exit code: {process.returncode}): "
                f"{error_message}"
            )

        return stdout.decode("utf-8"), stderr.decode("utf-8")

    except asyncio.TimeoutError:
        # Timeout handling
        if process is not None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        raise asyncio.TimeoutError(
            f"Codex CLI execution timed out (exceeded {timeout} seconds)"
        )

    except FileNotFoundError:
        raise RuntimeError(
            "codex command not found. Please ensure OpenAI Codex CLI is "
            "installed: npm install -g @openai/codex"
        )


def _extract_wrapped_content(
    text: str, start_delimiter: str, end_delimiter: str
) -> Optional[str]:
    """
    Extract content between start and end delimiters using greedy matching.
    Uses the first start delimiter and the last end delimiter to maximize content range.

    Args:
        text: Input text to search
        start_delimiter: Starting delimiter to find
        end_delimiter: Ending delimiter to find

    Returns:
        Content between delimiters, or None if not found properly
    """
    # Find the first occurrence of the start delimiter
    start_idx = text.find(start_delimiter)
    if start_idx == -1:
        return None

    # Find the last occurrence of the end delimiter after the start delimiter
    search_start = start_idx + len(start_delimiter)
    end_idx = text.rfind(end_delimiter)
    if end_idx == -1 or end_idx < search_start:
        return None

    # Extract content between delimiters
    content = text[search_start:end_idx]
    return content


def _extract_after_delimiter(text: str, delimiter: str) -> str:
    """
    Return the substring after the first occurrence of the delimiter.

    If the delimiter is not found, returns the original text unchanged.
    Leading newlines/spaces after the delimiter are stripped.
    """
    idx = text.find(delimiter)
    if idx == -1:
        return text
    # Move past delimiter
    after = text[idx + len(delimiter) :]
    # Strip all leading whitespace characters
    return after.lstrip()


def _escape_delimiter_for_display(delimiter: str) -> str:
    """
    Return a display-safe representation of a delimiter for inclusion in
    natural-language instructions without creating an exact match that could
    be mistaken for the actual delimiter in model output.

    Currently escapes square brackets by prefixing them with a backslash,
    which prevents accidental early recognition when using Lua-style long
    bracket delimiters like "--[=[" and "]=]--".
    """
    return delimiter.replace("[", r"\[").replace("]", r"\]")


def parse_codex_output(
    stdout: str,
    output_format: str,
    delimiter: Optional[str] = None,
    start_delimiter: Optional[str] = None,
    end_delimiter: Optional[str] = None,
    strict: Optional[bool] = None,
) -> dict:
    """
    Parse Codex CLI output into structured JSON.

    Args:
        stdout: Codex CLI standard output
        output_format: Expected output format
        delimiter: Single delimiter for backward compatibility (optional)
        start_delimiter: Start delimiter for wrapper extraction (optional)
        end_delimiter: End delimiter for wrapper extraction (optional)
        strict: Enable strict delimiter enforcement (optional)

    Returns:
        Structured parsing result
    """
    # Default delimiters and strict mode
    default_start_delimiter = "--[=["
    default_end_delimiter = "]=]--"
    default_strict = False
    # Backward-compatible single-line delimiter available as module-level constant

    # Handle delimiter extraction
    processed = stdout
    has_delimiter = False

    if start_delimiter is not None and end_delimiter is not None:
        # Wrapper-style delimiter extraction
        extracted_content = _extract_wrapped_content(
            stdout, start_delimiter, end_delimiter
        )
        if extracted_content is not None:
            processed = extracted_content
            has_delimiter = True
    elif delimiter is not None:
        # Single delimiter extraction for backward compatibility
        if delimiter in stdout:
            processed = _extract_after_delimiter(stdout, delimiter)
            has_delimiter = True
    else:
        # Use default wrapper delimiters
        extracted_content = _extract_wrapped_content(
            stdout, default_start_delimiter, default_end_delimiter
        )
        if extracted_content is not None:
            processed = extracted_content
            has_delimiter = True
        else:
            # Fallback to legacy single delimiter if present
            if FINAL_OUTPUT_DELIMITER in stdout:
                processed = _extract_after_delimiter(stdout, FINAL_OUTPUT_DELIMITER)
                has_delimiter = True

    # Check strict mode
    resolved_strict = strict if strict is not None else default_strict
    if resolved_strict and not has_delimiter:
        expected_delimiters = ""
        if start_delimiter and end_delimiter:
            expected_delimiters = f"'{start_delimiter}' and '{end_delimiter}'"
        elif delimiter:
            expected_delimiters = f"'{delimiter}'"
        else:
            expected_delimiters = (
                f"'{default_start_delimiter}' and '{default_end_delimiter}', "
                f"or the legacy '{FINAL_OUTPUT_DELIMITER}'"
            )

        return {
            "status": "error",
            "error_type": "final_output_delimiter_missing",
            "message": (
                f"Final output delimiters not found in model output; "
                f"expected {expected_delimiters}."
            ),
            "expected_delimiters": expected_delimiters,
            "format": output_format,
            "content": stdout.strip(),
        }

    # Auto-detect output type
    output_type = "explanation"  # Default type

    if "--- a/" in processed and "+++ b/" in processed:
        output_type = "diff"
    elif "```" in processed and processed.count("```") >= 2:
        output_type = "code"
    elif any(
        keyword in processed.lower()
        for keyword in ["file:", "class ", "function ", "def ", "import "]
    ):
        output_type = "code"

    return {
        "status": "success",
        "type": output_type,
        "content": processed.strip(),
        "format": output_format,
        "detected_type": output_type,
    }


@mcp.tool()
async def codex_delegate(
    task_description: str,
    working_directory: str,
    execution_mode: Literal[
        "untrusted", "on-failure", "on-request", "never"
    ] = "on-failure",
    sandbox_mode: Literal[
        "read-only", "workspace-write", "danger-full-access"
    ] = "read-only",
    output_format: Literal["diff", "full_file", "explanation"] = "diff",
    task_complexity: Literal["low", "medium", "high"] = "medium",
    final_output_start_delimiter: Optional[str] = None,
    final_output_end_delimiter: Optional[str] = None,
    final_output_strict: Optional[bool] = None,
) -> str:
    """
    Leverage Codex's advanced analytical capabilities for code comprehension and
    planning.

    Codex excels at reading and analyzing specific code files by filename
    and specializes in:
    • Precise file analysis when given explicit file paths
      (e.g., src/auth.py, tests/test_auth.py)
    • Designing architectural solutions and refactoring strategies
    • Planning implementation approaches and generating test strategies
    • Reviewing code for quality, security, and performance issues
    • Change impact mapping across codebases

    Evaluate each task's difficulty and set `task_complexity` to "low",
    "medium", or "high" so Codex can allocate appropriate reasoning effort.

    Note: Codex operates in read-only mode by default and produces analyses,
    plans, and proposed diffs.
    It never directly modifies source code - changes should be applied via
    Claude Code's editing tools.

    Args:
        task_description: Describe what you want Codex to analyze or plan
        working_directory: Project directory to analyze
        execution_mode: Approval strategy (default: on-failure)
        sandbox_mode: File access mode (forced to read-only unless --allow-write)
        output_format: How to format the analysis results; the bridge also
            injects a format-specific instruction into the prompt so the model
            returns only the requested format inside the delimiters
        task_complexity: Guidance for Codex's reasoning effort (default: "medium")
        final_output_start_delimiter: Start delimiter for output extraction
            (default: "--[=[")
        final_output_end_delimiter: End delimiter for output extraction
            (default: "]=]--")
        final_output_strict: Enable strict delimiter enforcement (default: False)

    Returns:
        Detailed analysis, recommendations, or implementation plan
    """
    # 1. Enforce read-only mode if write is not allowed (do this first)
    effective_sandbox_mode = sandbox_mode
    mode_notice: Optional[Dict[str, Union[str, List[str]]]] = None

    # Check if write operations are allowed (default: False for safety)
    allow_write = os.environ.get("CODEX_ALLOW_WRITE", "false").lower() == "true"

    if not allow_write and sandbox_mode != "read-only":
        effective_sandbox_mode = "read-only"
        mode_notice = {
            "mode": "planning",
            "description": "Operating in planning and analysis mode (read-only)",
            "message": (
                "Codex will analyze your code and provide detailed "
                "recommendations without modifying files."
            ),
            "hint": "To apply changes, restart the server with --allow-write flag",
            "benefits": [
                "Safe exploration of solutions",
                "Comprehensive analysis without risk",
                "Thoughtful planning before execution",
            ],
        }

    # 2. Validate working directory
    if not dde.validate_working_directory(working_directory):
        error_result: Dict[str, Any] = {
            "status": "error",
            "message": f"Invalid or unsafe working directory: {working_directory}",
            "error_type": "invalid_directory",
            "working_directory": working_directory,
            "sandbox_mode": effective_sandbox_mode,
            "requested_sandbox_mode": sandbox_mode,
        }

        # Add operation mode notice if applicable
        if mode_notice:
            error_result["operation_mode"] = mode_notice

        return json.dumps(error_result, indent=2, ensure_ascii=False)

    # 3. Use DDE to decide whether to delegate
    if not dde.should_delegate(task_description):
        rejection_result = {
            "status": "rejected",
            "message": "The task is not suitable for delegation to Codex CLI",
            "reason": "Task not suitable for Codex delegation",
        }
        return json.dumps(rejection_result, indent=2, ensure_ascii=False)

    # 4. Prepare Codex instruction
    codex_prompt = dde.prepare_codex_prompt(task_description)
    optimization_note = None  # Will be used for metacognitive optimization in future

    # Resolve delimiter parameters
    start_delimiter = (
        final_output_start_delimiter
        if final_output_start_delimiter is not None
        else "--[=["
    )
    end_delimiter = (
        final_output_end_delimiter
        if final_output_end_delimiter is not None
        else "]=]--"
    )

    # Build format-specific instruction and prepend delimiter instruction to prompt
    if output_format == "diff":
        format_instruction = (
            "Inside the wrapper, output a unified diff only in git patch format "
            "starting with '--- a/' and '+++ b/' headers. Do not include code "
            "fences, comments, or extra text."
        )
    elif output_format == "full_file":
        format_instruction = (
            "Inside the wrapper, output only the complete final file content(s) "
            "without any code fences or commentary. If multiple files, separate "
            "each with a line 'File: <path>' followed by the file content."
        )
    else:  # explanation
        format_instruction = (
            "Inside the wrapper, output only the explanation as plain text, "
            "with no code fences or extraneous headers."
        )

    display_start = _escape_delimiter_for_display(start_delimiter)
    display_end = _escape_delimiter_for_display(end_delimiter)

    # Note: We intentionally use escaped delimiters in the instruction text to
    # avoid accidental early detection if the model echoes the instruction.
    # The model should use the actual (unescaped) delimiters in its output.
    delimiter_instruction = (
        f"Please wrap your final deliverable content between "
        f"{display_start} and {display_end} delimiters. "
        f"Place any reasoning, explanation, or process details before the "
        f"start delimiter, and put only the final code, analysis, or requested "
        f"output between the delimiters. "
        f"Note: In this instruction, '[' and ']' are escaped with backslashes; "
        f"do not include backslashes in the actual delimiters in your output."
    )

    try:
        # 5. Invoke Codex CLI
        stdout, stderr = await invoke_codex_cli(
            f"{format_instruction}\n\n{delimiter_instruction}\n\n{codex_prompt}",
            working_directory,
            execution_mode,
            effective_sandbox_mode,
            task_complexity,
            allow_write,
        )

        # 6. Parse output
        result = parse_codex_output(
            stdout,
            output_format,
            start_delimiter=start_delimiter,
            end_delimiter=end_delimiter,
            strict=final_output_strict,
        )

        # Add metadata
        result.update(
            {
                "working_directory": working_directory,
                "execution_mode": execution_mode,
                "sandbox_mode": effective_sandbox_mode,
                "requested_sandbox_mode": sandbox_mode,
                "optimization_note": optimization_note,
                "original_task": task_description,
                "codex_prompt": (
                    codex_prompt if codex_prompt != task_description else None
                ),
            }
        )

        # Add operation mode notice if applicable
        if mode_notice:
            result["operation_mode"] = mode_notice

        # If there is stderr, include it as well
        if stderr.strip():
            result["stderr"] = stderr.strip()

        return json.dumps(result, indent=2, ensure_ascii=False)

    except Exception as e:
        # Handle execution errors
        error_result = {
            "status": "error",
            "message": str(e),
            "error_type": type(e).__name__,
            "working_directory": working_directory,
            "execution_mode": execution_mode,
            "sandbox_mode": effective_sandbox_mode,
            "requested_sandbox_mode": sandbox_mode,
            "optimization_note": "",  # No optimization applied on error
        }

        # Add operation mode notice if applicable
        if mode_notice:
            error_result["operation_mode"] = mode_notice

        return json.dumps(error_result, indent=2, ensure_ascii=False)


@mcp.resource("bridge://docs/usage")
def get_usage_guide() -> str:
    """
    Return the usage guide documentation for Claude-Codex Bridge.
    """
    return """
# Claude-Codex Bridge - Intelligent Code Analysis & Planning Tool

## Core Philosophy
Codex excels at understanding, analyzing, and planning - not just executing.
This bridge leverages Codex's unique strengths:

### 🧠 Deep Analysis
- Understand complex code relationships
- Identify architectural patterns and anti-patterns
- Analyze performance bottlenecks

### 📋 Strategic Planning
- Design refactoring strategies
- Plan feature implementations
- Create test strategies

### 🔍 Code Review
- Security vulnerability assessment
- Code quality evaluation
- Best practices recommendations

## Default Read-Only Mode
For safety and thoughtful development, the bridge operates in read-only mode by default.

### Benefits of Planning Mode:
1. **Risk-Free Analysis**: Explore solutions without modifying code
2. **Comprehensive Understanding**: Deep dive into codebase structure
3. **Better Decisions**: Plan thoroughly before execution
4. **Learning Opportunity**: Understand WHY changes are needed

## Recommended Workflow

### Step 1: Analyze (Read-Only)
```bash
# Start in default planning mode
uv run -m claude_codex_bridge
```
Ask Codex to:
- "Analyze the authentication system for security vulnerabilities"
- "Review the database layer for performance improvements"
- "Suggest architectural improvements for scalability"

### Step 2: Plan (Read-Only)
Review Codex's analysis and ask for specific plans:
- "Design a migration strategy for the suggested improvements"
- "Create a test plan for the refactoring"

### Step 3: Execute (Write Mode)
When ready to apply changes:
```bash
# Enable write mode
uv run -m claude_codex_bridge --allow-write
```

## Tool Usage

### Planning Mode (Default)
```python
codex_delegate(
    task_description="Analyze the user authentication system for security
                     vulnerabilities",
    working_directory="/path/to/your/project",
    execution_mode="on-failure",
    sandbox_mode="read-only",      # Enforced automatically
    output_format="explanation",
    task_complexity="medium"
)
```

### Execution Mode (--allow-write)
```python
codex_delegate(
    task_description="Implement the planned security improvements",
    working_directory="/path/to/your/project",
    execution_mode="on-failure",
    sandbox_mode="workspace-write",  # Now allowed
    output_format="diff",
    task_complexity="high"
)
```

### Parameter Explanation

**task_description** (required)
- Describe what you want Codex to analyze or plan
- Planning examples: "Analyze authentication security" or "Design refactoring strategy"
- Implementation examples: "Apply the planned security improvements"

**working_directory** (required)
- Absolute path to project directory to analyze
- Example: "/Users/username/my-project"

**execution_mode** (optional, default: "on-failure")
- `untrusted`: Only run trusted commands (safest for analysis)
- `on-failure`: Request approval only on failure (recommended)
- `on-request`: Model decides when to request approval
- `never`: Never request approval (use with caution)

**sandbox_mode** (optional, default: "read-only")
- `read-only`: Read-only access (automatically enforced unless --allow-write)
- `workspace-write`: Writable workspace (only available with --allow-write)
- `danger-full-access`: Full system access (dangerous, requires --allow-write)

**output_format** (optional, default: "diff")
- `explanation`: Natural language analysis and recommendations (best for planning)
- `diff`: Changes in patch format (useful for implementation)
- `full_file`: Complete modified file content

**task_complexity** (optional, default: "medium")
- Reflects task difficulty and guides Codex's reasoning effort
- Choose "low", "medium", or "high" after assessing the task

## Advanced Features

### Metacognitive Instruction Optimization
When `ANTHROPIC_API_KEY` environment variable is set, the bridge uses
Claude 3 Haiku to automatically optimize your task instructions for clarity
and specificity.

### Automatic Output Type Detection
The bridge automatically recognizes Codex return content types (diff,
code blocks, or explanation text) and labels them in responses.

## Best Practices

### Planning-First Approach
1. **Start with Analysis**: Begin in read-only mode to understand before acting
2. **Ask Strategic Questions**: Focus on "what patterns exist?" and
   "what could be improved?"
3. **Plan Comprehensively**: Design solutions before implementing them
4. **Review Before Executing**: Examine Codex's recommendations carefully

### Task Description Guidelines
1. **Planning Phase**: "Analyze X for Y" or "Design strategy for Z"
2. **Implementation Phase**: "Apply the planned improvements" or
   "Implement the designed solution"
3. **Be Specific**: State clear objectives and scope
4. **Provide Context**: Include relevant constraints and requirements

### Safety and Security
1. **Default to Read-Only**: Use planning mode by default for safety
2. **Absolute Paths**: Always use full paths for working directories
3. **Enable Write Carefully**: Only use --allow-write when ready to apply changes
4. **Validate Results**: Test thoroughly after applying modifications

## Example Usage

### Security Analysis Workflow

**Step 1: Analysis (Planning Mode)**
```
task_description: "Analyze the authentication system for security vulnerabilities"
working_directory: "/Users/username/my-web-app"
execution_mode: "on-failure"
sandbox_mode: "read-only"  # Automatically enforced
output_format: "explanation"
task_complexity: "medium"
```

**Step 2: Planning (Planning Mode)**
```
task_description: "Design security improvements for the identified vulnerabilities"
working_directory: "/Users/username/my-web-app"
execution_mode: "on-failure"
sandbox_mode: "read-only"  # Automatically enforced
output_format: "explanation"
task_complexity: "medium"
```

**Step 3: Implementation (Execution Mode - requires --allow-write)**
```
task_description: "Implement the planned security improvements"
working_directory: "/Users/username/my-web-app"
execution_mode: "on-failure"
sandbox_mode: "workspace-write"  # Now allowed
output_format: "diff"
task_complexity: "high"
```

### Performance Optimization Example

**Analysis Phase:**
```
task_description: "Analyze the database queries for performance bottlenecks"
working_directory: "/Users/username/my-django-project"
execution_mode: "on-failure"
sandbox_mode: "read-only"
output_format: "explanation"
task_complexity: "medium"
```

**Implementation Phase:**
```
task_description: "Apply the designed query optimizations"
working_directory: "/Users/username/my-django-project"
execution_mode: "on-failure"
sandbox_mode: "workspace-write"
output_format: "diff"
task_complexity: "high"
```

## Error Handling

The bridge provides detailed error information including:
- Working directory validation errors
- Codex CLI execution errors
- Timeout errors
- Permission errors

Check the `status` field in returned JSON to determine execution result.

## Prerequisites

1. Install OpenAI Codex CLI: `npm install -g @openai/codex`
2. Optional: Set `ANTHROPIC_API_KEY` environment variable to enable
   metacognitive optimization
"""


@mcp.resource("bridge://docs/best_practices")
def get_best_practices() -> str:
    """
    Returns best practices for effective planning-first development with Codex.
    """
    return """
# Best Practices for Codex Planning & Analysis

## Embrace the Planning-First Philosophy

Codex excels at analysis and strategic thinking. Use this strength by following
a structured approach: Analyze → Plan → Execute.

## Task Description Excellence

### ✅ Planning Phase Requests
- **Analysis**: "Analyze the authentication system for security vulnerabilities"
- **Evaluation**: "Review the API design for RESTful best practices"
- **Assessment**: "Evaluate the database schema for normalization issues"
- **Strategy**: "Design a migration plan from monolithic to microservices architecture"

### ✅ Implementation Phase Requests
- **Specific**: "Implement the planned security improvements for authentication"
- **Targeted**: "Apply the designed API restructuring to user endpoints"
- **Phased**: "Execute phase 1 of the database normalization plan"

### ❌ Requests to Avoid
- **Too vague**: "Improve the code" → What specifically needs improvement?
- **Too broad**: "Fix all issues" → Start with analysis to identify issues
- **No context**: "Add new feature" → Plan the feature design first

## Operational Mode Selection

### Planning Mode (Default - No --allow-write flag)
- **Use case**: Analysis, planning, strategy design, code review
- **Benefits**: Risk-free exploration, comprehensive understanding, better decisions
- **Sandbox**: Automatically enforced read-only mode
- **Best for**: Understanding problems before solving them

### Execution Mode (Requires --allow-write flag)
- **Use case**: Implementing planned solutions, applying designed changes
- **Benefits**: Execute well-planned modifications with confidence
- **Sandbox**: workspace-write or danger-full-access available
- **Best for**: Applying solutions you've already planned and reviewed

## Workflow Best Practices

### 1. Always Start with Planning
```
❌ Direct Implementation: "Add user authentication to the app"
✅ Planning First:
   - "Analyze current authentication patterns in the codebase"
   - "Design a secure authentication strategy"
   - "Plan implementation steps for authentication"
   - Then: "Implement the planned authentication system"
```

### 2. Break Down Complex Analysis
```
❌ Too Broad: "Analyze the entire application"
✅ Focused Analysis:
   - "Analyze the data layer for performance bottlenecks"
   - "Evaluate API endpoints for security vulnerabilities"
   - "Review frontend components for accessibility compliance"
```

### 3. Strategic Planning Questions
```
✅ Architecture: "What architectural patterns would improve scalability?"
✅ Performance: "Which components are performance bottlenecks and why?"
✅ Security: "What are the security vulnerabilities and their impact?"
✅ Quality: "What code quality issues affect maintainability?"
```

## Execution Strategies

### When to Enable Write Mode
1. **After thorough planning**: You have a clear plan from Codex's analysis
2. **Specific implementations**: You're ready to apply specific, planned changes
3. **Phased execution**: Implementing one phase of a larger plan
4. **With clear scope**: You understand exactly what will be modified

### Implementation Best Practices
1. **Reference the Plan**: "Implement the security improvements we planned earlier"
2. **Specific Scope**: "Apply the database optimizations to the user queries module"
3. **Phased Approach**: "Execute phase 1 of the authentication refactoring plan"
4. **Include Context**: "Apply the planned changes while maintaining
   backward compatibility"

## Example Workflow: Security Hardening

### Phase 1: Analysis (Planning Mode)
```
"Analyze all API endpoints for security vulnerabilities"
```

### Phase 2: Strategy (Planning Mode)
```
"Design comprehensive security improvements for the identified vulnerabilities"
```

### Phase 3: Implementation (Execution Mode)
```
"Implement the planned security improvements for the authentication endpoints"
```

### Phase 4: Validation (Planning Mode)
```
"Review the implemented security changes for completeness and effectiveness"
```

## Safety Guidelines

### Working Directory Security
- Use absolute paths only
- Ensure directories exist and are accessible
- Avoid system directories (/etc, /usr/bin, etc.)
- Test in development environments first

### Error Handling
- Check the `status` field in responses
- Review `operation_mode` notices for mode information
- Read `error_type` and `message` for troubleshooting details
- Use planning mode to understand issues before fixing

### Performance Tips
- Use planning mode for complex analysis (cheaper and safer)
- Break large tasks into focused analysis sessions
- Enable write mode only when ready to implement planned changes
"""


try:
    from mcp.server.fastmcp.messages import UserMessage
except ImportError:
    # Try alternative import path or use a simple dict alternative
    class FallbackUserMessage:
        def __init__(self, content: str) -> None:
            self.content = content

    UserMessage = FallbackUserMessage


@mcp.prompt()
def refactor_code(file_path: str, refactor_type: str = "general") -> list:
    """
    Generates a prompt template for refactoring code.

    Args:
        file_path: The path to the file to be refactored
        refactor_type: The type of refactoring (general, performance,
            readability, structure)
    """
    refactor_descriptions = {
        "general": "Perform general code refactoring to improve code quality",
        "performance": "Refactor code to improve performance and efficiency",
        "readability": "Refactor code to improve readability and maintainability",
        "structure": "Refactor code structure to improve architectural design",
    }

    description = refactor_descriptions.get(refactor_type, "Refactor code")

    task_description = (
        f"Please {description} for the file '{file_path}'. Keep the original "
        f"functionality unchanged, but improve code quality, readability, and "
        f"maintainability."
    )

    return [
        UserMessage(f"I will refactor the {file_path} file for you."),
        UserMessage(f"Refactoring type: {refactor_type}"),
        UserMessage(f"Task: {task_description}"),
        UserMessage(
            "Please ensure the working directory is set correctly before "
            "calling the codex_delegate tool."
        ),
    ]


@mcp.prompt()
def generate_tests(file_path: str, test_framework: str = "pytest") -> list:
    """
    Generates a prompt template for creating tests for a specified file.

    Args:
        file_path: The path to the file for which to generate tests
        test_framework: The testing framework (pytest, unittest, jest, etc.)
    """
    task_description = (
        f"Generate comprehensive {test_framework} test cases for file "
        f"'{file_path}'.\\n\\n"
        f"Requirements:\\n"
        f"1. Cover all public functions and methods\\n"
        f"2. Include normal cases and edge condition tests\\n"
        f"3. Add exception handling tests\\n"
        f"4. Ensure test cases are clear and well-described\\n"
        f"5. Follow {test_framework} best practices"
    )

    return [
        UserMessage(f"I will generate {test_framework} test cases for {file_path}."),
        UserMessage(
            "This will include comprehensive test coverage, including edge "
            "cases and exception scenarios."
        ),
        UserMessage(f"Task description: {task_description}"),
        UserMessage(
            "Please call the codex_delegate tool after setting the correct "
            "working directory."
        ),
    ]


if __name__ == "__main__":
    # Start the MCP server
    mcp.run()
