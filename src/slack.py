import json
import os

from fastapi import Request, Response
import modal
from pydantic import BaseModel
from src.core.entities import FileChangeRequest, Function, PullRequest, Snippet

from src.utils.constants import API_NAME, BOT_TOKEN_NAME, DB_NAME, SLACK_NAME
from src.core.prompts import slack_slash_command_prompt

image = modal.Image \
    .debian_slim() \
    .apt_install("git") \
    .pip_install(
        "slack-sdk",
        "PyGithub",
        "gitpython",
        "openai",
        "anthropic",
        "loguru",
        "tqdm",
        "highlight-io",
        "posthog"
    )
stub = modal.Stub(SLACK_NAME)
secrets = [
    modal.Secret.from_name("slack"),
    modal.Secret.from_name("openai-secret"),
    modal.Secret.from_name(BOT_TOKEN_NAME),
]

functions = [
    Function(
        name="create_pr",
        description="Creates a PR.",
        parameters={
            "properties": {
                "plan": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "The file path to change."
                            },
                            "instructions": {
                                "type": "string",
                                "description": "Detailed description of what the change as a list."
                            },
                        },
                        "required": ["file_path", "instructions"]
                    },
                    "description": "A list of files to modify or create and instructions for what to modify or create."
                },
                "title": {
                    "type": "string",
                    "description": "Title of PR",
                },
                "summary": {
                    "type": "string",
                    "description": "Summary of PR",
                },
                "branch": {
                    "type": "string",
                    "description": "Name of branch to create PR in.",
                },
            }
        }
    ),
    Function(
        name="get_relevant_snippets",
        description="Search engine for relevant snippets in the current repo in natural language.",
        parameters={
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query to search."
                }
            }
        }
    )
]

installation_id = 37093089

pr_format = """I'm going to create a PR with the following:

*{title}*

{summary}

I have the following plan:
{plan}
"""

class SlackSlashCommandRequest(BaseModel):
    channel_name: str
    channel_id: str
    text: str
    user_name: str
    user_id: str

@stub.function(
    image=image,
    secrets=secrets,
)
def reply_slack(request: SlackSlashCommandRequest):
    import slack_sdk
    import github
    from loguru import logger
    from src.core.sweep_bot import SweepBot
    from src.utils.github_utils import get_github_client

    create_pr = modal.Function.lookup(API_NAME, "create_pr")

    repo_name = "sweepai/dummy-repo"

    g = get_github_client(installation_id)
    repo = g.get_repo(repo_name)
    sweep_bot = SweepBot(repo=repo)

    client = slack_sdk.WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    thread = client.chat_postMessage(
        channel=request.channel_id,
        text=f">{request.text}\n- <@{request.user_name}>",
    )

    default_snippets = [
        Snippet(file_path="app/test.py", start=0, end=2, content=""),
        Snippet(file_path="README.md", start=0, end=1, content=""),
        Snippet(file_path="HelloWorld.tsx", start=0, end=11, content=""),
    ]
    sweep_bot.populate_snippets(default_snippets)

    try:
        logger.info("Fetching relevant snippets...")
        searching_message = client.chat_postMessage(
            channel=request.channel_id,
            text="Searching for relevant snippets...",
            thread_ts=thread["ts"],
        )
        snippets = default_snippets
        # snippets = sweep_bot.search_snippets(
        #     request.text,
        #     installation_id=installation_id
        # )
        message = "Some relevant snippets I found:\n\n"
        message += "\n".join(f"{snippet.get_slack_link(repo_name)}\n```{snippet.get_preview()}```" for snippet in snippets)
        client.chat_update(
            channel=request.channel_id,
            ts=searching_message["ts"],
            text=message,
        )
        prompt = slack_slash_command_prompt.format(
            relevant_snippets="\n".join([snippet.xml for snippet in snippets]),
            relevant_directories="\n".join([snippet.file_path for snippet in snippets]),
            repo_name=repo_name,
            repo_description=repo.description,
            username=request.user_name,
            query=request.text
        )
        response = sweep_bot.chat(prompt, functions=functions)
        logger.info(response)

        while sweep_bot.messages[-1].function_call is not None:
            obj = sweep_bot.messages[-1].function_call
            name = obj["name"]
            arguments = json.loads(obj["arguments"])
            if name == "get_relevant_snippets":
                logger.info("Searching for relevant snippets...")
                search_message = client.chat_postMessage(
                    channel=request.channel_id,
                    text=f"Searching \"{arguments['query']}\" in the codebase...",
                    thread_ts=thread["ts"],
                )
                # additional_snippets = sweep_bot.search_snippets(
                #     arguments["query"],
                #     installation_id=installation_id
                # )
                additional_snippets = default_snippets
                additional_snippets_message = f"Found {len(additional_snippets)} additional snippets:\n\n" +  "\n".join(
                    f"{snippet.get_slack_link(repo_name)}\n```{snippet.get_preview()}```" for snippet in additional_snippets
                )
                client.chat_update(
                    channel=request.channel_id,
                    text=additional_snippets_message,
                    ts=search_message["ts"],
                )
                response = sweep_bot.chat(additional_snippets_message, functions=functions)
            elif name == "create_pr":
                title = arguments["title"]
                summary = arguments["summary"]
                branch = arguments["branch"]
                plan = arguments["plan"]
                print(plan)
                plan_message = "\n".join(f"`{file['file_path']}`: {file['instructions']}" for file in plan)

                creating_pr_message = client.chat_postMessage(
                    channel=request.channel_id,
                    text=pr_format.format(
                        title=title,
                        summary=summary,
                        plan=plan_message
                    ),
                    thread_ts=thread["ts"],
                )
                file_change_requests = [
                    FileChangeRequest(
                        filename=file["file_path"],
                        instructions=file["instructions"],
                        change_type="create"
                    ) for file in plan
                ]
                pull_request = PullRequest(
                    title=title,
                    branch_name=branch,
                    content=summary,
                )
                results = create_pr.call(
                    file_change_requests=file_change_requests,
                    pull_request=pull_request,
                    sweep_bot=sweep_bot,
                    username=request.user_name,
                    installation_id=installation_id,
                )
                pr = results["pull_request"]
                client.chat_update(
                    channel=request.channel_id,
                    text=f"Created PR at {pr.html_url}.",
                    ts=creating_pr_message["ts"],
                )
                break
            else:
                raise Exception(f"Unknown function {name}")
        else:
            # no breaks were called
            client.chat_postMessage(
                channel=request.channel_id,
                text=response,
                thread_ts=thread["ts"],
            )
    except Exception as e:
        client.chat_postMessage(
            channel=request.channel_id,
            text="Sorry, something went wrong.",
            thread_ts=thread["ts"],
        )
        raise e


@stub.function(image=image)
@modal.web_endpoint(method="POST")
async def entrypoint(request: Request):
    body = await request.form()
    request = SlackSlashCommandRequest(**dict(body))
    reply_slack.spawn(request)
    return Response(status_code=200)
