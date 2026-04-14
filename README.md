# 🤖 SiudPaySlipBot — מחולל תלושי שכר לעובדי זר בסיעוד

> **Telegram bot that generates legally-compliant Hebrew payslips for foreign caregivers in Israel.**

---

## 🇮🇱 עברית

### מה זה?
בוט טלגרם שמסייע למעסיקים פרטיים של עובדי זר בסיעוד לחשב שכר ולהפיק תלוש שכר רשמי בעברית, בהתאם לחוק העבודה הישראלי (נכון לאפריל 2026).

### מה הבוט עושה?
1. מנהל שיחה בעברית ואוסף נתונים חודשיים
2. מחשב שכר כולל: שכר בסיס, ימי שבת וחג, דמי כיס, ניכויים, והפרשות מעסיק
3. מפיק תלוש שכר PDF בפורמט RTL (עברית) רשמי
4. שולח את הקובץ ומוחק אותו מיידית — **אפס שמירת נתונים**

### כלל הנתונים נמחקים מיידית לאחר שליחת התלוש (Zero-Data Retention).

### קבועים משפטיים (אפריל 2026)
| פרמטר | ערך |
|---|---|
| שכר מינימום | 6,443.85 ₪ |
| שכר יום מנוחה / חג | 439.73 ₪ |
| שכר יומי (÷25) | 257.75 ₪ |
| ניכוי מגורים (מקס') | 192 ₪ |
| ניכוי ביטוח רפואי (מקס') | 169 ₪ |
| הוצאות נלוות (מקס') | 94 ₪ |
| ניכוי כלכלה (מקס') | 644 ₪ |
| פנסיית מעסיק | 6.5% |
| פיצויי מעסיק | 6% |

---

## 🇬🇧 English

### What is this?
An open-source Telegram bot for Israeli employers of foreign caregivers. It collects monthly payroll data through a Hebrew conversation, calculates the salary according to Israeli labor law, generates a formal Hebrew PDF payslip, sends it to the user, and **immediately deletes all data** (Zero-Data Retention policy).

### Features
- ✅ Fully conversational Hebrew UI via Telegram
- ✅ Dynamic minimum wage — looks up the correct rate per month/year
- ✅ Pro-rata salary and deductions for partial months
- ✅ Shabbat & holiday additions at the legal rate
- ✅ Employer contributions section (pension 6.5%, severance 6%)
- ✅ Multi-select deductions keyboard (housing, health, extras, food)
- ✅ Zero-Data Retention — PDF deleted immediately after sending
- ✅ Production-ready Docker container
- ✅ 30 unit tests covering all calculation edge cases

---

## Project Structure

```
SiudPaySlipBot/
├── bot.py                  # aiogram v3 FSM — Telegram conversation
├── calculator.py           # Pure salary math engine
├── config.py               # Labor law constants & dynamic wage history
├── pdf_generator.py        # ReportLab PDF with Hebrew RTL
├── pyproject.toml          # Poetry project definition
├── requirements.txt        # pip dependencies
├── Dockerfile              # Production container
├── .env.example            # Token template
├── .gitignore
│
├── scripts/
│   ├── download_fonts.py   # One-time Hebrew font download
│   └── generate_sample.py  # Visual PDF inspection script
│
├── tests/
│   └── test_calculator.py  # 30 pytest unit tests
│
├── fonts/                  # Created by download_fonts.py (git-ignored)
│   └── NotoSansHebrew-Regular.ttf
│
└── resources/              # Reference documents (not deployed)
    ├── gemini_chat.txt
    └── gemini_initial_prompt_starter.md
```

---

## Setup & Running

### Prerequisites
- Python 3.13+
- A Telegram Bot token from [@BotFather](https://t.me/BotFather)

### Local Development

```bash
# 1. Clone & install dependencies
git clone <repo-url>
cd SiudPaySlipBot
pip install -r requirements.txt

# 2. Download the Hebrew font (one-time)
python scripts/download_fonts.py

# 3. Configure your token
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN=your_token_here

# 4. Run the bot
python bot.py
```

### Visual PDF Inspection

```bash
python scripts/generate_sample.py sample_payslip.pdf
# Opens a full sample payslip — verify Hebrew RTL and layout
```

### Run Tests

```bash
pytest tests/ -v
```

### Docker Deployment

```bash
# Build (downloads font automatically)
docker build -t payslipbot .

# Run
docker run --env-file .env payslipbot
```

---

## Adding Future Wage Updates

When the Israeli minimum wage changes, update **one line** in `config.py`:

```python
WAGE_HISTORY: list[tuple[date, Decimal, Decimal]] = [
    (date(2025, 4, 1), Decimal("6247.67"), Decimal("426.35")),
    (date(2026, 4, 1), Decimal("6443.85"), Decimal("439.73")),
    (date(2027, 4, 1), Decimal("XXXX.XX"), Decimal("XXX.XX")),  # ← add here
]
```

The bot will automatically use the correct rate for any month/year.

---

## Legal Disclaimer

This bot is provided as a community tool to assist with payroll calculations. The output does **not** constitute legal advice. Always verify calculations with a licensed payroll accountant or labor law specialist. The developers are not responsible for errors in salary calculations.

מסמך זה אינו מהווה ייעוץ משפטי. יש לאמת חישובים עם רואה חשבון או יועץ עבודה מוסמך.

---

## License

MIT License — Free to use, modify, and distribute.
