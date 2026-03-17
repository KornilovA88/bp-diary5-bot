import os
import re
import logging
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
import subprocess
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ── Config ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Database ─────────────────────────────────────────────────────────────
DB_PATH = os.environ.get("DB_PATH", "bp_diary.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            systolic INTEGER NOT NULL,
            diastolic INTEGER NOT NULL,
            pulse INTEGER,
            medication TEXT,
            notes TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_user_time 
        ON entries(user_id, timestamp DESC)
    """)
    conn.commit()
    conn.close()


# ── BP Classification ────────────────────────────────────────────────────
def classify_bp(sys_val: int, dia_val: int) -> tuple[str, str]:
    if sys_val < 120 and dia_val < 80:
        return "🟢", "Оптимальное"
    elif sys_val < 130 and dia_val < 85:
        return "🟢", "Нормальное"
    elif sys_val < 140 and dia_val < 90:
        return "🟡", "Повышенное"
    elif sys_val < 160 and dia_val < 100:
        return "🟠", "Гипертония 1 ст."
    elif sys_val < 180 and dia_val < 110:
        return "🔴", "Гипертония 2 ст."
    else:
        return "🚨", "Криз!"


# ── Photo Recognition via Tesseract OCR ─────────────────────────────────
def recognize_photo(photo_bytes: bytes) -> dict:
    """
    Process tonometer photo with Tesseract OCR.
    Pre-processes image with ImageMagick, then extracts numbers.
    """
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_in:
        tmp_in.write(photo_bytes)
        tmp_in_path = tmp_in.name

    tmp_processed = tmp_in_path.replace(".jpg", "_proc.png")
    tmp_out_base = tmp_in_path.replace(".jpg", "_ocr")

    try:
        # Pre-process: grayscale + contrast + sharpen + threshold
        subprocess.run(
            [
                "convert", tmp_in_path,
                "-colorspace", "Gray",
                "-contrast-stretch", "5%x5%",
                "-sharpen", "0x2",
                "-threshold", "50%",
                "-negate",
                tmp_processed,
            ],
            check=True, capture_output=True,
        )

        results = []

        # Run OCR on both original and processed with digits-only whitelist
        for img_path in [tmp_in_path, tmp_processed]:
            try:
                subprocess.run(
                    [
                        "tesseract", img_path, tmp_out_base,
                        "--psm", "6",
                        "-c", "tessedit_char_whitelist=0123456789 /",
                    ],
                    capture_output=True, text=True, timeout=15,
                )
                out_file = tmp_out_base + ".txt"
                if os.path.exists(out_file):
                    with open(out_file) as f:
                        results.append(f.read())
                    os.unlink(out_file)
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
                continue

        # Also try without whitelist (wider net)
        try:
            subprocess.run(
                ["tesseract", tmp_in_path, tmp_out_base, "--psm", "6"],
                capture_output=True, text=True, timeout=15,
            )
            out_file = tmp_out_base + ".txt"
            if os.path.exists(out_file):
                with open(out_file) as f:
                    results.append(f.read())
                os.unlink(out_file)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            pass

        all_text = " ".join(results)
        logger.info(f"OCR raw: {all_text!r}")

        numbers = [int(n) for n in re.findall(r"\b(\d{2,3})\b", all_text)]
        logger.info(f"OCR numbers: {numbers}")

        if not numbers:
            return {"error": "Не удалось найти числа на фото"}

        seen = set()
        unique = []
        for n in numbers:
            if n not in seen:
                seen.add(n)
                unique.append(n)

        if len(unique) < 2:
            return {"error": "Найдено слишком мало чисел", "found_numbers": unique}

        # Heuristic: sort descending, assign systolic > diastolic > pulse
        sorted_nums = sorted(unique, reverse=True)

        systolic = diastolic = pulse = None
        for n in sorted_nums:
            if systolic is None and 80 <= n <= 250:
                systolic = n
            elif diastolic is None and 40 <= n <= 160 and (systolic is None or n < systolic):
                diastolic = n
            elif pulse is None and 35 <= n <= 200 and n != systolic and n != diastolic:
                pulse = n

        if not systolic or not diastolic:
            return {"error": "Не удалось определить давление", "found_numbers": unique}
        if systolic <= diastolic:
            return {"error": f"Верхнее ({systolic}) ≤ нижнего ({diastolic})", "found_numbers": unique}

        return {"systolic": systolic, "diastolic": diastolic, "pulse": pulse}

    finally:
        for f in [tmp_in_path, tmp_processed]:
            if os.path.exists(f):
                os.unlink(f)


# ── Conversation states ──────────────────────────────────────────────────
WAITING_MEDICATION = 0
WAITING_NOTES = 1


# ── Handlers ─────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❤️ *Дневник давления*\n\n"
        "Я помогу вести учёт артериального давления.\n\n"
        "📸 *Отправьте фото тонометра* — распознаю цифры\n"
        "✏️ *Или введите вручную:* `120/80 72`\n"
        "   (давление и пульс через пробел)\n\n"
        "📋 /history — последние 10 записей\n"
        "📊 /stats — статистика за неделю\n"
        "🖨 /export — таблица для печати\n"
        "❓ /help — справка\n",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "*Добавить запись:*\n"
        "• Отправьте фото экрана тонометра\n"
        "• Или напишите: `130/85` или `130/85 72`\n"
        "  (верхнее/нижнее пульс)\n\n"
        "*После ввода давления:*\n"
        "• Бот спросит про лекарства\n"
        "• Затем про заметки (самочувствие)\n"
        "• Можно пропустить — нажмите кнопку\n\n"
        "*Команды:*\n"
        "/history — последние 10 записей\n"
        "/history\\_all — все записи\n"
        "/stats — средние за 7 дней\n"
        "/export — полная таблица (для печати)\n"
        "/delete — удалить последнюю запись\n"
        "/cancel — отменить текущий ввод\n",
        parse_mode="Markdown",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Ввод отменён.")
    return ConversationHandler.END


# ── Entry flow ───────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Распознаю показания...")

    photo = update.message.photo[-1]
    file = await photo.get_file()
    buf = BytesIO()
    await file.download_to_memory(buf)

    try:
        result = recognize_photo(buf.getvalue())
    except Exception as e:
        logger.error(f"OCR error: {e}")
        await msg.edit_text(
            "😕 Ошибка распознавания.\nВведите вручную: `120/80 72`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if "error" in result:
        extra = ""
        if "found_numbers" in result:
            extra = f"\nНайденные числа: {', '.join(str(n) for n in result['found_numbers'])}"
        await msg.edit_text(
            f"😕 {result['error']}{extra}\n\nВведите вручную: `120/80 72`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    sys_val = result["systolic"]
    dia_val = result["diastolic"]
    pulse_val = result.get("pulse")
    emoji, label = classify_bp(sys_val, dia_val)

    context.user_data["systolic"] = sys_val
    context.user_data["diastolic"] = dia_val
    context.user_data["pulse"] = pulse_val

    pulse_text = f"\n💓 Пульс: *{pulse_val}* уд/мин" if pulse_val else ""

    keyboard = [[
        InlineKeyboardButton("✅ Верно", callback_data="confirm_ocr"),
        InlineKeyboardButton("❌ Неверно", callback_data="reject_ocr"),
    ]]

    await msg.edit_text(
        f"🔍 Распознано:\n\n"
        f"🩸 Давление: *{sys_val}/{dia_val}* мм рт.ст.{pulse_text}\n"
        f"{emoji} Статус: *{label}*\n\n"
        f"Верно?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_MEDICATION


async def confirm_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_med")]]
    await query.edit_text(
        query.message.text + "\n\n"
        "💊 Принимали лекарства? Напишите названия или нажмите «Пропустить»",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_MEDICATION


async def reject_ocr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_text(
        "Введите показания вручную: `120/80 72`\n(давление и пульс через пробел)",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def handle_text_bp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "/" not in text:
        return ConversationHandler.END

    parts = text.replace(",", " ").split()
    bp_part = parts[0]

    try:
        bp_parts = bp_part.split("/")
        sys_val = int(bp_parts[0].strip())
        dia_val = int(bp_parts[1].strip())
        pulse_val = int(parts[1].strip()) if len(parts) > 1 else None
    except (ValueError, IndexError):
        await update.message.reply_text(
            "🤔 Не могу разобрать. Формат:\n`120/80` или `120/80 72`",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if sys_val < 60 or sys_val > 300 or dia_val < 30 or dia_val > 200:
        await update.message.reply_text("⚠️ Проверьте значения давления (60–300 / 30–200)")
        return ConversationHandler.END
    if pulse_val and (pulse_val < 30 or pulse_val > 250):
        await update.message.reply_text("⚠️ Проверьте пульс (30–250)")
        return ConversationHandler.END

    emoji, label = classify_bp(sys_val, dia_val)
    context.user_data["systolic"] = sys_val
    context.user_data["diastolic"] = dia_val
    context.user_data["pulse"] = pulse_val

    pulse_text = f"\n💓 Пульс: *{pulse_val}* уд/мин" if pulse_val else ""
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_med")]]

    await update.message.reply_text(
        f"🩸 Давление: *{sys_val}/{dia_val}* мм рт.ст.{pulse_text}\n"
        f"{emoji} Статус: *{label}*\n\n"
        f"💊 Принимали лекарства? Напишите названия или нажмите «Пропустить»",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_MEDICATION


async def medication_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["medication"] = update.message.text.strip()
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_notes")]]
    await update.message.reply_text(
        "📝 Заметки? (самочувствие, обстоятельства...)\nИли нажмите «Пропустить»",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_NOTES


async def skip_medication(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["medication"] = None
    keyboard = [[InlineKeyboardButton("⏭ Пропустить", callback_data="skip_notes")]]
    await query.edit_text(
        query.message.text + "\n\n"
        "📝 Заметки? (самочувствие, обстоятельства...)\nИли нажмите «Пропустить»",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return WAITING_NOTES


async def notes_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.message.text.strip()
    return await save_entry(update, context, is_callback=False)


async def skip_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["notes"] = None
    return await save_entry(update, context, is_callback=True)


async def save_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback: bool):
    data = context.user_data
    now = datetime.now().isoformat()
    user_id = update.effective_user.id

    conn = get_db()
    conn.execute(
        """INSERT INTO entries (user_id, timestamp, systolic, diastolic, pulse, medication, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, now, data["systolic"], data["diastolic"],
         data.get("pulse"), data.get("medication"), data.get("notes")),
    )
    conn.commit()
    row = conn.execute("SELECT COUNT(*) as cnt FROM entries WHERE user_id = ?", (user_id,)).fetchone()
    total = row["cnt"]
    conn.close()

    emoji, label = classify_bp(data["systolic"], data["diastolic"])
    med_line = f"\n💊 {data['medication']}" if data.get("medication") else ""
    notes_line = f"\n📝 {data['notes']}" if data.get("notes") else ""
    pulse_line = f"  💓 {data['pulse']}" if data.get("pulse") else ""

    text = (
        f"✅ *Запись сохранена!*\n\n"
        f"🩸 *{data['systolic']}/{data['diastolic']}*{pulse_line}\n"
        f"{emoji} {label}{med_line}{notes_line}\n\n"
        f"📊 Всего записей: {total}"
    )

    if is_callback:
        await update.callback_query.edit_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

    context.user_data.clear()
    return ConversationHandler.END


# ── History ──────────────────────────────────────────────────────────────
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_id = ? ORDER BY timestamp DESC LIMIT 10",
        (user_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "📋 Пока нет записей.\nОтправьте фото или введите `120/80`",
            parse_mode="Markdown",
        )
        return

    lines = ["📋 *Последние записи:*\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        emoji, _ = classify_bp(r["systolic"], r["diastolic"])
        pulse = f" 💓{r['pulse']}" if r["pulse"] else ""
        med = " 💊" if r["medication"] else ""
        lines.append(f"{emoji} `{dt.strftime('%d.%m %H:%M')}` *{r['systolic']}/{r['diastolic']}*{pulse}{med}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def history_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_id = ? ORDER BY timestamp DESC", (user_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📋 Пока нет записей.")
        return

    lines = ["📋 *Все записи:*\n"]
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        emoji, _ = classify_bp(r["systolic"], r["diastolic"])
        pulse = f" 💓{r['pulse']}" if r["pulse"] else ""
        med = " 💊" if r["medication"] else ""
        lines.append(f"{emoji} `{dt.strftime('%d.%m.%y %H:%M')}` *{r['systolic']}/{r['diastolic']}*{pulse}{med}")

    text = "\n".join(lines)
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000], parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")


# ── Stats ────────────────────────────────────────────────────────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_id = ? AND timestamp >= ? ORDER BY timestamp",
        (user_id, week_ago),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📊 Нет данных за последнюю неделю.")
        return

    sys_vals = [r["systolic"] for r in rows]
    dia_vals = [r["diastolic"] for r in rows]
    pulse_vals = [r["pulse"] for r in rows if r["pulse"]]

    avg_sys = sum(sys_vals) / len(sys_vals)
    avg_dia = sum(dia_vals) / len(dia_vals)
    avg_pulse = sum(pulse_vals) / len(pulse_vals) if pulse_vals else None

    emoji, label = classify_bp(int(avg_sys), int(avg_dia))
    pulse_line = f"\n💓 Средний пульс: *{avg_pulse:.0f}* уд/мин" if avg_pulse else ""

    trend = ""
    if len(sys_vals) >= 3:
        half = len(sys_vals) // 2
        first = sum(sys_vals[:half]) / half
        second = sum(sys_vals[half:]) / (len(sys_vals) - half)
        if second < first - 3:
            trend = "📉 Тренд: давление *снижается*"
        elif second > first + 3:
            trend = "📈 Тренд: давление *растёт*"
        else:
            trend = "➡️ Тренд: *стабильно*"

    await update.message.reply_text(
        f"📊 *Статистика за 7 дней*\n"
        f"Записей: {len(rows)}\n\n"
        f"🩸 Среднее: *{avg_sys:.0f}/{avg_dia:.0f}*\n"
        f"{emoji} Статус: {label}{pulse_line}\n\n"
        f"⬆️ Макс: *{max(sys_vals)}/{max(dia_vals)}*\n"
        f"⬇️ Мин: *{min(sys_vals)}/{min(dia_vals)}*\n\n{trend}",
        parse_mode="Markdown",
    )


# ── Export ───────────────────────────────────────────────────────────────
async def export_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_id = ? ORDER BY timestamp", (user_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📋 Нет записей для экспорта.")
        return

    html_rows = []
    for r in rows:
        dt = datetime.fromisoformat(r["timestamp"])
        _, label = classify_bp(r["systolic"], r["diastolic"])
        html_rows.append(
            f"<tr><td>{dt.strftime('%d.%m.%Y')}</td><td>{dt.strftime('%H:%M')}</td>"
            f"<td><b>{r['systolic']}/{r['diastolic']}</b></td>"
            f"<td>{r['pulse'] or '—'}</td><td>{label}</td>"
            f"<td>{r['medication'] or '—'}</td><td>{r['notes'] or '—'}</td></tr>"
        )

    d1 = datetime.fromisoformat(rows[0]["timestamp"]).strftime("%d.%m.%Y")
    d2 = datetime.fromisoformat(rows[-1]["timestamp"]).strftime("%d.%m.%Y")

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Дневник давления</title>
<style>
body{{font-family:Arial,sans-serif;padding:20px;font-size:13px;color:#222}}
h1{{text-align:center;font-size:20px;margin-bottom:4px}}
.info{{text-align:center;color:#666;margin-bottom:16px;font-size:12px}}
table{{width:100%;border-collapse:collapse}}
th{{background:#2c3e50;color:#fff;padding:8px 6px;text-align:left;font-size:12px}}
td{{padding:7px 6px;border-bottom:1px solid #ddd;font-size:12px}}
tr:nth-child(even) td{{background:#f5f6fa}}
@media print{{body{{padding:0}}}}
</style></head><body>
<h1>Дневник артериального давления</h1>
<div class="info">{d1} — {d2} &middot; Записей: {len(rows)}</div>
<table><tr><th>Дата</th><th>Время</th><th>Давление</th><th>Пульс</th><th>Статус</th><th>Лекарства</th><th>Заметки</th></tr>
{''.join(html_rows)}</table></body></html>"""

    buf = BytesIO(html.encode("utf-8"))
    buf.name = f"bp_diary_{datetime.now().strftime('%Y%m%d')}.html"
    buf.seek(0)
    await update.message.reply_document(
        document=buf, filename=buf.name,
        caption="🖨 Откройте в браузере → Ctrl+P → Печать или PDF",
    )


# ── Delete ───────────────────────────────────────────────────────────────
async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM entries WHERE user_id = ? ORDER BY timestamp DESC LIMIT 1", (user_id,),
    ).fetchone()

    if not row:
        await update.message.reply_text("📋 Нет записей для удаления.")
        conn.close()
        return

    conn.execute("DELETE FROM entries WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()

    dt = datetime.fromisoformat(row["timestamp"])
    await update.message.reply_text(
        f"🗑 Удалена запись от {dt.strftime('%d.%m.%Y %H:%M')}: {row['systolic']}/{row['diastolic']}"
    )


# ── Main ─────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.PHOTO, handle_photo),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_bp),
        ],
        states={
            WAITING_MEDICATION: [
                CallbackQueryHandler(confirm_ocr, pattern="^confirm_ocr$"),
                CallbackQueryHandler(reject_ocr, pattern="^reject_ocr$"),
                CallbackQueryHandler(skip_medication, pattern="^skip_med$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, medication_text),
            ],
            WAITING_NOTES: [
                CallbackQueryHandler(skip_notes, pattern="^skip_notes$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, notes_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("history_all", history_all))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export_data))
    app.add_handler(CommandHandler("delete", delete_last))
    app.add_handler(conv)

    logger.info("Bot started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
