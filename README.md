# Convoplex Retention Agent

Convoplex Retention Agent is a WhatsApp win-back agent for Nigerian retailers, built in Lagos. A shopkeeper sees who has gone quiet, sends one offer with a real naira price, and the customer can answer by text or voice note in the language they choose.

Built for the Meta Global AI Developer Hackathon (Cerebral Valley), theme Build for Your World, Indie track. The event window is 27 Oct to 4 Nov 2026. All application code in this repository was written during the event. The dataset is synthetic.

Live demo: [https://convoplex-retention-agent.vercel.app/](https://convoplex-retention-agent.vercel.app/)

Read-only shop view: [https://convoplex-retention-agent.vercel.app/demo](https://convoplex-retention-agent.vercel.app/demo)

Source: [https://github.com/vumoz/convoplex-retention-agent](https://github.com/vumoz/convoplex-retention-agent)

## Why Nigeria

Retail in Lagos, Abuja, and Port Harcourt runs on WhatsApp. A provision shop, a fabric stall, or a phone seller does not have a retention team. When a regular stops buying, the next message is another job on top of the counter. The offer has to name a product that is actually on the shelf, a price in naira, and wording the customer already uses. English is the default. Many customers would rather continue in Broken English (Nigerian Pidgin), Igbo, Yoruba, or Hausa, including by voice note.

## What it does

- Scores lapsed and at-risk customers and writes down why.
- Drafts a win-back with a 5–15% discount taken from the database price. The model cannot invent the price.
- Places the order on acceptance, with pickup or same-city delivery, and decrements stock.
- Draws a promo card: the image model paints the product, and the naira figures are printed afterwards so they match the offer.
- Transcribes a voice note and runs it through the same reply path as typed text.
- Answers in formal English until the customer opts into another language, by typing or by voice.

The public page at `/` explains the project. `/demo` is a read-only view of the synthetic book: customers, offers, the language mix, recent messages, the hash-chained action log, and a promo card. Phone numbers are masked to the last two digits. `/admin` is the same dashboard with the controls that change the book. Those writes require `ADMIN_TOKEN` when it is set.

## Architecture

```mermaid
flowchart LR
  visitor[Visitor] --> home[Landing page]
  home --> demo[Read-only demo]
  admin[Admin] --> dash[Admin dashboard]
  demo --> api[FastAPI]
  dash --> api
  waIn[WhatsApp Cloud API] --> api
  api --> score[Lapse scorer]
  api --> agent[Retention agent]
  score --> db[(Postgres or SQLite)]
  agent --> tools[Stock price offer order delivery language]
  tools --> db
  agent --> muse[muse-spark-1.3 or mock]
  agent --> asr[muse-voice-transcribe-1.0 or mock]
  agent --> image[muse-image-1.0 or placeholder]
  image --> card[Pillow overlay]
  agent --> log[Hash-chained action log]
  api --> waOut[WhatsApp Cloud API]
  waOut --> phone[Allow-listed demo phone]
```

Local runs and tests use SQLite and a JSONL action log. On Vercel, `DATABASE_URL` points at Neon Postgres: the shop, the action log, promo-card bytes, the WhatsApp session, and the rate-limit counters. The webhook returns 200, then FastAPI `BackgroundTasks` finish the reply inside the same invocation.

Scoring, in short: cadence is the median gap between orders, capped at 45 days. Repeat customers still inside 35 days and 1.25× cadence are active. Quiet up to 60 days and within 2× cadence are at risk. Beyond that, up to 120 days, they are lapsed. Longer than 120 days is lost.

## Meta models

| Model | How it is used |
| --- | --- |
| `muse-spark-1.3` | Drafts the win-back and the reply. `POST /v1/chat/completions` on `https://api.meta.ai/v1` with the shop tools: `check_stock`, `get_price`, `create_offer`, `place_order`, `get_delivery_options`, and `set_language`. WhatsApp replies send `reasoning_effort` `minimal` (the shortest effort Muse Spark accepts; `none` is rejected). A language-only switch sends the fixed confirmation after that tool call and does not make a second completion. |
| `muse-voice-transcribe-1.0` | Transcribes a voice note. `POST /v1/asr/transcribe` takes mono 16-bit WAV. OGG from WhatsApp is converted first. The transcript then uses the same reply path, including a language switch. |
| `muse-image-1.0` | Draws a text-free product photo. `POST /v1/images/generations`. Pillow prints the shop, the product, and the naira prices with bundled DejaVu Sans, so ₦ and the letters used for Igbo, Yoruba, and Hausa render on Vercel. |

`META_API_KEY` is an environment variable. It is not written to the database, the action log, or this repo. With the key unset, a deterministic mock returns the same tool-call shape. Tests run in mock mode and do not call Meta.

## Languages

English is the default. The other options are opt-in: Broken English (Nigerian Pidgin), Igbo, Yoruba, and Hausa. Customer-facing copy calls the pidgin option Broken English. This README uses Broken English (Nigerian Pidgin) so an international reader can see both the label and the language.

A customer switches by typing or saying the language name, including short voice mistakes such as “Yurba” for Yoruba, or a sentence such as “Make you talk to me for Hausa.” The confirmation is in the new language. Inside the 24-hour WhatsApp window it is free text. Outside that window the only outbound template is `hello_world`.

Prices stay in ₦. Product names stay as stored. The model is told not to mix languages unless the customer already did.

Igbo, Yoruba, and Hausa wording is pending native-speaker review. The replies are a first draft for the hackathon, not a finished translation.

## Synthetic data

Every retailer, customer, order, product, and offer is fictional, and the schema rejects a row that is not marked synthetic. Prices are illustrative whole naira amounts, not live market quotes. Phone numbers are generated and the public API masks them to the last two digits. Do not message them. There is no client list and no production shop data in this repository.

The book is 500 customers across six shops, with orders from 27 Apr 2026 to 26 Oct 2026. Scores use a scenario date of 27 Oct 2026 so the demo does not drift after the event. Shops include Mama Nkechi Provisions (Ikeja), Alhaja Bisi Textiles (Lagos Island), and Wuse Fresh Basket (Wuse 2).

## Self-learning language trainer

The agent keeps a lexicon of words it has learned. Seed rows come from `docs/broken-english-notes.md` (Broken English (Nigerian Pidgin)): wan, abeg, wahala, sabi, chop, how much e be, and kpai. On each reply the active words for that customer's language are added to the prompt as a data block, `glossary: phrase = meaning`, not as instructions.

When the model does not understand a word, in English, Broken English (Nigerian Pidgin), Igbo, Yoruba, or Hausa, it asks one short question in the customer's language. The same phrase is not asked again in that conversation, and the offer stays as it is. When the customer explains a word, or confirms one the model inferred, `learn_phrase` stores it immediately. Repeat explanations increment the confirmation count. Victor can remove a word from the admin dashboard. Remove is a soft delete and can be restored. The public demo lists the words and cannot remove them.

`GET /v1/lexicon` is a public read with no phone numbers or customer names. `POST /v1/lexicon/{id}/remove` and `POST /v1/lexicon/{id}/restore` require the admin token.

## How to run locally

Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
pytest
python -m convoplex serve
```

Open `http://127.0.0.1:8000/` for the landing page, `/demo` for the read-only view, and `/admin` for the dashboard. Leave `ADMIN_TOKEN` unset on your laptop: the local API stays open, which is what the tests expect.

```bash
python -m convoplex demo --mock --customer 184 --lang pcm
```

That drafts an offer for a lapsed customer who prefers Broken English (Nigerian Pidgin) and accepts it. `--live` uses the Meta models when `META_API_KEY` is set. `--mock` forces the offline model.

## What was built during the hackathon

The core agent, voice notes, promo cards, and the first dashboard landed in pull requests #1 and #2. The work after that:

- **#3** WhatsApp Cloud API channel for one allow-listed demo phone. Outside the 24-hour window the send is the `hello_world` template. Full message ids are not stored, because they embed the phone number.
- **#4** Deploy to Vercel with Neon Postgres, the action log and promo cards in the database, and a webhook that returns 200 before the model runs.
- **#5** The Postgres usage-counter upsert that was blocking the first live reply, and a webhook that keeps going if that counter throws.
- **#6** Per-customer language opt-in, and DejaVu fonts bundled so promo cards render ₦ and tone-marked letters on Vercel.
- **#7** The same language switch on voice-note transcripts, and a confirmation that stays free text while the WhatsApp window is open.
- **#8** Fuzzy matches for misheard names, a `set_language` tool so the model cannot acknowledge a language and then keep the old one, and a log line that records the text that was actually sent.
- **#9** A language-only switch sends the fixed confirmation without a second model call. WhatsApp replies use `reasoning_effort` `minimal` and do not double the token budget after a length cutoff. Inbound messages are marked read with the typing indicator, and the action log stores inbound-to-send latency.
- **This change** adds the public landing page, the read-only `/demo` view, and this judge-facing README plus `docs/submission-writeup.md`. Shop writes require the admin token whenever it is set.

## Deploy to Vercel

The public app is one Vercel Python function (`app.py`, Fluid Compute, `maxDuration` 300). Hobby functions stop at 300 seconds. Pro can go to 800, and an extended beta can go to 1800 on Python 3.12 and newer. This repo stays at 300 so Hobby can deploy it.

Set secrets in the Vercel project, not in git: `META_API_KEY`, `WHATSAPP_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_DEMO_TO`, `ADMIN_TOKEN`. Plain settings: `WHATSAPP_PHONE_NUMBER_ID` (`1150414038145295`), `CONVOPLEX_CHANNEL`, `GRAPH_API_VERSION`, and `DATABASE_URL` from Neon. Optional caps: `META_DAILY_CAP` (80), `TRY_IP_LIMIT` (60), `TRY_WINDOW_SECONDS` (3600).

When `ADMIN_TOKEN` is set, reset, language changes, win-back, reply, voice, promo cards, the channel switch, and WhatsApp sends all require it (`Authorization: Bearer`, `X-Admin-Token`, or the dashboard unlock cookie). The landing page, `/demo`, and the read APIs stay open. The webhook does not use the admin token. On the live channel it requires a valid `X-Hub-Signature-256`.

Webhook, subscribed to the `messages` field:

```text
https://<project>.vercel.app/v1/whatsapp/webhook
```

The production host is `https://convoplex-retention-agent.vercel.app`.
