# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
""" Transcribe API Mutation Processor
"""
import asyncio
from datetime import datetime, timedelta
from os import getenv
from typing import TYPE_CHECKING, Any, Coroutine, Dict, List, Literal, Optional
import uuid
import json

# third-party imports from Lambda layer
import boto3
from botocore.config import Config as BotoCoreConfig
from aws_lambda_powertools import Logger
from gql.client import AsyncClientSession as AppsyncAsyncClientSession
from gql.dsl import DSLMutation, DSLSchema, dsl_gql
from graphql.language.printer import print_ast


# custom utils/helpers imports from Lambda layer
# pylint: disable=import-error
from appsync_utils import execute_gql_query_with_retries
from graphql_helpers import (
    call_fields,
    transcript_segment_fields,
    transcript_segment_sentiment_fields,
)
from lex_utils import recognize_text_lex
from lambda_utils import invoke_lambda
from sentiment import ComprehendWeightedSentiment
# pylint: enable=import-error

if TYPE_CHECKING:
    from mypy_boto3_lexv2_runtime.type_defs import RecognizeTextResponseTypeDef
    from mypy_boto3_lexv2_runtime.client import LexRuntimeV2Client
    from mypy_boto3_lambda.type_defs import InvocationResponseTypeDef
    from mypy_boto3_lambda.client import LambdaClient
    from mypy_boto3_comprehend.client import ComprehendClient
    from mypy_boto3_comprehend.type_defs import DetectSentimentResponseTypeDef
    from mypy_boto3_comprehend.literals import LanguageCodeType
    from boto3 import Session as Boto3Session
else:
    LexRuntimeV2Client = object
    RecognizeTextResponseTypeDef = object
    LambdaClient = object
    InvocationResponseTypeDef = object
    ComprehendClient = object
    DetectSentimentResponseTypeDef = object
    LanguageCodeType = object
    Boto3Session = object

BOTO3_SESSION: Boto3Session = boto3.Session()
CLIENT_CONFIG = BotoCoreConfig(
    retries={"mode": "adaptive", "max_attempts": 3},
)
IS_SENTIMENT_ANALYSIS_ENABLED = getenv("IS_SENTIMENT_ANALYSIS_ENABLED", "true").lower() == "true"
if IS_SENTIMENT_ANALYSIS_ENABLED:
    COMPREHEND_CLIENT: ComprehendClient = BOTO3_SESSION.client("comprehend", config=CLIENT_CONFIG)
    COMPREHEND_LANGUAGE_CODE = getenv("COMPREHEND_LANGUAGE_CODE", "en")

IS_LEX_AGENT_ASSIST_ENABLED = False
LEXV2_CLIENT: Optional[LexRuntimeV2Client] = None
LEX_BOT_ID: str
LEX_BOT_ALIAS_ID: str
LEX_BOT_LOCALE_ID: str

IS_LAMBDA_AGENT_ASSIST_ENABLED = False
LAMBDA_CLIENT: Optional[LexRuntimeV2Client] = None
LAMBDA_AGENT_ASSIST_FUNCTION_ARN: str

LOGGER = Logger(location="%(filename)s:%(lineno)d - %(funcName)s()")
EVENT_LOOP = asyncio.get_event_loop()

CALL_EVENT_TYPE_TO_STATUS = {
    "START": "STARTED",
    "START_TRANSCRIPT": "TRANSCRIBING",
    "CONTINUE_TRANSCRIPT": "TRANSCRIBING",
    "CONTINUE": "TRANSCRIBING",
    "END_TRANSCRIPT": "ENDED",
    "TRANSCRIPT_ERROR": "ERRORED ",
    "ERROR": "ERRORED ",
    "END": "ENDED",
    "ADD_CHANNEL_S3_RECORDING_URL": "ENDED",
    "ADD_S3_RECORDING_URL": "ENDED",
} 

# DEFAULT_CUSTOMER_PHONE_NUMBER used to replace an invalid CustomerPhoneNumber
# such as seen from calls originating with Skype ('anonymous')
DEFAULT_CUSTOMER_PHONE_NUMBER = getenv("DEFAULT_CUSTOMER_PHONE_NUMBER", "+18005550000")

# Get value for DynamboDB TTL field
DYNAMODB_EXPIRATION_IN_DAYS = getenv("DYNAMODB_EXPIRATION_IN_DAYS", "90")
def get_ttl():
    return int((datetime.utcnow() + timedelta(days=int(DYNAMODB_EXPIRATION_IN_DAYS))).timestamp())

##########################################################################
# Transcripts
##########################################################################
def transform_segment_to_add_transcript(message: Dict) -> Dict[str, object]:
    """Transforms Kinesis Stream Transcript Payload to addTranscript API"""

    call_id: str = message["CallId"]
    channel: str = message["Channel"]
    stream_arn: str = message["StreamArn"]
    transaction_id: str = message["TransactionId"]
    segment_id: str = message["SegmentId"]
    start_time: float = message["StartTime"]
    end_time: float = message["EndTime"]
    transcript: str = message["Transcript"]
    is_partial: bool = message["IsPartial"]
    created_at = datetime.utcnow().astimezone().isoformat()


    return dict(
        CallId=call_id,
        Channel=channel,
        StreamArn=stream_arn,
        TransactionId=transaction_id,
        SegmentId=segment_id,
        StartTime=start_time,
        EndTime=end_time,
        Transcript=transcript,
        IsPartial=is_partial,
        CreatedAt=created_at,
        ExpiresAfter=get_ttl(),
        Status="TRANSCRIBING",
    )

def add_transcript_segments(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Transcript Segment GraphQL Mutation"""
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    tasks = []
        
    transcript_segment = {
        **transform_segment_to_add_transcript({**message}),
    }

    if transcript_segment:
        query = dsl_gql(
            DSLMutation(
                schema.Mutation.addTranscriptSegment.args(input=transcript_segment).select(
                    *transcript_segment_fields(schema),
                )
            )
        )
        ignore_exception_fn = lambda e: True if (e["message"] == 'item put condition failure') else False
        tasks.append(
            execute_gql_query_with_retries(
                query,
                client_session=appsync_session,
                logger=LOGGER,
                should_ignore_exception_fn = ignore_exception_fn, 
            ),
        )

    return tasks

async def detect_sentiment(text: str) -> DetectSentimentResponseTypeDef:
    # text_hash = hash(text)
    # if text_hash in self._sentiment_cache:
    #     LOGGER.debug("using sentiment cache on text: [%s]", text)
    #     return self._sentiment_cache[text_hash]

    LOGGER.debug("detect sentiment on text: [%s]", text)
    loop = asyncio.get_running_loop()
    sentiment_future = loop.run_in_executor(
        None,
        lambda: COMPREHEND_CLIENT.detect_sentiment(
            Text=text,
            LanguageCode=COMPREHEND_LANGUAGE_CODE,
        ),
    )
    results = await asyncio.gather(sentiment_future)
    result = results[0]
    # self._sentiment_cache[text_hash] = result
    return result

async def add_sentiment_to_transcript(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
):
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)
        
    transcript_segment = {
        **transform_segment_to_add_transcript({**message}),
    }

    text = transcript_segment["Transcript"]
    LOGGER.debug("detect sentiment on text: [%s]", text)

    
    sentiment_response:DetectSentimentResponseTypeDef = await detect_sentiment(text)
    LOGGER.debug("Sentiment Response: ", extra=sentiment_response)

    result = {}
    comprehend_weighted_sentiment = ComprehendWeightedSentiment()

    sentiment = {
        k: v for k, v in sentiment_response.items() if k in ["Sentiment", "SentimentScore"]
    }
    if sentiment:
        if sentiment.get("Sentiment") in ["POSITIVE", "NEGATIVE"]:
            sentiment["SentimentWeighted"] = comprehend_weighted_sentiment.get_weighted_sentiment_score(
                    sentiment_response=sentiment_response
                )
    
        transcript_segment_with_sentiment = {
            **transcript_segment,
            **sentiment
        }
        
        query = dsl_gql(
            DSLMutation(
                schema.Mutation.addTranscriptSegment.args(input=transcript_segment_with_sentiment).select(
                    *transcript_segment_fields(schema),
                    *transcript_segment_sentiment_fields(schema),
                )
            )
        )

        result = await execute_gql_query_with_retries(
            query,
            client_session=appsync_session,
            logger=LOGGER,
        )
        
    return result

def add_transcript_sentiment_analysis(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Transcript Sentiment GraphQL Mutation"""

    tasks = []

    task = add_sentiment_to_transcript(message, appsync_session)
    tasks.append(task)

    return tasks

async def execute_create_call_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)
    
    query = dsl_gql(
        DSLMutation(
            schema.Mutation.createCall.args(input=message).select(
                schema.CreateCallOutput.CallId
            )
        )
    )
    
    result = await execute_gql_query_with_retries(
                        query,
                        client_session=appsync_session,
                        logger=LOGGER,
                    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_update_call_status_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    status = CALL_EVENT_TYPE_TO_STATUS.get(message.get("EventType"))
    if not status:
        error_message = "unrecognized status from event type in update call"
        raise TypeError(error_message)

    if status == "STARTED":
        # STARTED status is set by createCall - skip update mutation
        return {"ok": True}

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateCallStatus.args(input={**message, "Status": status}).select(
                *call_fields(schema)
            )
        )
    )
    result = await execute_gql_query_with_retries(
                        query,
                        client_session=appsync_session,
                        logger=LOGGER,
                    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_add_s3_recording_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    recording_url = message.get("RecordingUrl")
    if not recording_url:
        error_message = "recording url doesn't exist in add s3 recording url event"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateRecordingUrl.args(
                input={**message, "RecordingUrl": recording_url}
            ).select(*call_fields(schema))
        )
    )
    
    result = await execute_gql_query_with_retries(
                        query,
                        client_session=appsync_session,
                        logger=LOGGER,
                    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

async def execute_update_agent_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> Dict:

    agentId = message.get("AgentId")
    if not agentId:
        error_message = "AgentId doesn't exist in UPDATE_AGENT event"
        raise TypeError(error_message)

    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    query = dsl_gql(
        DSLMutation(
            schema.Mutation.updateAgent.args(
                input={**message, "AgentId": agentId}
            ).select(*call_fields(schema))
        )
    )
    
    result = await execute_gql_query_with_retries(
                        query,
                        client_session=appsync_session,
                        logger=LOGGER,
                    )

    query_string = print_ast(query)
    LOGGER.debug("query result", extra=dict(query=query_string, result=result))

    return result

##########################################################################
# Lex Agent Assist
##########################################################################
def is_qnabot_noanswer(bot_response):
    if (
        bot_response["sessionState"]["dialogAction"]["type"] == "Close"
        and (
            bot_response["sessionState"]
            .get("sessionAttributes", {})
            .get("qnabot_gotanswer")
            == "false"
        )
    ):
        return True
    return False

def get_lex_agent_assist_message(bot_response):
    message = ""
    if is_qnabot_noanswer(bot_response):
        # ignore 'noanswer' responses from QnABot
        LOGGER.debug("QnABot \"Dont't know\" response - ignoring")
        return ""
    # Use markdown if present in appContext.altMessages.markdown session attr (Lex Web UI / QnABot)
    appContextJSON = bot_response.get("sessionState",{}).get("sessionAttributes",{}).get("appContext")
    if appContextJSON:
        appContext = json.loads(appContextJSON)
        markdown = appContext.get("altMessages",{}).get("markdown")
        if markdown:
            message = markdown
    # otherwise use bot message
    if not message and "messages" in bot_response and bot_response["messages"]:
        message = bot_response["messages"][0]["content"]
    return message

async def send_lex_agent_assist(
    transcript_segment_args: Dict[str, Any],
    content: str,
    appsync_session: AppsyncAsyncClientSession,
):
    """Sends Lex Agent Assist Requests"""
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    call_id = transcript_segment_args["CallId"]
    
    LOGGER.debug("Bot Request: %s", content)

    bot_response: RecognizeTextResponseTypeDef = await recognize_text_lex(
        text=content,
        session_id=call_id,
        lex_client=LEXV2_CLIENT,
        bot_id=LEX_BOT_ID,
        bot_alias_id=LEX_BOT_ALIAS_ID,
        locale_id=LEX_BOT_LOCALE_ID,
    )
    
    LOGGER.debug("Bot Response: ", extra=bot_response)

    result = {}
    transcript = get_lex_agent_assist_message(bot_response)
    if transcript:
        transcript_segment = {**transcript_segment_args, "Transcript": transcript}

        query = dsl_gql(
            DSLMutation(
                schema.Mutation.addTranscriptSegment.args(input=transcript_segment).select(
                    *transcript_segment_fields(schema),
                )
            )
        )

        result = await execute_gql_query_with_retries(
            query,
            client_session=appsync_session,
            logger=LOGGER,
        )

    return result

def add_lex_agent_assistances(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Lex Agent Assist GraphQL Mutations"""
    # pylint: disable=too-many-locals
    call_id: str = message["CallId"]
    channel: str = message["Channel"]
    is_partial: bool = message["IsPartial"]
    segment_id: str = message["SegmentId"]
    start_time: float = message["StartTime"]
    end_time: float = message["EndTime"]
    end_time = float(end_time) + 0.001 # UI sort order
    transcript: str = message["Transcript"]
    created_at = datetime.utcnow().astimezone().isoformat()

    send_lex_agent_assist_args = []
    if (channel == "CALLER" and not is_partial):
        send_lex_agent_assist_args.append(
                dict(
                    content=transcript,
                    transcript_segment_args=dict(
                        CallId=call_id,
                        Channel="AGENT_ASSISTANT",
                        CreatedAt=created_at,
                        EndTime=end_time,
                        ExpiresAfter=get_ttl(),
                        IsPartial=is_partial,
                        SegmentId=str(uuid.uuid4()),
                        StartTime=start_time,
                        Status="TRANSCRIBING",
                    ),
                )
            )
            
    tasks = []
    for agent_assist_args in send_lex_agent_assist_args:
        task = send_lex_agent_assist(
            appsync_session=appsync_session,
            **agent_assist_args,
        )
        tasks.append(task)

    return tasks

##########################################################################
# Lambda Agent Assist
##########################################################################

def get_lambda_agent_assist_message(lambda_response):
    message = ""
    try:
        payload = json.loads(lambda_response.get("Payload").read().decode("utf-8"))
        # Lambda result payload should include field 'message'
        message = payload["message"]
    except Exception as error:
        LOGGER.error(
            "Agent assist Lambda result payload parsing exception. Lambda must return object with key 'message'",
            extra=error,
        )
    return message

async def send_lambda_agent_assist(
    transcript_segment_args: Dict[str, Any],
    content: str,
    appsync_session: AppsyncAsyncClientSession,
):
    """Sends Lambda Agent Assist Requests"""
    if not appsync_session.client.schema:
        raise ValueError("invalid AppSync schema")
    schema = DSLSchema(appsync_session.client.schema)

    call_id = transcript_segment_args["CallId"]

    payload = {
        'text': content,
        'call_id': call_id,
        'transcript_segment_args': transcript_segment_args
    }
    
    LOGGER.debug("Agent Assist Lambda Request: %s", content)

    lambda_response: InvocationResponseTypeDef = await invoke_lambda(
        payload=payload,
        lambda_client=LAMBDA_CLIENT,
        lambda_agent_assist_function_arn=LAMBDA_AGENT_ASSIST_FUNCTION_ARN,
    )
    
    LOGGER.debug("Agent Assist Lambda Response: ", extra=lambda_response)

    result = {}
    transcript = get_lambda_agent_assist_message(lambda_response)
    if transcript:
        transcript_segment = {**transcript_segment_args, "Transcript": transcript}

        query = dsl_gql(
            DSLMutation(
                schema.Mutation.addTranscriptSegment.args(input=transcript_segment).select(
                    *transcript_segment_fields(schema),
                )
            )
        )

        result = await execute_gql_query_with_retries(
            query,
            client_session=appsync_session,
            logger=LOGGER,
        )

    return result

def add_lambda_agent_assistances(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
) -> List[Coroutine]:
    """Add Lambda Agent Assist GraphQL Mutations"""
    # pylint: disable=too-many-locals
    call_id: str = message["CallId"]
    channel: str = message["Channel"]
    is_partial: bool = message["IsPartial"]
    segment_id: str = message["SegmentId"]
    start_time: float = message["StartTime"]
    end_time: float = message["EndTime"]
    end_time = float(end_time) + 0.001 # UI sort order
    transcript: str = message["Transcript"]
    created_at = datetime.utcnow().astimezone().isoformat()

    send_lambda_agent_assist_args = []
    if (channel == "CALLER" and not is_partial):
        send_lambda_agent_assist_args.append(
                dict(
                    content=transcript,
                    transcript_segment_args=dict(
                        CallId=call_id,
                        Channel="AGENT_ASSISTANT",
                        CreatedAt=created_at,
                        EndTime=end_time,
                        ExpiresAfter=get_ttl(),
                        IsPartial=is_partial,
                        SegmentId=str(uuid.uuid4()),
                        StartTime=start_time,
                        Status="TRANSCRIBING",
                    ),
                )
            )

    tasks = []
    for agent_assist_args in send_lambda_agent_assist_args:
        task = send_lambda_agent_assist(
            appsync_session=appsync_session,
            **agent_assist_args,
        )
        tasks.append(task)

    return tasks
    
async def execute_process_event_api_mutation(
    message: Dict[str, Any],
    appsync_session: AppsyncAsyncClientSession,
    agent_assist_args: Dict[str, Any],
) -> Dict[Literal["successes", "errors"], List]:

    """Executes AppSync API Mutation"""
    # pylint: disable=global-statement
    global LEXV2_CLIENT
    global IS_LEX_AGENT_ASSIST_ENABLED
    global LEX_BOT_ID
    global LEX_BOT_ALIAS_ID
    global LEX_BOT_LOCALE_ID
    global LAMBDA_CLIENT
    global LAMBDA_AGENT_ASSIST_FUNCTION_ARN
    # pylint: enable=global-statement

    LEXV2_CLIENT = agent_assist_args.get("lex_client")
    IS_LEX_AGENT_ASSIST_ENABLED = LEXV2_CLIENT is not None
    LEX_BOT_ID = agent_assist_args.get("lex_bot_id", "")
    LEX_BOT_ALIAS_ID = agent_assist_args.get("lex_bot_alias_id", "")
    LEX_BOT_LOCALE_ID = agent_assist_args.get("lex_bot_locale_id", "")
    LAMBDA_CLIENT = agent_assist_args.get("lambda_client")
    IS_LAMBDA_AGENT_ASSIST_ENABLED = LAMBDA_CLIENT is not None
    LAMBDA_AGENT_ASSIST_FUNCTION_ARN = agent_assist_args.get("lambda_agent_assist_function_arn", "")   

    return_value: Dict[Literal["successes", "errors"], List] = {
        "successes": [],
        "errors": [],
    }

    message["ExpiresAfter"] = get_ttl()
    event_type = message.get("EventType", "")

    if event_type == "START":
        # CREATE CALL
        LOGGER.debug("CREATE CALL") 

        response = await execute_create_call_mutation(
                            message=message, 
                            appsync_session=appsync_session
                        )
                        
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type in [
        "START_TRANSCRIPT",
        "CONTINUE_TRANSCRIPT",
        "CONTINUE",
        "END_TRANSCRIPT",
        "TRANSCRIPT_ERROR",
        "ERROR",
        "END",
        "ADD_CHANNEL_S3_RECORDING_URL",]:
        # UPDATE STATUS
        LOGGER.debug("update status")
        response = await execute_update_call_status_mutation(
                                message=message,
                                appsync_session=appsync_session
                        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)


    elif event_type == "ADD_TRANSCRIPT_SEGMENT":
        # UPDATE STATUS
        LOGGER.debug("Add Transcript Segment")
        add_transcript_tasks = add_transcript_segments(
            message=message,
            appsync_session=appsync_session,
        )

        add_transcript_sentiment_tasks = []
        if IS_SENTIMENT_ANALYSIS_ENABLED and not message.get("IsPartial", True):
            add_transcript_sentiment_tasks = add_transcript_sentiment_analysis(
                message=message,
                appsync_session=appsync_session,
            )

        add_lex_agent_assists_tasks = []
        if IS_LEX_AGENT_ASSIST_ENABLED:
            add_lex_agent_assists_tasks = add_lex_agent_assistances(
                    message=message,
                    appsync_session=appsync_session,
                )

        add_lambda_agent_assists_tasks = []
        if IS_LAMBDA_AGENT_ASSIST_ENABLED:
            add_lambda_agent_assists_tasks = add_lambda_agent_assistances(
                    message=message,
                    appsync_session=appsync_session,
                )

        task_responses = await asyncio.gather(
            *add_transcript_tasks,
            *add_transcript_sentiment_tasks,
            *add_lex_agent_assists_tasks,
            *add_lambda_agent_assists_tasks,
            return_exceptions=True,
        )

        for response in task_responses:
            if isinstance(response, Exception):
                return_value["errors"].append(response)
            else:
                return_value["successes"].append(response)

    elif event_type == "ADD_S3_RECORDING_URL":
        # ADD S3 RECORDING URL 
        LOGGER.debug("Add recording url")
        response = await execute_add_s3_recording_mutation(
                                message=message,
                                appsync_session=appsync_session
                        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    elif event_type == "UPDATE_AGENT":
        # UPDATE AGENT 
        LOGGER.debug("Update AgentId for call")
        response = await execute_update_agent_mutation(
                                message=message,
                                appsync_session=appsync_session
                        )
        if isinstance(response, Exception):
            return_value["errors"].append(response)
        else:
            return_value["successes"].append(response)

    else:
        LOGGER.warning("unknown event type [%s]", event_type)
        

    return return_value
