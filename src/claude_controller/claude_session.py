"""Claude Code session manager — wraps the Agent SDK for interactive sessions."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    HookMatcher,
)

from claude_controller.config import CLAUDE_CWD, CLAUDE_MODEL

logger = logging.getLogger(__name__)


@dataclass
class PendingQuestion:
    """A question Claude is waiting for the user to answer."""
    questions: list[dict[str, Any]]
    answer_future: asyncio.Future


@dataclass
class SessionState:
    """Tracks the current Claude Code session."""
    session_id: str | None = None
    running: bool = False
    last_output: list[str] = field(default_factory=list)
    pending_question: PendingQuestion | None = None
    total_cost_usd: float = 0.0


class ClaudeSession:
    """Manages a Claude Code session with AskUserQuestion interception."""

    def __init__(self) -> None:
        self.state = SessionState()
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self, prompt: str, on_message: Any = None) -> None:
        """Start a new Claude Code session with the given prompt.

        Args:
            prompt: The task prompt to send to Claude.
            on_message: Optional async callback(text: str) for streaming output.
        """
        if self.state.running:
            raise RuntimeError("Session already running")

        self._loop = asyncio.get_running_loop()
        self.state.running = True
        self.state.last_output = []
        self.state.pending_question = None

        self._task = asyncio.create_task(self._run(prompt, on_message))

    async def _run(self, prompt: str, on_message: Any) -> None:
        """Execute the Claude Code query."""
        try:
            options = ClaudeAgentOptions(
                cwd=CLAUDE_CWD,
                permission_mode="acceptEdits",
                can_use_tool=self._can_use_tool,
                hooks={
                    "PreToolUse": [
                        HookMatcher(matcher=None, hooks=[self._pre_tool_hook])
                    ]
                },
            )
            if CLAUDE_MODEL:
                options.model = CLAUDE_MODEL

            if self.state.session_id:
                options.resume = self.state.session_id

            # can_use_tool requires streaming mode — wrap prompt as async iterable
            async def _prompt_stream():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": prompt},
                }

            async for message in query(prompt=_prompt_stream(), options=options):
                # Extract session ID from init message
                if hasattr(message, "subtype") and message.subtype == "init":
                    data = getattr(message, "data", {})
                    if "session_id" in data:
                        self.state.session_id = data["session_id"]
                        logger.info("Session ID: %s", self.state.session_id)

                # Extract text from assistant messages
                if hasattr(message, "content"):
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            self.state.last_output.append(block.text)
                            if on_message:
                                await on_message(block.text)

                # Extract result
                if hasattr(message, "result") and message.result:
                    self.state.last_output.append(message.result)
                    if on_message:
                        await on_message(message.result)

                # Track cost
                if hasattr(message, "total_cost_usd") and message.total_cost_usd:
                    self.state.total_cost_usd = message.total_cost_usd

        except Exception as e:
            logger.error("Claude session error: %s", e, exc_info=True)
            self.state.last_output.append(f"Error: {e}")
        finally:
            self.state.running = False

    async def _pre_tool_hook(self, input_data: Any, tool_use_id: str, context: Any) -> dict:
        """Dummy hook required for can_use_tool to work in Python SDK."""
        return {"continue_": True}

    async def _can_use_tool(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        context: Any,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """Handle tool permission requests — intercepts AskUserQuestion."""
        if tool_name == "AskUserQuestion":
            logger.info("Claude is asking a question — waiting for user reply")
            questions = input_data.get("questions", [])

            # Create a future that the poller will resolve when user replies
            future = asyncio.get_running_loop().create_future()
            self.state.pending_question = PendingQuestion(
                questions=questions,
                answer_future=future,
            )

            # Wait for the user's answer (from /claude -reply)
            answers = await future
            self.state.pending_question = None

            return PermissionResultAllow(updated_input={
                "questions": questions,
                "answers": answers,
            })

        # Auto-approve all other tools
        return PermissionResultAllow(updated_input=input_data)

    async def reply(self, answer_text: str) -> bool:
        """Resolve a pending AskUserQuestion with the user's answer.

        Returns True if there was a pending question, False otherwise.
        """
        pq = self.state.pending_question
        if not pq:
            return False

        # Build answers dict keyed by question text
        answers = {}
        for q in pq.questions:
            question_text = q.get("question", "")
            options = q.get("options", [])

            # Try to match answer to an option label
            matched = False
            for opt in options:
                if opt["label"].lower() == answer_text.lower():
                    answers[question_text] = answer_text
                    matched = True
                    break

            # Try numeric selection
            if not matched:
                try:
                    idx = int(answer_text) - 1
                    if 0 <= idx < len(options):
                        answers[question_text] = options[idx]["label"]
                        matched = True
                except (ValueError, IndexError):
                    pass

            # Default: use raw text as answer
            if not matched:
                answers[question_text] = answer_text

        pq.answer_future.set_result(answers)
        return True

    def get_status(self) -> dict[str, Any]:
        """Return current session status."""
        status = "idle"
        if self.state.running:
            status = "waiting_for_input" if self.state.pending_question else "running"

        result: dict[str, Any] = {
            "status": status,
            "session_id": self.state.session_id,
            "cost_usd": round(self.state.total_cost_usd, 4),
        }

        if self.state.pending_question:
            result["pending_question"] = self.state.pending_question.questions

        # Last 3 output blocks
        if self.state.last_output:
            result["last_output"] = self.state.last_output[-3:]

        return result

    async def stop(self) -> None:
        """Cancel the running session."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.state.running = False
        self.state.pending_question = None
