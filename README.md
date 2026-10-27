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
2. Pterodactyl: **App py file** = `bot.py` (or `app.py`), **Requirements file** = `requirements.txt`.
   If requirements were not installed, the bot installs any missing package itself on start.
3. Optional but recommended: Developer Portal → Bot → enable **Message Content Intent** (ticket transcripts include text).
   Without it the bot still starts; transcripts just have no message text.
4. Invite with scopes `bot` + `applications.commands`.

Staff commands need **Administrator** or a role listed in `STAFF_ROLE_IDS`.

## Channels

| Setting | Receives |
|---|---|
| `INVOICE_CHANNEL_ID` | every new invoice (quick or itemised) |
| `STATEMENTS_CHANNEL_ID` | every Sales Statement and Expense Statement |
| `TICKET_LOG_CHANNEL_ID` | transcripts, close/reopen logs, stale-ticket digest |
| `SALES_CHANNEL_ID` | follow-up reminders |
| `TRANSACTION_CHANNEL_IDS` | optional extra copies of invoice / payment cards |

## Commands

**Statements**

| Command | What it does |
|---|---|
| `/sale add` | Record a sale → posts a **Sales Statement** (Date, Client Name/Contact, Server Name/UUID, Service/Plan, Amount Received, Payment Method, Transaction ID, Remarks, **Total Sales**) |
| `/expense add` | Record an expense → posts an **Expense Statement** (Date, Description, Category, Amount, Payment Method, Paid To, Transaction ID, Remarks, **Total Expenses**) |
| `/statement sales`, `/statement expenses` | Period report (today / 7 days / 30 days / all time) with the period total |

**Invoices** (`/invoice ...`)

| Command | What it does |
|---|---|
| `create` | Quick invoice form (client, contact, amount, service/plan, due). Its **Mark as paid** button asks for method / transaction ID / server / UUID / remarks, then posts a Sales Statement |
| `create-detailed` | Itemised PDF invoice (items form; see item format below) |
| `list [status] [month]` | Quick and itemised invoices together |
| `view`, `pdf`, `pay`, `void`, `send-ticket`, `deliver` | Itemised invoice tools (payments ask for method + transaction ID) |

Itemised item line: `name | quantity | unit price | type=VPS; server=Hector-VPS-12; node=Ryzen; plan=4GB; id=HX-1122; link=https://...`

**Tickets** (`/ticket ...`): `open`, `panel`, `info`, `list`, `claim`, `assign`, `note`, `priority`, `close`, `reopen`, `transcript`, `dashboard`.
Panels have buttons: Claim, Create Invoice, Linked Ticket, Add Note, Transcript, Sales, Close/Reopen.

**Sales pipeline** (`/sales ...`): `pipeline`, `list`, `deal`, `ticket`, `add`, `stage`, `followup`, `note`, `won`, `lost`, `due`.
Purchase / enquiry tickets create deals automatically; invoices move them to Quoted, full payment marks them Won.

**Finance** (`/finance ...`): `income`, `expense`, `category-add`, `transactions`, `void`, `audit`, `dashboard`, `pnl`.

## How they connect

* A payment on an itemised invoice → Sales Statement in the statements channel + ledger entry.
* A new itemised invoice → card in the invoice channel.
* `/sale add`, `/expense add` and quick-invoice payments → also recorded in the books, so `/finance dashboard` and the P&L include them.
* `/finance income` and `/finance expense` → also posted as Sales / Expense Statements.

So **Total Sales** and **Total Expenses** on statements cover everything.

## Files

```
bot.py  app.py  config.py  data.py  ui.py  requirements.txt
cogs/finance.py     /sale /expense /statement
cogs/invoices.py    quick invoice form
desk/               database, invoices, finance, tickets, sales, views, bridge, pdf_gen, utils
logo.png  NotoSans-*.ttf   invoice PDF branding (replace logo.png with yours)
tests/              python -m unittest discover -s tests -t .
```

Back up `data.json` and the `data/` folder (database + transcripts); never wipe them on reinstall.
