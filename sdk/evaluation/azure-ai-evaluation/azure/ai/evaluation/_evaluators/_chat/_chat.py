# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------
import json
import logging
from concurrent.futures import as_completed
from typing import Dict, List, Union

import numpy as np

from promptflow.tracing import ThreadPoolExecutorWithContext as ThreadPoolExecutor

from .._coherence import CoherenceEvaluator
from .._fluency import FluencyEvaluator
from .._groundedness import GroundednessEvaluator
from ..._model_configurations import AzureOpenAIModelConfiguration, OpenAIModelConfiguration
from .._relevance import RelevanceEvaluator
from .retrieval import RetrievalChatEvaluator
from azure.ai.evaluation._exceptions import EvaluationException, ErrorBlame, ErrorCategory, ErrorTarget

logger = logging.getLogger(__name__)


class ChatEvaluator:
    """
    Initialize a chat evaluator configured for a specific Azure OpenAI model.

    :param model_config: Configuration for the Azure OpenAI model.
    :type model_config: Union[~azure.ai.evaluation.AzureOpenAIModelConfiguration,
        ~azure.ai.evaluation.OpenAIModelConfiguration]
    :param eval_last_turn: Set to True to evaluate only the most recent exchange in the dialogue,
        focusing on the latest user inquiry and the assistant's corresponding response. Defaults to False
    :type eval_last_turn: bool
    :param parallel: If True, use parallel execution for evaluators. Else, use sequential execution.
        Default is True.
    :type parallel: bool
    :return: A function that evaluates and generates metrics for "chat" scenario.
    :rtype: Callable

    **Usage**

    .. code-block:: python

        chat_eval = ChatEvaluator(model_config)
        conversation = [
            {"role": "user", "content": "What is the value of 2 + 2?"},
            {"role": "assistant", "content": "2 + 2 = 4", "context": {
                "citations": [
                        {"id": "math_doc.md", "content": "Information about additions: 1 + 2 = 3, 2 + 2 = 4"}
                    ]
                }
            }
        ]
        result = chat_eval(conversation=conversation)

    **Output format**

    .. code-block:: python

        {
            "evaluation_per_turn": {
                "gpt_retrieval": [1.0, 2.0],
                "gpt_groundedness": [5.0, 2.0],
                "gpt_relevance": [3.0, 5.0],
                "gpt_coherence": [1.0, 2.0],
                "gpt_fluency": [3.0, 5.0]
            }
            "gpt_retrieval": 1.5,
            "gpt_groundedness": 3.5,
            "gpt_relevance": 4.0,
            "gpt_coherence": 1.5,
            "gpt_fluency": 4.0
        }
    """

    def __init__(
        self,
        model_config: dict,
        eval_last_turn: bool = False,
        parallel: bool = True,
    ):
        self._eval_last_turn = eval_last_turn
        self._parallel = parallel

        # TODO: Need a built-in evaluator for retrieval. It needs to be added to `self._rag_evaluators` collection
        self._rag_evaluators = [
            GroundednessEvaluator(model_config),
            RelevanceEvaluator(model_config),
        ]
        self._non_rag_evaluators = [
            CoherenceEvaluator(model_config),
            FluencyEvaluator(model_config),
        ]
        # TODO: Temporary workaround to close the gap of missing retrieval score
        # https://msdata.visualstudio.com/Vienna/_workitems/edit/3186644
        # For long term, we need to add a built-in evaluator for retrieval after prompt is generalized for QA and Chat
        self._retrieval_chat_evaluator = RetrievalChatEvaluator(model_config)

    def __call__(self, *, conversation, **kwargs):
        """
        Evaluates chat scenario.

        :keyword conversation: The conversation to be evaluated. Each turn should have "role" and "content" keys.
            "context" key is optional for assistant's turn and should have "citations" key with list of citations.
        :paramtype conversation: List[Dict]
        :return: The scores for Chat scenario.
        :rtype: dict
        """
        self._validate_conversation(conversation)

        # Extract queries, responses and contexts from conversation
        queries = []
        responses = []
        contexts = []

        if self._eval_last_turn:
            # Process only the last two turns if _eval_last_turn is True
            conversation_slice = conversation[-2:] if len(conversation) >= 2 else conversation
        else:
            conversation_slice = conversation

        for each_turn in conversation_slice:
            role = each_turn["role"]
            if role == "user":
                queries.append(each_turn["content"])
            elif role == "assistant":
                responses.append(each_turn["content"])
                if "context" in each_turn and "citations" in each_turn["context"]:
                    citations = json.dumps(each_turn["context"]["citations"])
                    contexts.append(citations)

        # Select evaluators to be used for evaluation
        compute_rag_based_metrics = True
        if len(responses) != len(contexts):
            safe_message = (
                "Skipping rag based metrics as we need citations or "
                "retrieved_documents in context key of every assistant's turn"
            )
            logger.warning(safe_message)
            compute_rag_based_metrics = False

        selected_evaluators = []
        selected_evaluators.extend(self._non_rag_evaluators)
        if compute_rag_based_metrics:
            selected_evaluators.extend(self._rag_evaluators)

        # Evaluate each turn
        per_turn_results = []
        for turn_num in range(len(queries)):
            current_turn_result = {}

            if self._parallel:
                # Parallel execution
                with ThreadPoolExecutor() as executor:
                    future_to_evaluator = {
                        executor.submit(
                            self._evaluate_turn, turn_num, queries, responses, contexts, evaluator
                        ): evaluator
                        for evaluator in selected_evaluators
                    }

                    for future in as_completed(future_to_evaluator):
                        result = future.result()
                        current_turn_result.update(result)
            else:
                # Sequential execution
                for evaluator in selected_evaluators:
                    async_evaluator = evaluator._to_async()
                    result = self._evaluate_turn(turn_num, queries, responses, contexts, async_evaluator)
                    current_turn_result.update(result)

            per_turn_results.append(current_turn_result)

        # Aggregate results
        # Final aggregated results for a conversation will look like:
        #     "gpt_groundedness": 2.0, # Mean of all groundedness scores
        #     "evaluation_per_turn": {
        #         "gpt_groundedness": {
        #             "score": [1.0, ...],
        #             "reason": ["reason1", ...],
        #         },
        #     },
        # }
        aggregated = self._aggregate_results(per_turn_results)

        # Run RetrievalChatEvaluator and merge the results
        if compute_rag_based_metrics:
            retrieval_score = self._retrieval_chat_evaluator(conversation=conversation_slice)
            aggregated["gpt_retrieval"] = retrieval_score["gpt_retrieval"]
            aggregated["evaluation_per_turn"]["gpt_retrieval"] = retrieval_score["evaluation_per_turn"]["gpt_retrieval"]
            aggregated = dict(sorted(aggregated.items()))

        return aggregated

    def _evaluate_turn(self, turn_num, queries, responses, contexts, evaluator):
        try:
            query = queries[turn_num] if turn_num < len(queries) else ""
            response = responses[turn_num] if turn_num < len(responses) else ""
            context = contexts[turn_num] if turn_num < len(contexts) else ""

            score = evaluator(query=query, response=response, context=context)

            return score
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning(
                f"Evaluator {evaluator.__class__.__name__} failed for turn {turn_num + 1} with exception: {e}"
            )
            return {}

    def _aggregate_results(self, per_turn_results: List[Dict]):
        scores = {}
        reasons = {}

        for turn in per_turn_results:
            for metric, value in turn.items():
                if "reason" in metric:
                    if metric not in reasons:
                        reasons[metric] = []
                    reasons[metric].append(value)
                else:
                    if metric not in scores:
                        scores[metric] = []
                    scores[metric].append(value)

        aggregated = {}
        evaluation_per_turn = {}

        for metric, values in scores.items():
            aggregated[metric] = np.nanmean(values)

            # Prepare per-turn evaluations
            evaluation_per_turn[metric] = {"score": values}
            reason_key = f"{metric}_reason"
            if reason_key in reasons:
                evaluation_per_turn[metric]["reason"] = reasons[reason_key]

        aggregated["evaluation_per_turn"] = evaluation_per_turn

        return aggregated

    def _validate_conversation(self, conversation: List[Dict]):
        if conversation is None or not isinstance(conversation, list):
            msg = "conversation must be a list of dictionaries"
            raise EvaluationException(
                message=msg,
                internal_message=msg,
                target=ErrorTarget.CHAT_EVALUATOR,
                category=ErrorCategory.INVALID_VALUE,
                blame=ErrorBlame.USER_ERROR,
            )

        expected_role = "user"
        for turn_num, turn in enumerate(conversation):
            one_based_turn_num = turn_num + 1

            if not isinstance(turn, dict):
                msg = f"Each turn in 'conversation' must be a dictionary. Turn number: {one_based_turn_num}"
                raise EvaluationException(
                    message=msg,
                    internal_message=msg,
                    target=ErrorTarget.CHAT_EVALUATOR,
                    category=ErrorCategory.INVALID_VALUE,
                    blame=ErrorBlame.USER_ERROR,
                )

            if "role" not in turn or "content" not in turn:
                msg = f"Each turn in 'conversation' must have 'role' and 'content' keys. Turn number: {one_based_turn_num}"
                raise EvaluationException(
                    message=msg,
                    internal_message=msg,
                    target=ErrorTarget.CHAT_EVALUATOR,
                    category=ErrorCategory.INVALID_VALUE,
                    blame=ErrorBlame.USER_ERROR,
                )

            if turn["role"] != expected_role:
                msg = f"Expected role {expected_role} but got {turn['role']}. Turn number: {one_based_turn_num}"
                raise EvaluationException(
                    message=msg,
                    internal_message=msg,
                    target=ErrorTarget.CHAT_EVALUATOR,
                    category=ErrorCategory.INVALID_VALUE,
                    blame=ErrorBlame.USER_ERROR,
                )

            if not isinstance(turn["content"], str):
                msg = f"Content in each turn must be a string. Turn number: {one_based_turn_num}"
                raise EvaluationException(
                    message=msg,
                    internal_message=msg,
                    target=ErrorTarget.CHAT_EVALUATOR,
                    category=ErrorCategory.INVALID_VALUE,
                    blame=ErrorBlame.USER_ERROR,
                )

            if turn["role"] == "assistant" and "context" in turn:
                if not isinstance(turn["context"], dict):
                    msg = f"Context in each assistant's turn must be a dictionary. Turn number: {one_based_turn_num}"
                    raise EvaluationException(
                        message=msg,
                        internal_message=msg,
                        target=ErrorTarget.CHAT_EVALUATOR,
                        category=ErrorCategory.INVALID_VALUE,
                        blame=ErrorBlame.USER_ERROR,
                    )

                if "citations" not in turn["context"]:
                    msg = (
                        f"Context in each assistant's turn must have 'citations' key. Turn number: {one_based_turn_num}"
                    )
                    raise EvaluationException(
                        message=msg,
                        internal_message=msg,
                        target=ErrorTarget.CHAT_EVALUATOR,
                        category=ErrorCategory.MISSING_FIELD,
                        blame=ErrorBlame.USER_ERROR,
                    )

                if not isinstance(turn["context"]["citations"], list):
                    msg = f"'citations' in context must be a list. Turn number: {one_based_turn_num}"
                    raise EvaluationException(
                        message=msg,
                        internal_message=msg,
                        target=ErrorTarget.CHAT_EVALUATOR,
                        category=ErrorCategory.INVALID_VALUE,
                        blame=ErrorBlame.USER_ERROR,
                    )

                for citation_num, citation in enumerate(turn["context"]["citations"]):
                    if not isinstance(citation, dict):
                        msg = f"Each citation in 'citations' must be a dictionary. Turn number: {one_based_turn_num}, Citation number: {citation_num + 1}"
                        raise EvaluationException(
                            message=msg,
                            internal_message=msg,
                            target=ErrorTarget.CHAT_EVALUATOR,
                            category=ErrorCategory.INVALID_VALUE,
                            blame=ErrorBlame.USER_ERROR,
                        )

            # Toggle expected role for the next turn
            expected_role = "user" if expected_role == "assistant" else "assistant"

        # Ensure the conversation ends with an assistant's turn
        if expected_role != "user":
            msg = "The conversation must end with an assistant's turn."
            raise EvaluationException(
                message=msg,
                internal_message=msg,
                target=ErrorTarget.CHAT_EVALUATOR,
                category=ErrorCategory.INVALID_VALUE,
                blame=ErrorBlame.USER_ERROR,
            )
