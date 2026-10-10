# Paper Trader

An automatic crypto trading bot that trades pretend money on the five largest
coins, plus a dashboard you open on your iPhone. It runs for free on GitHub.
No real money is ever involved.

## Set it up (about 10 minutes)

A computer is easiest, but every step also works in Safari on an iPhone.

**1. Create a free GitHub account** at github.com, if you don't have one.

**2. Create a repository.** Tap **+** then **New repository**. Name it
`paper-trader`, choose **Public**, and tap **Create repository**.
(Public is what makes it free. Nothing private is stored in it.)

**3. Upload the files.** On the new repository page tap **uploading an
existing file**. Select every file from this folder and tap **Commit changes**.
On an iPhone: save the zip to the Files app, tap it to unzip, then choose all
the files inside.

**4. Add the schedule.** Tap the **Actions** tab, then **set up a workflow
yourself**. Delete the sample text in the editor, paste in everything from
`workflow.txt`, and tap **Commit changes** twice.

**5. Turn on the dashboard.** Tap **Settings**, then **Pages**. Under
**Branch** choose **main**, leave the folder as **/ (root)**, and tap **Save**.

**6. Start the bot.** Tap **Actions**, then **Trading bot**, then
**Run workflow**. After this it runs by itself about every 5 minutes.

**7. Put it on your iPhone.** In Safari open

    https://YOUR-USERNAME.github.io/paper-trader/

Tap the **Share** button, then **Add to Home Screen**. It now opens like an app.
The page can take a minute or two to appear the first time.

## Optional: let AI read the news (free, 3 minutes)

Without this the bot reads headlines by matching keywords. With it, an AI
model judges what each headline means, which coins it affects and how much it
matters, and writes a short market summary for the dashboard.

1. Go to console.groq.com, sign in, open **API Keys**, and create a key. Copy it.
2. In your repository: **Settings**, **Secrets and variables**, **Actions**,
   **New repository secret**.
3. Name: `GROQ_API_KEY`. Secret: paste the key. Tap **Add secret**.

The News tab on the dashboard shows which reader is in use. A Google Gemini key
works too; name the secret `GEMINI_API_KEY` instead.

## Reading the dashboard

- **Now**: what the bot is doing in one sentence, account value, how it
  compares with simply holding the coins, open positions, latest decisions.
- **Coins**: each coin's signal from -1 to +1. Tap **Show the evidence** to see
  the six things behind it and what each one says.
- **Activity**: results so far, every decision with its reason, every closed trade.
- **News**: the headlines it read and how it judged them.
- **System**: the trading rules, the costs it assumes, and which data sources
  are answering.

## What it looks at

| Evidence | Where it comes from |
|---|---|
| Price action | 5-minute and hourly candles from Kraken, Coinbase, Binance or OKX |
| Order flow | Live order book, futures funding rates and open interest |
| News | CoinDesk, Cointelegraph, Decrypt, The Block, CryptoSlate, Bitcoin Magazine, Google News, CNBC, the Federal Reserve |
| Crowd mood | Fear & Greed index, Reddit when reachable |
| On-chain | Active addresses, exchange inflows and outflows, stablecoin supply, on-chain trading volume |
| Wider economy | Nasdaq futures, the US dollar, the 10-year Treasury yield, the VIX, the economic calendar |

A source that is down is left out until it comes back.

## Changing the settings

Open `config.json` in your repository, tap the pencil, edit, and commit.

| Setting | Meaning | Default |
|---|---|---|
| `start_balance` | Pretend money to start with | 10000 |
| `fee_pct` | Fee charged on every buy and sell, in percent | 0.20 |
| `slippage_pct` | Minimum slippage on every buy and sell, in percent | 0.05 |
| `enter_score` | Signal needed to buy. Higher means fewer, pickier trades | 0.25 |
| `exit_score` | Signal at which it sells early | -0.15 |
| `max_position_pct` | Most of the account it puts in one coin | 25 |
| `daily_loss_limit_pct` | Stops opening trades for the day after losing this much | 3.0 |
| `reset_counter` | Change this number to wipe the account and start over | 0 |

## If something looks wrong

- **Dashboard says "Waiting for the bot's first check-in"**: open **Actions**
  and check the latest run has a green tick. Step 6 starts one by hand.
- **The time at the top right is more than 20 minutes old**: GitHub's timer is
  best-effort and sometimes runs late or skips a turn. Stop-losses touched in
  the gap are still honoured at the next check-in.
- **Red dots on the System tab**: that source is blocked or down. A few red
  dots are normal. Reddit in particular often blocks cloud servers.
- **GitHub emails that the workflow will be disabled**: this can happen after
  about 60 days. Tap the button in the email, or **Actions** then **Enable workflow**.

## Good to know

- This is a practice simulator, not financial advice. Most short-term trading
  strategies lose money after fees. Compare the bot with the "Just holding"
  line for at least a few weeks before drawing any conclusion.
- The default trading rules are sensible starting points. They have not been
  proven on real market history.
- Because the repository is public, anyone who has the link can see the
  dashboard. Your AI key is stored as a secret and is not visible to anyone.
