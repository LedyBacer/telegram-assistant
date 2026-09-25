/* Shared application state: identity, settings, language, locale dictionary,
 * and the active section. Kept deliberately flat and tiny so a future reader
 * (or a small LLM) can reason about the whole app from one file.
 */

import { api } from "./api.js";

export const state = {
  me: null, // { user, settings }
  lang: "ru", // active UI language (ru | en)
  tab: "today",
  month: null, // { y, m } cursor for the calendar (m is 0-based)
  selectedDate: null, // "YYYY-MM-DD" local date for the calendar
  editId: null, // item id open in the edit view (tab === "edit")
  editReturn: "today", // tab to go back to when leaving the edit view
};

/**
 * Boot-time Russian fallback for the Mini App strings (V5 §14). The real
 * dictionary is fetched from the API on load, but this built-in copy is what
 * the UI renders before that fetch resolves — and what it falls back to if the
 * fetch fails — so the app never paints raw i18n keys. Kept in sync with the
 * `miniapp.*` keys in src/assistant/i18n/locales/ru.json.
 */
export const FALLBACK_RU = {
  "miniapp.tab_today": "📅 Сегодня",
  "miniapp.tab_upcoming": "🗓️ Ближайшие",
  "miniapp.tab_new": "➕ Новая",
  "miniapp.tab_workouts": "💪 Тренировки",
  "miniapp.tab_files": "📁 Файлы",
  "miniapp.tab_facts": "🧠 Факты",
  "miniapp.tab_settings": "⚙️ Настройки",
  "miniapp.tab_more": "Ещё",
  "miniapp.status_auth": "Не удалось пройти проверку — открой приложение заново из бота.",
  "miniapp.status_open": "Открой эту страницу внутри Telegram, чтобы продолжить.",
  "miniapp.empty_today": "На сегодня ничего не запланировано.",
  "miniapp.empty_upcoming": "В ближайшие 7 дней ничего нет.",
  "miniapp.btn_done": "✓ Готово",
  "miniapp.btn_cancel": "Отменить",
  "miniapp.btn_delete": "Удалить",
  "miniapp.btn_edit": "Изменить",
  "miniapp.item_starts": "Начало {when}",
  "miniapp.item_ends": "Конец {when}",
  "miniapp.item_due": "Срок {when}",
  "miniapp.edit_title": "Изменить задачу / событие",
  "miniapp.new_ends": "Конец",
  "miniapp.clear": "Очистить",
  "miniapp.item_not_found": "Этот элемент больше недоступен.",
  "miniapp.reminder_offset": "за {n} мин до начала",
  "miniapp.reminder_at_start": "в начале",
  "miniapp.reminders_empty": "Напоминаний не запланировано.",
  "miniapp.reminder_cancelled": "Напоминание отменено.",
  "miniapp.new_title": "Новая задача / событие",
  "miniapp.new_task": "Задача",
  "miniapp.new_event": "Событие",
  "miniapp.new_low": "Низкий",
  "miniapp.new_normal": "Обычный",
  "miniapp.new_high": "Высокий",
  "miniapp.new_starts": "Начало",
  "miniapp.new_due": "Срок",
  "miniapp.new_end": "Окончание",
  "miniapp.new_reminders": "Напоминания",
  "miniapp.new_title_ph": "Название",
  "miniapp.reminder_preset_10": "за 10 мин",
  "miniapp.reminder_preset_30": "за 30 мин",
  "miniapp.reminder_preset_60": "за час",
  "miniapp.reminder_preset_1440": "за день",
  "miniapp.reminder_min": "{n} мин",
  "miniapp.reminder_custom_ph": "Своё, минут (0–1440)",
  "miniapp.reminder_add": "Добавить",
  "miniapp.reminder_limit": "Не более {n} напоминаний",
  "miniapp.reminder_range": "Допустимо 0–1440 минут",
  "miniapp.reminder_save": "Сохранить напоминания",
  "miniapp.reminder_added": "Напоминания сохранены.",
  "miniapp.reminder_needs_start": "Сначала задайте время начала.",
  "miniapp.save": "Сохранить",
  "miniapp.saved": "Сохранено.",
  "miniapp.discard_confirm": "Покинуть без сохранения? Несохранённые изменения будут потеряны.",
  "miniapp.discard": "Покинуть",
  "miniapp.workouts_stats": "Статистика",
  "miniapp.stats_total": "всего тренировок",
  "miniapp.stats_minutes": "всего минут",
  "miniapp.stats_week": "за неделю",
  "miniapp.stats_streak": "дн. подряд",
  "miniapp.workout_log": "Записать тренировку",
  "miniapp.workout_name_ph": "Название тренировки (напр. Тяжёлый день)",
  "miniapp.when": "Когда",
  "miniapp.minutes": "Минуты",
  "miniapp.minutes_ph": "Минуты",
  "miniapp.effort": "Усилие (необязательно)",
  "miniapp.log": "Записать",
  "miniapp.workout_logged": "Тренировка записана.",
  "miniapp.recent": "Недавние",
  "miniapp.effort_value": "усилие {value}/10",
  "miniapp.minutes_value": "{minutes} мин",
  "miniapp.workouts_empty": "Тренировки ещё не записаны.",
  "miniapp.search": "Поиск",
  "miniapp.search_ph": "Поиск по файлам…",
  "miniapp.searching": "Ищем…",
  "miniapp.search_chunk": "{file} · фрагмент {position}",
  "miniapp.search_empty": "Ничего не найдено.",
  "miniapp.my_files": "Мои файлы",
  "miniapp.files_empty": "Файлы ещё не загружены — отправь документ боту.",
  "miniapp.facts_add": "Добавить факт (остаётся предложенным до подтверждения)",
  "miniapp.facts_value_ph": "Расскажи ассистенту, что запомнить",
  "miniapp.facts_category_ph": "Категория (необязательно)",
  "miniapp.propose": "Предложить",
  "miniapp.proposed": "Предложено.",
  "miniapp.my_facts": "Мои факты",
  "miniapp.confirm": "Подтвердить",
  "miniapp.reject": "Отклонить",
  "miniapp.facts_empty": "Фактов пока нет.",
  "miniapp.settings": "Настройки",
  "miniapp.settings_timezone": "Часовой пояс",
  "miniapp.settings_digest": "Время утреннего дайджеста",
  "miniapp.settings_motivation": "Присылать мотивационные сообщения",
  "miniapp.settings_language": "Язык",
  "miniapp.settings_saved": "Настройки сохранены.",
  "miniapp.tz_search_ph": "Поиск часового пояса…",
  "miniapp.lang_ru": "Русский",
  "miniapp.lang_en": "English",
  "miniapp.error_load": "Не удалось загрузить раздел. Попробуй ещё раз.",
  "miniapp.error_generic": "Что-то пошло не так. Попробуй ещё раз.",
  "miniapp.retry": "Повторить",
  "miniapp.confirm_delete": "Удалить «{title}»?",
  "miniapp.deleted": "Удалено.",
  "miniapp.status_completed": "Готово",
  "miniapp.status_cancelled": "Отменено",
  "miniapp.status_overdue": "Просрочено",
  "miniapp.calendar_empty_day": "На этот день ничего не запланировано.",
  "miniapp.today_summary": "Итоги дня",
  "miniapp.summary_total": "Всего: {n}",
  "miniapp.summary_left": "Осталось: {n}",
  "miniapp.summary_done": "Готово: {n}",
  "miniapp.summary_overdue": "Просрочено: {n}",
  "miniapp.cal_prev": "Предыдущий месяц",
  "miniapp.cal_next": "Следующий месяц",
  "miniapp.cal_today": "Сегодня",
  "miniapp.new_description": "Описание",
  "miniapp.new_description_ph": "Описание (необязательно)",
  "miniapp.new_kind": "Тип",
  "miniapp.new_priority": "Приоритет",
  "miniapp.not_set": "Не задано",
  "miniapp.new_title_required": "Введи название.",
  "miniapp.now": "Сейчас",
  "miniapp.workout_name_required": "Введи название тренировки.",
  "miniapp.upload": "Загрузить файл",
  "miniapp.uploading": "Загрузка…",
  "miniapp.uploaded": "Файл «{name}» загружен.",
  "miniapp.upload_failed": "Не удалось загрузить файл.",
  "miniapp.state_queued": "в очереди",
  "miniapp.state_downloading": "загружается",
  "miniapp.state_extracting": "извлечение текста",
  "miniapp.state_chunking": "нарезка",
  "miniapp.state_embedding": "встраивание",
  "miniapp.state_indexed": "готов",
  "miniapp.state_failed": "ошибка",
  "miniapp.state_rejected": "отклонён",
  "miniapp.fact_proposed": "предложен",
  "miniapp.fact_confirmed": "подтверждён",
  "miniapp.fact_rejected": "отклонён",
  "miniapp.fact_superseded": "заменён",
  "miniapp.facts_category": "Категория",
  "miniapp.fact_value_required": "Введи текст факта.",
  "miniapp.sheet_cancel": "Отмена",
  "miniapp.pick_datetime": "Выбрать дату и время",
  "miniapp.pick_time": "Выбрать время",
  "miniapp.size_bytes": "{n} Б",
  "miniapp.size_kb": "{n} КБ",
  "miniapp.size_mb": "{n} МБ",
  "miniapp.tab_actions": "⏳ Действия",
  "miniapp.actions_empty": "Нет ожидающих действий — перед изменениями ассистент спросит вас здесь.",
  "miniapp.action_expires": "истекает {when}",
  "miniapp.action_stale": "Это действие больше не актуально.",
  "miniapp.action_stale_detail": "Действие неактуально: {reason}",
  "miniapp.action_target_item": "Цель: запись №{id}",
  "miniapp.action_target_reminder": "Цель: напоминание №{id}",
  "miniapp.action_proposed": "ожидает",
  "miniapp.action_executed": "выполнено",
  "miniapp.action_rejected": "отклонено",
  "miniapp.action_expired": "истекло",
  "miniapp.workout_schedule": "Запланировать тренировку",
  "miniapp.when_required": "Сначала выбери время.",
  "miniapp.schedule": "Запланировать",
  "miniapp.workout_scheduled": "Тренировка запланирована.",
  "miniapp.file_retry": "Повторить индексацию",
  "miniapp.fact_replace": "Заменить",
  "miniapp.fact_replace_ph": "Новое значение",
  "miniapp.fact_replaces": "Заменяет",
  "miniapp.fact_replacement": "замена",
  "miniapp.fact_replaced_by": "Заменено на",
  "miniapp.proactive_title": "Проактивные уведомления",
  "miniapp.proactive_enabled": "Проактивные уведомления",
  "miniapp.proactive_weekly_review": "Еженедельный обзор (понедельник)",
  "miniapp.proactive_workout_nudge": "Напоминание о тренировке (нет тренировки 48 ч)",
  "miniapp.proactive_overdue_nudge": "Напоминание о просроченных задачах",
  "miniapp.proactive_quiet_from": "Тихие часы с",
  "miniapp.proactive_quiet_until": "Тихие часы до",
  "miniapp.proactive_max_per_day": "Максимум напоминаний в день",
  "miniapp.proactive_min_interval": "Минимальный интервал между напоминаниями",
  "miniapp.proactive_load_error": "Не удалось загрузить проактивные уведомления.",
};

/**
 * Flat locale dictionary for the active language. Seeded with the Russian
 * fallback so the first paint is real Russian, then replaced by the API dict.
 */
export let STR = { ...FALLBACK_RU };

/**
 * Translate a key with {param} interpolation. Falls back to the key itself so
 * a missing key is obvious in tests and never renders "undefined".
 */
export function S(key, params) {
  let text = STR[key];
  if (text === undefined) text = key;
  return String(text).replace(/\{(\w+)\}/g, (m, k) =>
    params && params[k] !== undefined ? String(params[k]) : m
  );
}

/**
 * Load the locale dictionary for a language and activate it.
 * @param {string} lang
 */
export async function setLanguage(lang) {
  try {
    const remote = await api(`/api/v1/i18n/${lang}`);
    // The server dictionary wins; the built-in fallback covers any key it
    // lacks, so a partial dict can never render a raw key (V5 §14).
    STR = { ...FALLBACK_RU, ...remote };
  } catch {
    // i18n fetch failed — keep the Russian fallback so the UI still renders
    // in real text instead of raw keys.
    STR = { ...FALLBACK_RU };
  }
  state.lang = lang;
  document.documentElement.lang = lang;
  document.title = lang === "en" ? "Assistant" : "Ассистент";
}

/** Bootstrap: fetch identity+settings, then the matching locale. */
export async function loadMe() {
  const me = await api("/api/v1/me");
  state.me = me;
  const lang = me.settings.language || "ru";
  await setLanguage(lang);
  return me;
}

/** Locale code for Intl/Flatpickr (ru / en). */
export function localeCode() {
  return state.lang === "en" ? "en" : "ru";
}
