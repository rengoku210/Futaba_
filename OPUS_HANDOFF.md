# OPUS HANDOFF & FUTABA SYSTEM DOCUMENTATION
## Master Engineering Directive — Autonomous Windows Copilot

---

## 1. Project Overview & Cognitive Architecture

**FUTABA** is a production-grade autonomous Windows AI copilot built on top of the **Hermes Agent** execution foundation. It bridges conversational speech/voice via Gemini Live with native Windows operating system automation, UI element interaction, browser control, and multi-step autonomous task planning.

### The 3 Speed Tiers

To eliminate unnecessary LLM calls, planning overhead, and latency, FUTABA implements a 3-tier cognitive execution hierarchy:

```text
User Input / Voice Utterance
            ↓
    Intent & Context Engine
            ↓
       Tier Router
   ┌────────┼────────┐
   ↓        ↓        ↓
 Tier 0   Tier 1   Tier 2
 Direct    Fast    Complex
 Action Interactive Autonomous
```

1. **Tier 0: Direct Action (<100ms – 2s latency budget)**
   - **Scope**: Deterministic single-operation actions that require zero semantic ambiguity resolution or multi-step planning.
   - **Execution**: Handled directly in `CommandRouter` and `MicroActionEngine` via native Win32 API / OS calls (`keybd_event`, `mouse_event`, process signals).
   - **Operations**: Volume adjustments (up, down, mute), media control (play, pause, next, prev), browser back/forward, tab switching, global shortcuts (`Ctrl+C`, `Ctrl+V`, `Alt+F4`), window minimizes/restores.
   - **Latency**: Typically <10ms for Win32 key events.

2. **Tier 1: Fast Interactive Task (0.5s – 5s latency budget)**
   - **Scope**: Single-surface interactive commands with clear targets on the current active window or browser tab.
   - **Execution**: Bypasses the full LLM planner. Uses `SemanticUITargeter` to resolve natural language descriptors against the live Windows UIA accessibility tree / DOM, and executes via `MicroActionEngine` using the strict pattern: **Before State → Execute → After State → Verify**.
   - **Operations**: "click the general channel", "click the search bar", "select the third Short", "click the first video", "type ESP32 into the search box", "scroll down".
   - **Latency**: Typically 150ms – 800ms for UIA scan + element action + state verification.

3. **Tier 2: Complex Autonomous Task (Full Hermes Agent Execution Loop)**
   - **Scope**: Multi-step workflows, ambiguous requests, cross-application chaining, compound goals requiring decomposition, recovery from unexpected failures, or file/code generation.
   - **Execution**: Dispatches to `TaskManager` and `AgentController` (Hermes Agent foundation). Employs full Plan → Execute → Observe → Verify → Recover loop with atomic JSON journaling and checkpoints.
   - **Operations**: "download and install Python", "find the latest invoice in my email, download it, and extract the total into an Excel sheet", "build a script to organize my downloads by file type".

---

## 2. Core Modules Implemented for Master Directive

### 1. Latency Telemetry (`src/futaba/intelligence/telemetry.py`)
- Tracks per-request lifecycle timestamps:
  - `speech_end`
  - `intent_detected`
  - `context_snapshot`
  - `entity_resolution`
  - `surface_selection`
  - `planner_start` / `planner_end`
  - `tool_start` / `tool_end`
  - `verification_start` / `verification_end`
- Calculates millisecond latencies for each stage:
  - `intent_ms`, `context_ms`, `resolution_ms`, `surface_ms`, `planning_ms`, `execution_ms`, `verification_ms`, and `total_ms`.
- Checks compliance against defined latency budgets (Tier 0: <2000ms, Tier 1: <5000ms, Tier 2: <30000ms).
- Provides logging, telemetry history, and average breakdown reporting.

### 2. Semantic UI Element Targeter (`src/futaba/intelligence/semantic_ui.py`)
- Traverses the live Windows UIA accessibility tree (`pywinauto.Desktop(backend="uia")`) to discover visible interactive controls without needing expensive vision/VLM calls.
- Normalizes control names, automation IDs, bounding rectangles, control types, and states.
- Resolves natural language target descriptors:
  - **Ordinal queries**: "first video", "second link", "3rd result", "last item".
  - **Role queries**: "search button", "channel list", "message edit box".
  - **Fuzzy label matching**: Resolves queries like "general" to Discord's `# general` text channel control.
- Safe fallback: If UIA desktop access fails or is non-interactive/headless, gracefully returns empty results without crashing.

### 3. Micro-Action Engine (`src/futaba/intelligence/micro_action.py`)
- Implements all 19 atomic desktop micro-actions:
  `click`, `double_click`, `right_click`, `type`, `keypress`, `hotkey`, `scroll`, `focus`, `select`, `open`, `close`, `switch_tab`, `switch_window`, `navigate`, `drag`, `drop`, `submit`, `play`, `pause`.
- Strict execution cycle:
  1. Capture **Before State** (active window handle, title, control state).
  2. **Execute** the micro-action with timeout safety.
  3. Capture **After State**.
  4. Perform **Ground Truth Verification** (ensure focus changed, text appeared, window opened/closed, or process spawned).
- Headless and non-interactive station resilience:
  - Mouse scroll gracefully falls back to `{PGDN}` / `{PGUP}` key simulation when Win32 `SetCursorPos` times out.
  - Direct Win32 media and volume control via `ctypes.windll.user32.keybd_event` (`VK_VOLUME_DOWN = 0xAE`, `VK_VOLUME_UP = 0xAF`, `VK_VOLUME_MUTE = 0xAD`).

### 4. Tier Router (`src/futaba/intelligence/tier_router.py`)
- Analyzes incoming user requests, current application context, and extracted intent.
- Determines whether a request qualifies for Tier 0 (direct action), Tier 1 (fast interactive action), or Tier 2 (complex autonomous task).
- Ensures multi-action conjunctions ("open YouTube and search for ESP32", "download ... then install ...") reliably escalate to Tier 2.

### 5. Section 31 Task State Machine (`src/futaba/tasks/task_manager.py`)
- Full compliance with Section 31 state machine specification:
  - `CREATED`
  - `UNDERSTANDING`
  - `CONTEXT_RESOLUTION`
  - `PLANNING`
  - `EXECUTING`
  - `RUNNING`
  - `OBSERVING`
  - `VERIFYING`
  - `RECOVERING`
  - `PAUSED`
  - `BLOCKED`
  - `COMPLETED`
  - `FAILED`
  - `CANCELLED`
- Rigid transition guard: Prevents illegal transitions (e.g. `FAILED -> VERIFYING`, `COMPLETED -> EXECUTING`, `CANCELLED -> EXECUTING`).

### 6. Persistent Context State (`src/futaba/intelligence/context_engine.py`)
- Enhanced `ContextState` with Section 9 context tracking:
  - `active_server` (e.g., "Donut SMP")
  - `active_channel` (e.g., "general")
  - `active_page` / URL
  - `active_search_query`
  - `visible_controls` (cached UIA controls)
  - `recent_user_intent`
  - `recent_entities` (extracted entities across turns)
  - `last_successful_action` / `last_failed_action`
  - `futaba_state`
- Powers seamless multi-turn conversational continuity (e.g. "open discord" → "switch to Donut SMP" → "go to general" → "now open youtube" → "search esp32" → "click the first video" → "go back" → "third Short" → "play it" → "volume down").

---

## 3. Test Suite Verification

The full test suite was executed and verified:

```text
============================== 135 passed in 59.17s ==============================
```

### Breakdown of Test Suites:
1. **Master Cognitive Architecture Suite (`tests/test_cognitive_architecture_master.py`)** — **14/14 PASSED**:
   - `test_01_telemetry_lifecycle`: Complete lifecycle timestamps and derived duration calculation.
   - `test_02_telemetry_budgets`: Enforcement of latency budgets for Tier 0, 1, and 2.
   - `test_01_parse_ordinal_targets`: Accurate parsing of ordinals ("third Short", "first video", "2nd link").
   - `test_02_scan_controls_never_crashes`: Non-interactive/headless resilience of UIA tree scan.
   - `test_01_all_19_actions_supported`: Verification of all 19 micro-action enumerations and handlers.
   - `test_02_micro_action_execute_with_verification`: Validation of Before State → Action → After State → Verify loop.
   - `test_03_scroll_fallback`: Verification of `{PGDN}` fallback when mouse scroll cursor is unavailable.
   - `test_01_tier_0_routing`: Routing of volume, media, back, and hotkeys to Tier 0.
   - `test_02_tier_1_routing`: Routing of single-target UIA clicks and types to Tier 1.
   - `test_03_tier_2_routing`: Routing of multi-step plans and ambiguous tasks to Tier 2.
   - `test_01_all_section_31_states_present`: Verification of all 14 Section 31 states.
   - `test_02_valid_transitions`: Verification of legal task lifecycle transitions.
   - `test_03_illegal_transitions_rejected`: Rejection of illegal transitions (`COMPLETED -> EXECUTING`, `FAILED -> VERIFYING`, `CANCELLED -> EXECUTING`).
   - `test_full_conversational_continuity_scenario`: Verification of the complete 10-turn Master Directive conversational workflow.
2. **Acceptance Test Suite (`tests/acceptance/test_full_acceptance.py`)** — **23/23 PASSED**.
3. **Core Smoke Tests (`tests/test_core_smoke.py`)** — **PASSED**.
4. **Async Component Tests (`tests/test_async_components.py`)** — **PASSED**.
5. **Packaged EXE Tests (`tests/test_packaged_exe.py`)** — **PASSED**.
6. **Task Recovery & Clean Run Tests (`tests/test_clean_task_run.py`)** — **PASSED**.

---

## 4. Packaged Executable Location & Verification

The production-ready standalone executable bundle is located at:

```text
d:\futaba cop\dist\Futaba\Futaba.exe
```

### Included Modules & Capabilities:
- Full Python 3.11 runtime environment.
- PySide6 Qt GUI, HUD overlay, and system tray.
- Hermes Agent foundation (`cua-driver-rs`, computer-use tools, browser tools).
- Native Windows UI Automation engine (`pywinauto`, `win32gui`, `win32con`, UIA backend).
- New cognitive modules: `futaba.intelligence.telemetry`, `futaba.intelligence.semantic_ui`, `futaba.intelligence.micro_action`, `futaba.intelligence.tier_router`.
- DPAPI Windows Credential Manager integration.
- WebSocket IPC server on port `45850`.

---

## 5. How to Run FUTABA

### Launch Full Desktop GUI:
```cmd
dist\Futaba\Futaba.exe --mode full
```
*(or via source: `python main.py --mode full`)*

### Launch Headless Daemon / Background Mode:
```cmd
dist\Futaba\Futaba.exe --mode headless
```
*(or via source: `python main.py --mode headless`)*

### Run Automated Acceptance Verification:
```cmd
python -m pytest tests/test_cognitive_architecture_master.py -v
python -m pytest tests/ -q
```
