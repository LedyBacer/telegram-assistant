/* Shared application state: identity, settings, language, locale dictionary,
 * and the active section. Kept deliberately flat and tiny so a future reader
 * (or a small LLM) can reason about the whole app from one file.
 */

import { api } from "./api.js";
import { tgLanguage } from "./telegram.js";

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
 * Boot-time fallbacks for the Mini App strings (V5 §14; V5.1 P1 #13 — the
 * English copy too, so a client reporting `language_code: en` gets real
 * English on first paint, before /me resolves the stored preference). The
 * real dictionary is fetched from the API on load; the built-in copy is what
 * the UI renders before that fetch resolves and what it falls back to if the
 * fetch fails — so the app never paints raw i18n keys. Kept in sync with the
 * `miniapp.*` keys in src/assistant/i18n/locales/{ru,en}.json.
 */
export const FALLBACKS = {
  ru: {
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
  "miniapp.end_before_start": "Конец раньше начала.",
  "miniapp.workout_minutes_invalid": "Введи продолжительность в минутах.",
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
  "miniapp.picker_unavailable": "Не удалось загрузить выбор даты. Проверь соединение.",
  "miniapp.size_bytes": "{n} Б",
  "miniapp.size_kb": "{n} КБ",
  "miniapp.size_mb": "{n} МБ",
  "miniapp.tab_actions": "⏳ Действия",
  "miniapp.actions_empty": "Нет ожидающих действий — перед изменениями ассистент спросит вас здесь.",
  "miniapp.actions_pending": "Ожидают",
  "miniapp.actions_history": "История",
  "miniapp.actions_empty_history": "История пуста.",
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
  "miniapp.app_title": "Ассистент",
  "miniapp.navigation": "Навигация",
  },
  en: {
  "miniapp.tab_today": "📅 Today",
  "miniapp.tab_upcoming": "🗓️ Upcoming",
  "miniapp.tab_new": "➕ New",
  "miniapp.tab_workouts": "💪 Workouts",
  "miniapp.tab_files": "📁 Files",
  "miniapp.tab_facts": "🧠 Facts",
  "miniapp.tab_settings": "⚙️ Settings",
  "miniapp.tab_more": "More",
  "miniapp.status_auth": "Authentication failed — reopen the app from the bot.",
  "miniapp.status_open": "Open this page inside Telegram to continue.",
  "miniapp.empty_today": "Nothing scheduled for today.",
  "miniapp.empty_upcoming": "Nothing in the next 7 days.",
  "miniapp.btn_done": "✓ Done",
  "miniapp.btn_cancel": "Cancel",
  "miniapp.btn_delete": "Delete",
  "miniapp.btn_edit": "Edit",
  "miniapp.item_starts": "Starts {when}",
  "miniapp.item_ends": "Ends {when}",
  "miniapp.item_due": "Due {when}",
  "miniapp.edit_title": "Edit task / event",
  "miniapp.new_ends": "Ends",
  "miniapp.clear": "Clear",
  "miniapp.item_not_found": "This item is no longer available.",
  "miniapp.reminder_offset": "{n} min before",
  "miniapp.reminder_at_start": "at the start",
  "miniapp.reminders_empty": "No reminders scheduled.",
  "miniapp.reminder_cancelled": "Reminder cancelled.",
  "miniapp.new_title": "New task / event",
  "miniapp.new_task": "Task",
  "miniapp.new_event": "Event",
  "miniapp.new_low": "Low",
  "miniapp.new_normal": "Normal",
  "miniapp.new_high": "High",
  "miniapp.new_starts": "Starts",
  "miniapp.new_due": "Due",
  "miniapp.new_end": "Ends",
  "miniapp.new_reminders": "Reminders",
  "miniapp.new_title_ph": "Title",
  "miniapp.reminder_preset_10": "10 min before",
  "miniapp.reminder_preset_30": "30 min before",
  "miniapp.reminder_preset_60": "1 hour before",
  "miniapp.reminder_preset_1440": "1 day before",
  "miniapp.reminder_min": "{n} min",
  "miniapp.reminder_custom_ph": "Custom, minutes (0–1440)",
  "miniapp.reminder_add": "Add",
  "miniapp.reminder_limit": "Max {n} reminders",
  "miniapp.reminder_range": "0–1440 minutes allowed",
  "miniapp.reminder_save": "Save reminders",
  "miniapp.reminder_added": "Reminders saved.",
  "miniapp.reminder_needs_start": "Set a start time before adding reminders.",
  "miniapp.save": "Save",
  "miniapp.saved": "Saved.",
  "miniapp.discard_confirm": "Leave without saving? Unsaved changes will be lost.",
  "miniapp.discard": "Leave",
  "miniapp.workouts_stats": "Stats",
  "miniapp.stats_total": "total workouts",
  "miniapp.stats_minutes": "total minutes",
  "miniapp.stats_week": "this week",
  "miniapp.stats_streak": "day streak",
  "miniapp.workout_log": "Log a workout",
  "miniapp.workout_name_ph": "Workout name (e.g. Push day)",
  "miniapp.when": "When",
  "miniapp.minutes": "Minutes",
  "miniapp.minutes_ph": "Minutes",
  "miniapp.effort": "Effort (optional)",
  "miniapp.log": "Log",
  "miniapp.workout_logged": "Workout logged.",
  "miniapp.recent": "Recent",
  "miniapp.effort_value": "effort {value}/10",
  "miniapp.minutes_value": "{minutes} min",
  "miniapp.workouts_empty": "No workouts logged yet.",
  "miniapp.search": "Search",
  "miniapp.search_ph": "Search your files…",
  "miniapp.searching": "Searching…",
  "miniapp.search_chunk": "{file} · chunk {position}",
  "miniapp.search_empty": "No matches.",
  "miniapp.my_files": "My files",
  "miniapp.files_empty": "No files uploaded yet — send a document to the bot.",
  "miniapp.facts_add": "Add a fact (stays proposed until confirmed)",
  "miniapp.facts_value_ph": "Tell the assistant something to remember",
  "miniapp.facts_category_ph": "Category (optional)",
  "miniapp.propose": "Propose",
  "miniapp.proposed": "Proposed.",
  "miniapp.my_facts": "My facts",
  "miniapp.confirm": "Confirm",
  "miniapp.reject": "Reject",
  "miniapp.facts_empty": "No facts yet.",
  "miniapp.settings": "Settings",
  "miniapp.settings_timezone": "Timezone",
  "miniapp.settings_digest": "Daily digest time",
  "miniapp.settings_motivation": "Send motivational messages",
  "miniapp.settings_language": "Language",
  "miniapp.settings_saved": "Settings saved.",
  "miniapp.tz_search_ph": "Find a timezone…",
  "miniapp.lang_ru": "Русский",
  "miniapp.lang_en": "English",
  "miniapp.error_load": "Couldn't load this section. Please try again.",
  "miniapp.error_generic": "Something went wrong. Please try again.",
  "miniapp.retry": "Retry",
  "miniapp.confirm_delete": "Delete \"{title}\"?",
  "miniapp.deleted": "Deleted.",
  "miniapp.status_completed": "Done",
  "miniapp.status_cancelled": "Cancelled",
  "miniapp.status_overdue": "Overdue",
  "miniapp.calendar_empty_day": "Nothing scheduled for this day.",
  "miniapp.today_summary": "Day summary",
  "miniapp.summary_total": "Total: {n}",
  "miniapp.summary_left": "Left: {n}",
  "miniapp.summary_done": "Done: {n}",
  "miniapp.summary_overdue": "Overdue: {n}",
  "miniapp.cal_prev": "Previous month",
  "miniapp.cal_next": "Next month",
  "miniapp.cal_today": "Today",
  "miniapp.new_description": "Description",
  "miniapp.new_description_ph": "Description (optional)",
  "miniapp.new_kind": "Type",
  "miniapp.new_priority": "Priority",
  "miniapp.not_set": "Not set",
  "miniapp.new_title_required": "Enter a title.",
  "miniapp.end_before_start": "End is before start.",
  "miniapp.workout_minutes_invalid": "Enter duration in minutes.",
  "miniapp.now": "Now",
  "miniapp.workout_name_required": "Enter the workout name.",
  "miniapp.upload": "Upload a file",
  "miniapp.uploading": "Uploading…",
  "miniapp.uploaded": "Uploaded \"{name}\".",
  "miniapp.upload_failed": "Couldn't upload the file.",
  "miniapp.state_queued": "queued",
  "miniapp.state_downloading": "downloading",
  "miniapp.state_extracting": "extracting",
  "miniapp.state_chunking": "chunking",
  "miniapp.state_embedding": "embedding",
  "miniapp.state_indexed": "ready",
  "miniapp.state_failed": "failed",
  "miniapp.state_rejected": "rejected",
  "miniapp.fact_proposed": "proposed",
  "miniapp.fact_confirmed": "confirmed",
  "miniapp.fact_rejected": "rejected",
  "miniapp.fact_superseded": "superseded",
  "miniapp.facts_category": "Category",
  "miniapp.fact_value_required": "Enter the fact text.",
  "miniapp.sheet_cancel": "Cancel",
  "miniapp.pick_datetime": "Pick date and time",
  "miniapp.pick_time": "Pick time",
  "miniapp.picker_unavailable": "Could not load date picker. Check your connection.",
  "miniapp.size_bytes": "{n} B",
  "miniapp.size_kb": "{n} KB",
  "miniapp.size_mb": "{n} MB",
  "miniapp.tab_actions": "⏳ Actions",
  "miniapp.actions_empty": "No pending actions — the assistant will ask you here before changing anything.",
  "miniapp.actions_pending": "Pending",
  "miniapp.actions_history": "History",
  "miniapp.actions_empty_history": "No history yet.",
  "miniapp.action_expires": "expires {when}",
  "miniapp.action_stale": "This action no longer applies to current data.",
  "miniapp.action_stale_detail": "Action no longer applies: {reason}",
  "miniapp.action_target_item": "Target: item #{id}",
  "miniapp.action_target_reminder": "Target: reminder #{id}",
  "miniapp.action_proposed": "pending",
  "miniapp.action_executed": "done",
  "miniapp.action_rejected": "rejected",
  "miniapp.action_expired": "expired",
  "miniapp.workout_schedule": "Schedule a workout",
  "miniapp.when_required": "Pick a time first.",
  "miniapp.schedule": "Schedule",
  "miniapp.workout_scheduled": "Workout scheduled.",
  "miniapp.file_retry": "Retry indexing",
  "miniapp.fact_replace": "Replace",
  "miniapp.fact_replace_ph": "New value",
  "miniapp.fact_replaces": "Replaces",
  "miniapp.fact_replacement": "replacement",
  "miniapp.fact_replaced_by": "Replaced by",
  "miniapp.proactive_title": "Proactive notifications",
  "miniapp.proactive_enabled": "Enable proactive notifications",
  "miniapp.proactive_weekly_review": "Weekly review (Monday)",
  "miniapp.proactive_workout_nudge": "Workout nudge (no workout for 48 h)",
  "miniapp.proactive_overdue_nudge": "Overdue task nudge",
  "miniapp.proactive_quiet_from": "Quiet hours from",
  "miniapp.proactive_quiet_until": "Quiet hours until",
  "miniapp.proactive_max_per_day": "Max nudges per day",
  "miniapp.proactive_min_interval": "Minimum interval between nudges",
  "miniapp.proactive_load_error": "Could not load proactive notifications.",
  "miniapp.app_title": "Assistant",
  "miniapp.navigation": "Navigation",
  },
};

/**
 * Flat locale dictionary for the active language. Bootstrapped with the
 * client-language fallback (V5.1 P1 #13: the Telegram client's reported
 * `language_code` is all we know before /me resolves the stored
 * preference), then replaced by the API dict.
 */
export let STR = { ...FALLBACKS[tgLanguage() === "en" ? "en" : "ru"] };

// The client language is also the <html> lang and state.lang on first paint;
// /me's stored preference overrides both via setLanguage().
state.lang = tgLanguage() === "en" ? "en" : "ru";
document.documentElement.lang = state.lang;

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
 * Localize the static shell (V5.1 P1 #14): the app title heading and the
 * bottom nav's aria-label are fixed markup in index.html, so re-apply the
 * active language to them whenever the dictionary changes.
 */
export function syncShellLabels() {
  const title = document.getElementById("app-title");
  if (title) title.textContent = S("miniapp.app_title");
  const nav = document.getElementById("nav");
  if (nav) nav.setAttribute("aria-label", S("miniapp.navigation"));
  document.title = S("miniapp.app_title");
}

syncShellLabels();

/**
 * Load the locale dictionary for a language and activate it.
 * @param {string} lang
 */
export async function setLanguage(lang) {
  const fallback = FALLBACKS[lang] || FALLBACKS.ru;
  try {
    const remote = await api(`/api/v1/i18n/${lang}`);
    // The server dictionary wins; the built-in fallback covers any key it
    // lacks, so a partial dict can never render a raw key (V5 §14).
    STR = { ...fallback, ...remote };
  } catch {
    // i18n fetch failed — keep the language's fallback so the UI still
    // renders in real text instead of raw keys.
    STR = { ...fallback };
  }
  state.lang = lang;
  document.documentElement.lang = lang;
  syncShellLabels();
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
