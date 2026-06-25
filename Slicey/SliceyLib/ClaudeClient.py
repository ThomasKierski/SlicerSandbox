import logging

import slicer

from . import FolderAccess
from . import PythonExecutor
from . import Settings

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "list_shared_folders",
        "description": (
            "List the folders the user has explicitly shared with you, and whether each is "
            "read-only or read-write. You can only read/write inside these folders - there is "
            "no other filesystem access. Call this first if you don't already know what's shared."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories under an absolute path inside one of the shared folders.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path inside a shared folder."},
                "recursive": {"type": "boolean", "description": "List subdirectories recursively. Default false."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_text_file",
        "description": "Read a UTF-8 text file's contents. The path must be inside a shared folder. Large files are truncated.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to the file."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_text_file",
        "description": (
            "Create or overwrite a UTF-8 text file. The path must be inside a shared folder that "
            "is marked read-write. Parent directories are created automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file."},
                "content": {"type": "string", "description": "Full text content to write."},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "Default overwrite."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_python_in_slicer",
        "description": (
            "Execute Python code inside a real, running 3D Slicer application's Python environment "
            "(the same interpreter the Script Repository snippets target: slicer, vtk, qt, ctk are "
            "already importable). Use this instead of ever asking the user to copy-paste code. "
            "Prefer slicer.util.reloadScriptedModule('ModuleName') after editing a module rather than "
            "restarting Slicer. Set a dict on a variable named __execResult if you want structured "
            "data back in addition to stdout/stderr.\n"
            "Where this runs is fixed by the user in the Settings panel's Execution section, not by "
            "you: either their already-open Slicer window (affects their live scene/GUI immediately), "
            "or a separate, isolated companion Slicer process with no scene loaded.\n"
            "When running in the user's current Slicer window, getPythonConsoleOutput(historyIndex=0, "
            "offset=0, length=None) is also already available - it returns text recently printed to "
            "Slicer's own Python console, including from things other than your own code (e.g. the "
            "user manually interacting with the GUI, or other modules logging/erroring). historyIndex=0 "
            "(default) is everything since the user's last chat message, 1 is the window one chat "
            "message before that, 2 two messages before that, etc. (up to 10 kept); offset/length page "
            "through a long result, character-based."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source code to execute."},
            },
            "required": ["code"],
        },
    },
]


def buildSystemPrompt():
    folders = FolderAccess.listSharedFolders()
    if folders:
        folderLines = "\n".join(
            "  - {}{}{}".format(
                f["path"],
                " (read-write)" if f["writable"] else " (read-only)",
                "" if f["exists"] else " [MISSING]",
            )
            for f in folders
        )
    else:
        folderLines = "  (none configured yet - ask the user to add one in the Settings panel if you need file access)"

    if Settings.getExecutionTarget() == "new_instance":
        executionNote = (
            "run_python_in_slicer currently runs in a separate, isolated companion Slicer "
            "process with no scene loaded - it will NOT affect the user's open scene/GUI."
        )
    else:
        executionNote = (
            "run_python_in_slicer currently runs in the user's already-open Slicer window - it "
            "affects their live scene/GUI immediately."
        )

    prompt = (
        f"{Settings.SYSTEM_PROMPT_INSTRUCTIONS}\n\n{executionNote}\n\n"
        f"Shared folders you currently have access to:\n{folderLines}"
    )

    customText = Settings.getCustomSystemPromptText().strip()
    if customText:
        prompt += f"\n\nAdditional instructions from the user:\n{customText}"

    return prompt


def requiresMainThread(name, toolInput):
    """Returns True if dispatching this tool call must happen on Slicer's main thread (it
    touches Slicer/Qt/VTK/MRML objects). The only tool call that's safe to run on a background
    thread is run_python_in_slicer targeting a separate companion instance, since that only
    involves subprocess/socket/HTTP calls on our side. The target is the user's Settings panel
    choice (Settings.getExecutionTarget()), not something Claude requests per call.
    """
    if name == "run_python_in_slicer":
        return Settings.getExecutionTarget() != "new_instance"
    return True


class _MissingToolArgument(Exception):
    pass


def _require(toolInput, key):
    """Looks up a required tool argument, raising an actionable error (instead of a bare
    KeyError) if it's missing - most often because the response got cut off by the max_tokens
    limit partway through a large tool call (e.g. writing a big file), which can leave the
    JSON for that argument incomplete."""
    if key not in toolInput:
        raise _MissingToolArgument(
            f"Missing required '{key}' argument - the response was likely cut off by the "
            f"max_tokens limit while generating this tool call. Try again with a smaller "
            f"amount of content per call (e.g. write a short file first, then use "
            f"write_text_file with mode='append' one or more times to add the rest)."
        )
    return toolInput[key]


def dispatchTool(name, toolInput):
    """Executes a tool call and returns its result as a JSON-serializable dict."""
    try:
        if name == "list_shared_folders":
            return {"folders": FolderAccess.listSharedFolders()}
        if name == "list_directory":
            return FolderAccess.listDirectory(_require(toolInput, "path"), toolInput.get("recursive", False))
        if name == "read_text_file":
            return FolderAccess.readTextFile(_require(toolInput, "path"))
        if name == "write_text_file":
            return FolderAccess.writeTextFile(_require(toolInput, "path"), _require(toolInput, "content"), toolInput.get("mode", "overwrite"))
        if name == "run_python_in_slicer":
            code = _require(toolInput, "code")
            if Settings.getExecutionTarget() == "new_instance":
                return PythonExecutor.executeInNewInstance(code)
            return PythonExecutor.executeInProcess(code)
        return {"error": f"Unknown tool: {name}"}
    except _MissingToolArgument as e:
        return {"error": str(e)}
    except Exception as e:
        logger.exception("Slicey: tool execution failed")
        return {"error": str(e)}


def _ensureAnthropicPackage():
    try:
        import anthropic
    except ImportError:
        slicer.util.pip_install("anthropic")
        import anthropic
    return anthropic


def createClient(apiKey):
    anthropic = _ensureAnthropicPackage()
    return anthropic.Anthropic(api_key=apiKey)


def testApiKey(apiKey):
    """Makes a minimal request to validate the key. Returns (ok: bool, message: str)."""
    try:
        client = createClient(apiKey)
        client.messages.create(
            model=Settings.VALIDATION_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return True, "Connected."
    except Exception as e:
        return False, str(e)


def sendMessage(client, model, messages, systemPrompt, maxTokens=16000):
    """Blocking call to the Messages API with tools enabled. Safe to call from a background
    thread - performs no Slicer/Qt access itself.
    """
    return client.messages.create(
        model=model,
        max_tokens=maxTokens,
        system=systemPrompt,
        tools=TOOLS,
        messages=messages,
    )
