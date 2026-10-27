# Invoice, Ticket & Sales Bot (Discord Components V2)

One bot with two halves that share the same flat Components V2 design
(`## Title`, divider, `**Label:** value` fields, `-#` footer, buttons at the bottom):

* **Statements ledger** (`data.json`): quick invoices, **Sales Statements** and **Expense Statements**, each showing the running total.
* **Desk** (`data/bot.db`, folder `desk/`): itemised PDF invoices, tickets, sales pipeline, income/expense books, dashboard and P&L.

They are kept in step automatically (see "How they connect").

## Setup (no .env file)

1. Open `config.py` and fill the **EDIT THESE** block: `BOT_TOKEN`, `GUILD_ID`, `INVOICE_CHANNEL_ID`,
   `STATEMENTS_CHANNEL_ID`, `STAFF_ROLE_IDS`, and the ticket / business settings you need.
   (Panel variables with the same names override the file.)
   Then fill in the three **company** channels described below — money commands refuse to save until
   the chosen company has a statement channel.
2. Pterodactyl: **App py file** = `bot.py` (or `app.py`), **Requirements file** = `requirements.txt`.
   If requirements were not installed, the bot installs any missing package itself on start.
3. Optional but recommended: Developer Portal → Bot → enable **Message Content Intent** (ticket transcripts include text).
   Without it the bot still starts; transcripts just have no message text.
4. Invite with scopes `bot` + `applications.commands`.

Staff commands need **Administrator** or a role listed in `STAFF_ROLE_IDS`.

## Channels

| Setting | Receives |
|---|---|
| `COMPANY_ZARBUNEX_STATEMENT_CHANNEL_ID` | Sales / Expense / Revenue Statements for **Zarbunex PVT LTD** |
| `COMPANY_HECTORHOST_STATEMENT_CHANNEL_ID` | Sales / Expense / Revenue Statements for **HectorHost** |
| `COMPANY_MATRIXMC_STATEMENT_CHANNEL_ID` | Sales / Expense / Revenue Statements for **MatrixMC** |
| `COMPANY_*_INVOICE_CHANNEL_ID` | optional: that company's invoice cards (defaults to its statement channel) |
| `INVOICE_CHANNEL_ID` | only invoices that predate companies (legacy rows) |
| `STATEMENTS_CHANNEL_ID` | only legacy statements (records with no company) |
| `TICKET_LOG_CHANNEL_ID` | transcripts, close/reopen logs, stale-ticket digest |
| `SALES_CHANNEL_ID` | follow-up reminders |
| `TRANSACTION_CHANNEL_IDS` | optional extra copies of invoice / payment cards |

## Commands

**Statements** (all company-scoped: pick `company:` every time)

| Command | What it does |
|---|---|
| `/sale add` | Record a sale → posts a **Sales Statement** to that company's channel (Company, Date, Client Name/Contact, Server Name/UUID, Service/Plan, Amount Received, Payment Method, Transaction ID, Remarks, **Total Sales** for that company) |
| `/expense add` | Record an expense → posts an **Expense Statement** to that company's channel (Company, Date, Description, Category, Amount, Payment Method, Paid To, Transaction ID, Remarks, **Total Expenses** for that company) |
| `/revenue` | Record revenue (`Commission`, `Investment`, `General`) → posts a **Revenue Statement** (Transaction ID `REV-0001`, Company, Category, Amount, Description, Date, Recorded By) to that company's channel |
| `/statement sales`, `/statement expenses` | Per-company period report (today / 7 days / 30 days / all time) with the period total |

**Invoices** (`/invoice ...`)

| Command | What it does |
|---|---|
| `create` | Quick invoice form for a **required company** (client, contact, amount, service/plan, due). Its **Mark as paid** button asks for method / transaction ID / server / UUID / remarks, then posts a Sales Statement for the invoice's company |
| `create-detailed` | Itemised PDF invoice for a **required company** (items form also shows the company, preselected from the ticket when it has one) |
| `list [status] [month]` | Quick and itemised invoices together |
| `view`, `pdf`, `pay`, `void`, `send-ticket`, `deliver` | Itemised invoice tools (payments ask for method + transaction ID) |

Itemised item line: `name | quantity | unit price | type=VPS; server=Hector-VPS-12; node=Ryzen; plan=4GB; id=HX-1122; link=https://...`

**Tickets** (`/ticket ...`): `open`, `panel`, `info`, `list`, `claim`, `assign`, `note`, `priority`, `company`, `close`, `reopen`, `transcript`, `dashboard`.
Panels have buttons: Claim, Create Invoice, Linked Ticket, Add Note, Transcript, Sales, Close/Reopen.
`/ticket company` files a ticket under a company; invoices raised from that ticket preselect it, and
moving the ticket between Discord categories never changes it.

**Sales pipeline** (`/sales ...`): `pipeline`, `list`, `deal`, `ticket`, `add`, `stage`, `followup`, `note`, `won`, `lost`, `due`.
Purchase / enquiry tickets create deals automatically; invoices move them to Quoted, full payment marks them Won.

**Announcements**

| Command | What it does |
|---|---|
| `/embed` | Post a card (`## title`, message body, `-# footer`) in the chosen channel. `channel`, `title` and `footer` are command options; the message itself is typed in a form, where title and footer can still be edited. Staff only, never pings anyone |

**Giveaways** (`/giveaway ...`)

| Command | What it does |
|---|---|
| `create` | Post a giveaway card in a channel: prize, winners, duration in minutes, optional required role. The card shows a live entry count and a Discord countdown; the **Enter giveaway** button adds one entry per member |
| `end` | Draw the winners immediately instead of waiting |
| `reroll` | Pick a replacement winner after the giveaway ended (never repeats a previous winner) |

Giveaways, entries and winners live in `data/bot.db`, so entries and running giveaways survive a restart;
the draw happens automatically within ~30 seconds of the end time and pings only the winners.

**Finance** (`/finance ...`): `income`, `expense`, `category-add`, `transactions`, `void`, `audit`, `dashboard`, `pnl`.
`income`/`expense` require a company; `transactions`, `dashboard` and `pnl` accept an optional company
(omitted = all companies). The dashboard and P&L list Commission / Investment / General separately;
Investment is never counted as operating revenue or profit.

## Companies (multi-company finance)

Three companies are configured in `config.py` → `COMPANY_CONFIG` (keys `zarbunex`, `hectorhost`,
`matrixmc`). Every sale, expense, revenue entry, invoice, payment transaction and ticket stores the
company key, and each company's statements go to its own channel only — which may be in a different
server.

1. Create one statements channel per company (in whichever server you want) and, optionally, a
   separate invoices channel per company.
2. Invite the bot to **each** server with `bot` + `applications.commands` and the permissions from
   `bot.py` (`INVITE_PERMISSIONS`), and give it **View Channel** + **Send Messages** in those channels.
3. Set the ids as panel / environment variables (or edit `COMPANY_CONFIG`):

```
COMPANY_ZARBUNEX_STATEMENT_CHANNEL_ID=...
COMPANY_HECTORHOST_STATEMENT_CHANNEL_ID=...
COMPANY_MATRIXMC_STATEMENT_CHANNEL_ID=...
COMPANY_ZARBUNEX_INVOICE_CHANNEL_ID=...      # optional
COMPANY_HECTORHOST_INVOICE_CHANNEL_ID=...    # optional
COMPANY_MATRIXMC_INVOICE_CHANNEL_ID=...      # optional
COMPANY_ZARBUNEX_NAME=...                    # optional display-name override
```

A company with no channel is simply unavailable: commands reply with the exact variable to set, and
nothing is saved. There is deliberately **no default company** — every financial command asks for one.

Existing `data/bot.db` files are upgraded in place on start (new `company_id` columns and a `revenue`
table); old rows keep their values with `company_id` empty and still show under "all companies" reports.

## How they connect

* A payment on an itemised invoice → Sales Statement in the statements channel + ledger entry.
* A new itemised invoice → card in the invoice channel.
* `/sale add`, `/expense add` and quick-invoice payments → also recorded in the books, so `/finance dashboard` and the P&L include them.
* `/finance income` and `/finance expense` → also posted as Sales / Expense Statements.

So **Total Sales** and **Total Expenses** on statements cover everything.

## Files

```
bot.py  app.py  config.py  data.py  ui.py  requirements.txt
cogs/finance.py     /sale /expense /revenue /statement
cogs/invoices.py    quick invoice form
desk/               database, invoices, finance, tickets, sales, views, bridge, pdf_gen, utils
logo.png  NotoSans-*.ttf   invoice PDF branding (replace logo.png with yours)
tests/              python -m unittest discover -s tests -t .
```

Back up `data.json` and the `data/` folder (database + transcripts); never wipe them on reinstall.
The migration only adds columns/tables, so restoring an old `data/bot.db` into the new version is safe.
