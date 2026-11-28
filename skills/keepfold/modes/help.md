# Help (`/keepfold help`)
No session: do not call `keepfold_start`. `/keepfold help <word>` (for example `help study`, `help terminal`) shows only that section.

1. **Greet them.** One warm line with their name (from prime or the brief) and the time of day, for example "Good evening, Sam!" Then: "I'm Keepfold, your second brain. I remember what you study, what you get wrong and who you are, and I plan what to revise next."
2. **Show the reference below**, as written. It is the full list; don't drop rows.
3. **Close** by asking what they would like to do, and suggest three things to try: `/keepfold learn`, `/keepfold sweep`, and `/keepfold short` if it is late.

---

## Study
| Command | What it does |
|---|---|
| `/keepfold` | Home: a short map of what's due, what's coming and what's waiting, then it asks what you want |
| `/keepfold learn` | Today's plan: ask first, teach the gap, re-test. Add a topic to focus: `/keepfold learn dbms` |
| `/keepfold review` | Only what is due for a refresh |
| `/keepfold short` | Tired? At most 5 small steps, your confident misses first |
| `/keepfold sweep` | 10 mixed cold questions with "how sure? 1–4"; explanations at the end |
| `/keepfold mock` | A mock interview (coding, aptitude, HR, project rounds) with a debrief |
| `/keepfold mock bank` | Drafts STAR stories from your projects; saves one only after you approve it |
| `/keepfold log` | Records a result you report ("4/5 on the TCS aptitude test") |
| `/keepfold week` | This week's numbers |

## Second brain
| Command | What it does |
|---|---|
| `/keepfold about <topic>` (or `/keepfold <topic>`) | What you know and have about a topic |
| "what do I know about X?" | Search your whole vault by meaning; no command needed |
| `/keepfold capture` | Saves a source (article, PDF, video) and the ideas worth keeping |
| `/keepfold project <name>` | One project: status, decisions, next steps |
| `/keepfold reflect` | Weekly review: confirm or drop what's waiting about you, idea clusters, spot-checks of how a chat graded you |
| `/keepfold map` | Opens the brain and lists in the Keepfold app; Obsidian's graph is Ctrl+G |

## Chats
| Command | What it does |
|---|---|
| `/keepfold save` | Saves what's worth keeping from this chat; you approve the list first |
| `/keepfold history` | Saves older Claude Code chats, a few at a time |
| `/keepfold harvest` | Distils your imported Gemini, Perplexity and Claude chats, about 10 minutes a night. It reads only your own messages and writes "What I explored" lines on topic pages. |

## About you
| Command | What it does |
|---|---|
| "save this about me" | Saves a preference or personality line, after your yes |
| `/keepfold help` | This list |

## Without any command
In every chat Keepfold already knows you from your first message, saves what's durable (facts, sources, decisions, quiz answers) and tells you when it wrote something. Each chat's changes can be undone in one step.

## Terminal (`keepfold …` in any terminal; from a source checkout, prefix `uv run`)
| Command | What it does |
|---|---|
| `today` / `brief` | Today's plan / brief, teaching contract and Today |
| `dashboard` | Opens the Keepfold app's home (starts the app if needed) |
| `recall "query"` | Searches the vault by meaning and keywords |
| `log assessment\|result\|solved\|taught` | Records results by hand |
| `reflect` | The weekly review pack |
| `claims` (`--summary`, `--unasked`) | Lines about you: all, how well Keepfold knows you per page, or saved without asking |
| `chats` / `import-chats` | Past chats / import a claude.ai export |
| `digest` | Review chats imported since the last digest; each topic you confirm is a touch, never a grade |
| `harvest status` | Harvest progress and undo commands |
| `safety-net` (`--dry-run`, `--undo`) | Record the topics and concepts your recent chats covered that the chat forgot to log |
| `fact-harvest` | Save facts you stated about yourself in recent chats as guesses about you (needs a local model already loaded) |
| `export context` | A paste-ready "about me" file for ChatGPT, Gemini or Cursor |
| `pack general\|coding\|writing\|interview\|research\|review` | A context pack (≤ 1,500 characters, or 5,000 with `--chars 5000`) to paste into a web chat |
| `undo <session>` / `void` | Revert one chat's changes / cancel one record |
| `backup` | Zip the whole vault |
| `restore` | Unpack a backup zip into a new or empty folder, never over a vault |
| `doctor` / `lint` / `relink` | Health check / vault problems / fix Obsidian links |
| `migrate` | Move what Cortex (0.6.1 and older) left: settings, vault folder, AI-app connections |
| `agent-text` | Every fixed text Keepfold shows an AI app, with its size per app |
| `rollups` (`--since YYYY-MM`) | Write the week, month and year summary pages (code only, no model) |
| `eval` (`all`, `learning`, `recall`) | How well search and the study forecasts are doing |
| `topics` (`status`, `attach`, `build`, `eval`, `split`) | The topic tree |
| `concept` (`add`, `alias`, `merge`, `duplicates`) | Edit the concept list |
| `catalog sync` / `replay` | Keep the catalog in step / recompute all states |
| `model` (`download`, `build`, `status`) | The local search model |
| `browse` (`scan`, `import`, `status`, `pair`, `serve`) / `llm status` | Your browsing as day pages, filtered on this laptop / the optional local LLM |
| `exclude add\|list\|remove` | Folders where Keepfold stays off (e.g. a work repo) |
| `watch add\|list\|remove` | Folders (or Zotero) the app imports by itself: their new and changed files, every 10 minutes |
| `voice import` / `forget` / `kill` / `access` | Your writing samples / delete a person's page / emergency stop / what was read as you |
| `session close\|sweep` | Close leftover sessions |
| `setup` / `init` / `sample remove` / `mcp` / `uninstall` | Install, create a vault, remove the sample, run the server, remove the app (the vault stays) |

`keepfold --help` prints every command; `docs/GUIDE.md` explains each in detail.
