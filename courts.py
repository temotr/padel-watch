name: Telegram listener

on:
  workflow_dispatch:         # started every minute by cron-job.org; each run listens for ~50 s

concurrency:
  group: tg-listener
  cancel-in-progress: false

permissions:
  contents: write
  actions: write

jobs:
  listen:
    runs-on: ubuntu-latest
    timeout-minutes: 3        # one run listens for about 50 seconds
    steps:
      - uses: actions/checkout@v4

      - name: Check Telegram for new messages
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          GITHUB_TOKEN: ${{ github.token }}
          MEETUP_EVENTS_URL: ${{ secrets.MEETUP_EVENTS_URL }}
        run: python3 tg_listen.py