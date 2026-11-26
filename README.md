# Commit Issues V0

Five emotions hold a public argument about a pull request. Joy books a parade, Anger represents the brackets, Sadness brings flowers, Fear laminates a disaster plan, and Disgust can taste the staples. The seventh comment cuts the argument off mid-feud.

## Try it offline

Use Python 3.10 or newer. Run from the extracted project folder. Install the pinned official Groq SDK before running the project.

```sh
python -m pip install -r requirements.txt
python -m commit_issues preview --fixture --html .commit-issues/preview.html
python -m unittest discover -s tests -v
```

The fixture follows a leftovers function that replaces `return None` with `return []`, plus a new test assertion. The HTML file is self-contained. Every card can reveal the exact GitHub Markdown. Preview output and the SQLite file are local to the machine running the command. Keep any private-repository exports out of shared folders and source control.

## What runs each turn

A fixed order gives all five emotions a turn and brings Joy and Anger back for two replies. In model mode, each speaker gets its own Markdown prompt, one observed fact, up to two preceding turns, and at most one stored grievance from a previous PR in the same repo. The application adds the factual evidence line to every comment. The paid provider receives neither raw patches, filenames, GitHub usernames, nor human-authored PR comments. Model replies are treated as plain text and reviewed in the private preview for tone and factual claims before posting.

| Mode | Input per million tokens | Output per million tokens | Generation |
| --- | ---: | ---: | --- |
| `mock` | $0 | $0 | Scripted dialogue, default |
| `claude-haiku-5-5` | $0.10 | $0.50 | Direct Anthropic Messages API at low effort |
| `gpt-6-luna` | $0.10 | $0.50 | OpenAI Responses API with reasoning effort `none` |

Rates are standard short-context rates checked on November 26, 2026. At an illustrative 500 input and 192 output tokens per turn, seven requests cost about $0.0010 at either rate. Provider token accounting and infrastructure charges can differ. The limits are 2,200 UTF-8 prompt bytes per turn, 192 output tokens per Anthropic or OpenAI request, seven requests per draft, and 21 reserved requests across the lifetime of the SQLite database. A reservation counts against the local budget even if the process or provider fails. The episode falls back to the script if a request fails or a response fails the output check. [Anthropic model and rates](https://platform.claude.com/docs/en/models/haiku-5-5/overview), [OpenAI model and rates](https://developers.openai.com/api/docs/models/gpt-6-luna).

The hosted model can be tried on the fixture first. After explicit authorization and loading the provider key into the environment, use `preview --fixture --model claude-haiku-5-5 --approve-paid-model`, or substitute `gpt-6-luna`. The CLI reads `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` respectively. Hosted timing, writing quality, and account access require an approved API run to verify.

## Groq inference

Set the following environment variables after approval. Load the key privately into the process environment. An explicit `--model` takes precedence, and the default provider is `mock`.

```ini
AI_PROVIDER=groq
GROQ_API_KEY=
GROQ_MODEL=qwen/qwen3.8-27b
```

| `GROQ_MODEL` | Effort | Temperature | Completion cap per request | Input / output per million tokens |
| --- | --- | ---: | ---: | ---: |
| `qwen/qwen3.8-27b` (default, Groq Preview) | `none` | 0.9 | 512 | $0.80 / $4.00 |
| `openai/gpt-oss-20b` | `low` | 0.7 | 1,024 | $0.075 / $0.30 |

Groq receives the same prompt and bounded dialogue history using the official Python SDK, with an 18-second request timeout and SDK retries disabled. The same 280-character line validator applies. GPT-OSS requests suppress returned reasoning, and only final assistant content is passed to the validator. A draft can retry one HTTP 429 after Groq's `Retry-After`, up to five seconds. The retry consumes one of the seven reserved requests, so the last turn may use the script. Timeouts, invalid JSON, and 429s without a usable retry window switch the remaining turns to the local script. [Groq Python SDK](https://github.com/groq/groq-python), [reasoning options](https://console.groq.com/docs/reasoning), [rate limits](https://console.groq.com/docs/rate-limits), [model prices](https://console.groq.com/docs/models).

### One-request smoke test per model

After explicit authorization and loading `GROQ_API_KEY`, run these from the project folder. Both use Joy's opening on the same built-in PR fixture. The other six turns use the local script, and each model can make only one request, even on HTTP 429. For each successful smoke test, check that Joy's line is marked `[model]`. `[fallback]` on that turn means the request or response did not pass. The fixture drafts are local and cannot be published. Each run still reserves the existing seven-call allowance in SQLite.

```sh
python -m commit_issues preview --fixture --groq-smoke --model qwen/qwen3.8-27b --approve-paid-model
python -m commit_issues preview --fixture --groq-smoke --model openai/gpt-oss-20b --approve-paid-model
```

Live compatibility and token usage remain unmeasured. For planning, at about 500 input tokens per request, a seven-attempt draft would use about 3,500 input tokens. Completion ceilings are 3,584 tokens for Qwen or 7,168 for GPT-OSS across those attempts. At those ceilings and the listed rates, the estimated costs are about $0.017 and $0.0024 respectively. Each one-request smoke uses one turn's input and completion allowance. GPT-OSS completion usage can include reasoning tokens even when reasoning text is suppressed.

## Sprite handoff

These steps require explicit approval for the Sprite, connector authorization, and any associated cost. Use the [Sprites quickstart](https://docs.fly.io/sprites/quickstart) to install and authenticate the CLI. Run the following in Bash from the folder containing the source ZIP. Keep the Sprite private and retain one SQLite database on it for the commenting run.

```sh
sprite create commit-issues-demo
sprite file push -s commit-issues-demo ./Commit_Issues_V0.zip /tmp/Commit_Issues_V0.zip
sprite exec -s commit-issues-demo -- python3 -m zipfile -e /tmp/Commit_Issues_V0.zip /home/sprite
sprite exec -s commit-issues-demo --dir /home/sprite/Commit_Issues -- python3 -m venv .venv
sprite exec -s commit-issues-demo --dir /home/sprite/Commit_Issues -- .venv/bin/python -m pip install -r requirements.txt
sprite exec -s commit-issues-demo --dir /home/sprite/Commit_Issues -- .venv/bin/python -m unittest discover -s tests -v
sprite console -s commit-issues-demo
```

The `exec`, `console`, and file transfer commands are documented in the [Sprites CLI reference](https://docs.fly.io/sprites/cli/commands). From the project directory on the Sprite, activate the environment with `source .venv/bin/activate`, then run `python3 -m commit_issues preview --fixture`.

In the Fly.io dashboard, add the first-party GitHub OAuth Connector. Review the scopes shown by GitHub. Use a dedicated GitHub identity with access to the demo repository and restrict the connector to the demo Sprite. If available, set Connector route rules to the chosen repository's `pulls` and `issues/.../comments` paths used below. The timeline will show the authorized GitHub identity as the commenter and a different character name in each message. [Fly's connector setup and GitHub example](https://fly.io/sprites/ecosystem/#github), [Fly's gateway and access policies](https://fly.io/connectors/).

From the Sprite console, set the repository and use the GitHub Connector base listed under **API Usage** in the dashboard if it includes a connection ID. The default is Fly's documented `https://api.sprites.dev/v1/gateway/github`. Ensure the repository and PR were designated for the demo. Use a human-authored, open, ready-for-review PR from a branch in the same repo and apply the `commit-issues-demo` label. In this example the PR number is `42`.

```sh
cd /home/sprite/Commit_Issues
source .venv/bin/activate
export COMMIT_ISSUES_REPO=owner/demo-repo
python3 -m commit_issues preview --pr 42 --html .commit-issues/demo-42.html
```

The preview performs only Connector GETs and uses the script by default. View the private transcript in the console or pull the HTML file with `sprite file pull`. If the label, commit, or target changes, prepare a fresh preview. To replay a saved draft locally, run `python3 -m commit_issues show DRAFT_FINGERPRINT`.

After the operator explicitly approves the exact transcript, use `python3 -m commit_issues publish --approve-comments DRAFT_FINGERPRINT`. It verifies the bound repository, PR, both commit SHAs, and opt-in label before writing. The only mutation is `POST /repos/{owner}/{repo}/issues/{number}/comments` via the official Connector. It produces up to seven timeline comments in order, spaced by at least 1.2 seconds during a single run. There are no webhook or comment triggers, so even an OAuth account whose posts appear as a normal user cannot trigger another episode. Merge decisions, review states, and branch protections are untouched by the workflow. A draft created from the fixture cannot be published. [Create an issue comment on a pull request](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment).

### Write verification and recovery

Fly documents authenticated GitHub REST forwarding and write-capable integrations. GitHub documents a `201 Created` response for the timeline comment route. The ability of a particular OAuth connection to send this POST still depends on the account's permissions and Connector policy. The approved demo run is the live verification. Keep the receipt links printed by the command and inspect the seven comments in the PR conversation. [Fly GitHub Connector](https://fly.io/connectors/), [Fly's PR workflow](https://fly.io/learn/background-coding-agents-pr-workflow/), [GitHub response contract](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment).

Use the same database and approval fingerprint after an interruption. SQLite records a turn as in flight before its sole POST attempt. A later run can confirm an attempted turn from an exact matching GitHub comment and continue. An attempted turn that cannot be confirmed stops the batch for manual inspection. Comments from a different posting ledger, a changed revision, and a projected timeline above 99 comments also stop publication. Keep the demo to one Sprite and database so competing publishers share the same posting ledger.

Hacksgiving requires an agent running on a Sprite with at least one Connector. That milestone is reached when the Sprite runs the opted-in PR preview through GitHub. The approved live comment run supplies the PR timeline demo. [Hacksgiving 2026 rules](https://fly.io/hacksgiving/).
