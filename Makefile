.PHONY: run_local

delete_webhook:
	python -c "import asyncio, config; from aiogram import Bot; asyncio.run(Bot(config.TELEGRAM_BOT_TOKEN).delete_webhook(drop_pending_updates=True))"

run_local: delete_webhook
	watchfiles --filter python "python bot.py"