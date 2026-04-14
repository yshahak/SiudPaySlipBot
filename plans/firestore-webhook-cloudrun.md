# Plan: Firestore Persistence + Webhook Support for SiudPaySlipBot

## Context

The bot currently runs in polling mode with `MemoryStorage` — stateless between restarts, no data retained across sessions. The user wants to deploy on **Google Cloud Run**, which requires webhook mode (no persistent process for polling). They also want to persist `employer_name`, `caregiver_name`, and running `vacation_balance`/`sick_balance` totals in **Firestore** so returning users can skip re-entering names and see their accrued entitlements.

**Note:** This intentionally relaxes the "Zero-Data Retention" guarantee for names and balances only. Personal data like passport numbers is still never stored.

---

## Files to Create/Modify

| File | Change |
|---|---|
| `database.py` | **NEW** — Firestore async layer |
| `config.py` | Add `WEBHOOK_URL` and `PORT` env reads |
| `bot.py` | 6 targeted edits (imports, cmd_start, employer_name entry, use_saved handler, _generate_and_send, main) |
| `requirements.txt` | Add `google-cloud-firestore` |
| `Dockerfile` | Add `EXPOSE 8080` |

---

## Step 1: `config.py` — Add Two Env Vars

After the existing `TELEGRAM_BOT_TOKEN` line, add:
```python
WEBHOOK_URL: str | None = os.environ.get("WEBHOOK_URL")  # e.g. "https://my-service.run.app"
PORT: int = int(os.environ.get("PORT", "8080"))
```

---

## Step 2: New `database.py`

Firestore collection: `payslip_users`, document key: `str(user_id)`

Document schema:
```
employer_name:    str
caregiver_name:   str
vacation_balance: float   # cumulative accrued vacation days
sick_balance:     float   # cumulative accrued sick days
```

Functions:
- `init_db() -> None` — creates `AsyncClient()`, assigns to module-level `_db`. Any error → `_db` stays `None`, bot continues without Firestore.
- `get_user(user_id: int) -> dict | None` — returns doc dict or `None`; returns `None` silently on any error.
- `upsert_user(user_id, employer_name, caregiver_name)` — `set(..., merge=True)` with only those two fields (never overwrites balances).
- `add_to_balances(user_id, vacation_delta: Decimal, sick_delta: Decimal)` — `set({"vacation_balance": firestore.Increment(float(vacation_delta)), "sick_balance": firestore.Increment(float(sick_delta))}, merge=True)`. The `Increment` + `merge=True` correctly initializes to 0 on first write.

All functions guard `if _db is None: return None/return` and are wrapped in `try/except Exception`.

---

## Step 3: `bot.py` — Six Targeted Edits

### 3a. Imports (top of file)
Add:
```python
import database
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
```

### 3b. `cmd_start` (line 246)
After `await state.clear()`, query Firestore and store result in FSM state:
```python
user_id = message.from_user.id
saved_data: dict | None = None
try:
    saved_data = await database.get_user(user_id)
except Exception:
    log.warning("Firestore get_user failed — continuing without saved data")
await state.update_data(saved_data=saved_data, user_id=user_id)
```
Also update the privacy disclaimer in the welcome message to acknowledge that names and balances are saved.

### 3c. New `_ask_employer_name(message, state)` helper
Extract the "ask employer name" prompt into a helper that checks `saved_data`. Replace the two hardcoded `await message.answer("מהו שם המעסיק?", reply_markup=_skip_kb())` + `await state.set_state(PayslipForm.employer_name)` calls (in `handle_work_period` at line 329 and `handle_partial_day` at line 396) with a call to this helper.

```python
async def _ask_employer_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    saved: dict | None = data.get("saved_data")
    await state.set_state(PayslipForm.employer_name)

    if saved and saved.get("employer_name", _SKIPPED) != _SKIPPED:
        employer = saved["employer_name"]
        caregiver = saved.get("caregiver_name", _SKIPPED)
        vac = saved.get("vacation_balance", 0.0)
        sick = saved.get("sick_balance", 0.0)
        builder = InlineKeyboardBuilder()
        builder.button(text=f"⚡ פרטים קודמים: {employer} / {caregiver}", callback_data="use_saved")
        builder.button(text="⏭️ דלג", callback_data="skip_field")
        builder.adjust(1)
        await message.answer(
            f"✅ *יתרה צבורה:* {vac:.2f} ימי חופשה | {sick:.2f} ימי מחלה\n\nמהו שם המעסיק?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=builder.as_markup(),
        )
    else:
        await message.answer("מהו שם המעסיק?", reply_markup=_skip_kb())
```

### 3d. New `handle_use_saved` callback handler
Register on `PayslipForm.employer_name` state, `F.data == "use_saved"`. Insert after `skip_employer_name` handler (after line 407):

```python
@router.callback_query(PayslipForm.employer_name, F.data == "use_saved")
async def handle_use_saved(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    saved: dict = data.get("saved_data") or {}
    employer_name = saved.get("employer_name", _SKIPPED)
    caregiver_name = saved.get("caregiver_name", _SKIPPED)
    await state.update_data(employer_name=employer_name, caregiver_name=caregiver_name)
    await callback.message.edit_text(
        f"⚡ נטענו פרטים קודמים:\nמעסיק: {employer_name} | מטפל/ת: {caregiver_name}"
    )
    await callback.message.answer("מהו מספר הדרכון של המטפל/ת?", reply_markup=_skip_kb())
    await state.set_state(PayslipForm.passport)
```

This skips both `employer_name` and `caregiver_name` states in one step, mirroring the two consecutive skips.

### 3e. `_generate_and_send` (line 825)
After `await state.clear()` and `calculate()`, fire DB updates as background tasks then gather them before the follow-up message:

```python
data = await state.get_data()
payslip_input = _build_payslip_input(data)
user_id: int = data.get("user_id", message.chat.id)

await state.clear()  # FSM state gone before PDF

try:
    result = calculate(payslip_input)
except ValueError as exc:
    ...
    return

# Fire DB updates in parallel with PDF generation (non-blocking)
db_upsert = asyncio.create_task(
    database.upsert_user(user_id, result.employer_name, result.caregiver_name)
)
db_balances = asyncio.create_task(
    database.add_to_balances(user_id, result.vacation_accrued, result.sick_accrued)
)

# ... existing PDF generation and send code ...

# Await DB tasks; log errors, don't crash
db_results = await asyncio.gather(db_upsert, db_balances, return_exceptions=True)
for i, r in enumerate(db_results):
    if isinstance(r, Exception):
        log.warning("DB task %d failed: %s", i, r)

# Send updated balance (best-effort)
try:
    updated = await database.get_user(user_id)
    if updated:
        vac = updated.get("vacation_balance", 0.0)
        sick = updated.get("sick_balance", 0.0)
        await message.answer(
            f"✅ *יתרה עדכנית:* {vac:.2f} ימי חופשה | {sick:.2f} ימי מחלה",
            parse_mode=ParseMode.MARKDOWN,
        )
except Exception:
    pass

await message.answer("להפקת תלוש נוסף לחץ /start")
```

Note: `result.employer_name` and `result.caregiver_name` will be `"---"` if skipped — do NOT write `"---"` to Firestore. In `upsert_user`, guard: `if employer_name == "---": return`.

### 3f. `main()` (line 870) — webhook vs polling
```python
async def main() -> None:
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    try:
        await database.init_db()
        log.info("Firestore initialized.")
    except Exception as exc:
        log.warning("Firestore unavailable — running without persistence: %s", exc)

    if config.WEBHOOK_URL:
        webhook_url = f"{config.WEBHOOK_URL}/webhook"
        await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
        log.info("Webhook set: %s", webhook_url)

        app = web.Application()
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
        setup_application(app, dp, bot=bot)

        # Use AppRunner to stay within the async event loop
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=config.PORT)
        await site.start()
        log.info("Webhook server running on port %d", config.PORT)
        await asyncio.Event().wait()  # run forever
    else:
        log.info("No WEBHOOK_URL — polling mode (local dev).")
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
```

`asyncio.Event().wait()` with no `.set()` call blocks indefinitely, which is the standard pattern for keeping an async server alive.

---

## Step 4: `requirements.txt`
Append:
```
google-cloud-firestore==2.19.0
```

---

## Step 5a: `/forget_me` command in `bot.py`

Add a new command handler for `/forget_me`. It deletes the user's Firestore document (employer name, caregiver name, and all balances).

Add to `database.py`:
```python
async def delete_user(user_id: int) -> bool:
    """Delete all stored data for this user. Returns True if deleted, False if not found."""
    if _db is None:
        return False
    try:
        ref = get_db().collection(COLLECTION).document(str(user_id))
        doc = await ref.get()
        if not doc.exists:
            return False
        await ref.delete()
        return True
    except Exception as exc:
        log.warning("delete_user failed for %s: %s", user_id, exc)
        return False
```

Add to `bot.py` (alongside other command handlers):
```python
@router.message(Command("forget_me"))
async def cmd_forget_me(message: Message) -> None:
    user_id = message.from_user.id
    deleted = await database.delete_user(user_id)
    if deleted:
        await message.answer(
            "✅ כל הנתונים שלך נמחקו מהמערכת.\n"
            "שמות, יתרת חופשה ויתרת מחלה — הכל נמחק."
        )
    else:
        await message.answer("ℹ️ לא נמצאו נתונים שמורים עבורך.")
```

Also add `Command` to the aiogram imports: `from aiogram.filters import CommandStart, Command`

---

## Step 5b: `Dockerfile`
Add `EXPOSE 8080` before `CMD`:
```dockerfile
EXPOSE 8080
CMD ["python", "bot.py"]
```

Cloud Run auto-injects `PORT=8080` — `config.py` will read it.

---

## Cloud Run Deployment Notes (not in code)

- Set env vars: `TELEGRAM_BOT_TOKEN`, `WEBHOOK_URL=https://<service-url>`
- Grant the Cloud Run service account `roles/datastore.user` on the Firestore database
- `PORT` is injected automatically by Cloud Run — do not set it manually

---

## Step 6: GitHub Actions CI/CD (`.github/workflows/deploy.yml`)

**GCP project:** `siud-payslip-bot` (number: `967606346792`)

The workflow triggers on push to `main`, builds the Docker image, pushes to **Artifact Registry**, and deploys to **Cloud Run**. Uses **Workload Identity Federation** (no long-lived service account keys stored in GitHub).

### One-time GCP setup (via `gcloud` CLI, not in code):
```bash
# Enable APIs
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  iamcredentials.googleapis.com firestore.googleapis.com --project=siud-payslip-bot

# Create Artifact Registry repo
gcloud artifacts repositories create payslipbot \
  --repository-format=docker --location=europe-west1 --project=siud-payslip-bot

# Create deploy service account
gcloud iam service-accounts create github-deployer \
  --project=siud-payslip-bot

# Grant permissions
gcloud projects add-iam-policy-binding siud-payslip-bot \
  --member="serviceAccount:github-deployer@siud-payslip-bot.iam.gserviceaccount.com" \
  --role="roles/run.admin"
gcloud projects add-iam-policy-binding siud-payslip-bot \
  --member="serviceAccount:github-deployer@siud-payslip-bot.iam.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
gcloud projects add-iam-policy-binding siud-payslip-bot \
  --member="serviceAccount:github-deployer@siud-payslip-bot.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Create Workload Identity Pool + Provider (replace GITHUB_ORG/REPO)
gcloud iam workload-identity-pools create github-pool \
  --location=global --project=siud-payslip-bot
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github-pool \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --project=siud-payslip-bot
gcloud iam service-accounts add-iam-policy-binding \
  github-deployer@siud-payslip-bot.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/967606346792/locations/global/workloadIdentityPools/github-pool/attribute.repository/GITHUB_ORG/REPO_NAME"
```

### GitHub secrets to add:
- `GCP_WORKLOAD_IDENTITY_PROVIDER` — full WIF provider resource name
- `GCP_SERVICE_ACCOUNT` — `github-deployer@siud-payslip-bot.iam.gserviceaccount.com`
- `TELEGRAM_BOT_TOKEN` — the bot token

### Workflow file: `.github/workflows/deploy.yml`
```yaml
name: Deploy to Cloud Run

on:
  push:
    branches: [main]

env:
  PROJECT_ID: siud-payslip-bot
  REGION: europe-west1
  REGISTRY: europe-west1-docker.pkg.dev/siud-payslip-bot/payslipbot
  SERVICE: payslipbot

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write   # required for WIF

    steps:
      - uses: actions/checkout@v4

      - id: auth
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ secrets.GCP_WORKLOAD_IDENTITY_PROVIDER }}
          service_account: ${{ secrets.GCP_SERVICE_ACCOUNT }}

      - uses: google-github-actions/setup-gcloud@v2

      - name: Configure Docker for Artifact Registry
        run: gcloud auth configure-docker ${{ env.REGION }}-docker.pkg.dev --quiet

      - name: Build and push image
        run: |
          docker build -t ${{ env.REGISTRY }}/${{ env.SERVICE }}:${{ github.sha }} .
          docker push ${{ env.REGISTRY }}/${{ env.SERVICE }}:${{ github.sha }}

      - name: Deploy to Cloud Run
        uses: google-github-actions/deploy-cloudrun@v2
        with:
          service: ${{ env.SERVICE }}
          region: ${{ env.REGION }}
          image: ${{ env.REGISTRY }}/${{ env.SERVICE }}:${{ github.sha }}
          env_vars: |
            WEBHOOK_URL=https://payslipbot-<hash>-ew.a.run.app
          secrets: |
            TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest
```

**Note:** `WEBHOOK_URL` must be the actual Cloud Run service URL. After the first manual deploy you'll know this URL and can hardcode it in the workflow (or store it as a GitHub secret).

The Cloud Run service's default service account needs Firestore access:
```bash
gcloud projects add-iam-policy-binding siud-payslip-bot \
  --member="serviceAccount:<PROJECT_NUMBER>-compute@developer.gserviceaccount.com" \
  --role="roles/datastore.user"
```

---

## Verification

1. **Local dev (polling):** `python bot.py` with no `WEBHOOK_URL` env var — bot starts polling as before
2. **Firestore test:** Set `WEBHOOK_URL` and run locally with `gcloud auth application-default login` — verify `get_user` returns data after first payslip
3. **Full flow:** Run through a complete conversation, generate PDF → check Firestore for saved names and updated balances
4. **"Use previous" flow:** Run `/start` again with same user — confirm saved names appear, clicking the button skips employer/caregiver states and jumps to passport
5. **Graceful degradation:** Disconnect Firestore (unset credentials) — confirm bot still generates PDF normally without errors
6. **Tests:** `pytest tests/ -v` must stay 30/30 (no calculator changes)
