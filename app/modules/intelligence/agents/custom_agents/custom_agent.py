import json
import logging
import os
from typing import AsyncGenerator, List

import httpx
from dotenv import load_dotenv
from langchain.schema import HumanMessage, SystemMessage
from langchain_core.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
)
from langchain_core.runnables import RunnableSequence
from sqlalchemy.orm import Session

from app.modules.auth.auth_service import AuthService
from app.modules.conversations.message.message_model import MessageType
from app.modules.conversations.message.message_schema import NodeContext
from app.modules.intelligence.agents.custom_agents.custom_agents_service import (
    CustomAgentsService,
)
from app.modules.intelligence.memory.chat_history_service import ChatHistoryService
from app.modules.intelligence.prompts.prompt_service import PromptService

logger = logging.getLogger(__name__)

load_dotenv()


class CustomAgent:
    def __init__(self, llm, db: Session, agent_id: str, user_id: str):
        self.llm = llm
        self.db = db
        self.agent_id = agent_id
        self.user_id = user_id
        self.history_manager = ChatHistoryService(db)
        self.prompt_service = PromptService(db)
        self.custom_agents_service = CustomAgentsService()
        self.chain = None
        self.base_url = os.getenv("POTPIE_PLUS_BASE_URL")

    async def _get_system_prompt(self) -> str:
        """Fetch system prompt from POTPIE_PLUS_BASE_URL with HMAC authentication"""
        try:
            user_id = self.user_id
            hmac_signature = AuthService.generate_hmac_signature(user_id)
            headers = {"X-HMAC-Signature": hmac_signature}

            url = f"{self.base_url}/custom-agents/agents/{self.agent_id}?user_id={user_id}"

            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                return data.get("system_prompt")
        except Exception as e:
            logger.error(f"Error fetching system prompt: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to fetch system prompt: {str(e)}")

    async def _create_chain(self) -> RunnableSequence:
        system_prompt = await self._get_system_prompt()
        if not system_prompt:
            raise ValueError(f"System prompt not found for agent {self.agent_id}")

        prompt_template = ChatPromptTemplate(
            messages=[
                SystemMessagePromptTemplate.from_template(system_prompt),
                MessagesPlaceholder(variable_name="history"),
                MessagesPlaceholder(variable_name="tool_results"),
            ]
        )
        return prompt_template | self.llm

    async def run(
        self,
        query: str,
        project_id: str,
        user_id: str,
        conversation_id: str,
        node_ids: List[NodeContext],
    ) -> AsyncGenerator[str, None]:
        try:
            if not self.chain:
                self.chain = await self._create_chain()

            history = self.history_manager.get_session_history(user_id, conversation_id)
            validated_history = [
                (
                    HumanMessage(content=str(msg))
                    if isinstance(msg, (str, int, float))
                    else msg
                )
                for msg in history
            ]
            custom_agent_result = await self.custom_agents_service.run_agent(
                self.agent_id, query, conversation_id, user_id, node_ids
            )

            tool_results = [
                SystemMessage(
                    content=f"Custom Agent result: {json.dumps(custom_agent_result)}"
                )
            ]

            inputs = {
                "history": validated_history,
                "tool_results": tool_results,
                "input": query,
            }

            logger.debug(f"Inputs to LLM: {inputs}")

            full_response = ""
            async for chunk in self.chain.astream(inputs):
                content = chunk.content if hasattr(chunk, "content") else str(chunk)
                full_response += content
                self.history_manager.add_message_chunk(
                    conversation_id,
                    content,
                    MessageType.AI_GENERATED,
                )
                yield json.dumps({"message": content, "citations": []})

            logger.debug(f"Full LLM response: {full_response}")
            self.history_manager.flush_message_buffer(
                conversation_id, MessageType.AI_GENERATED
            )

        except Exception as e:
            logger.error(f"Error during CustomAgent run: {str(e)}", exc_info=True)
            yield f"An error occurred: {str(e)}"
